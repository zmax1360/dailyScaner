"""period_report — what happened over a chosen period: the stock, the scanner's top
picks, gamma, and your own trades.

No Streamlit and no network. It reads what the scanner already recorded (scan archives
and the chain snapshots) and is given price bars and trades by the caller. Display only;
nothing here feeds scoring, and nothing is re-scored.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

import gex
import trade_history as th
import volume_history as vh
from scoring_pool import POOL_0DTE, POOL_1DTE

ET = ZoneInfo("America/New_York")
ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive")
PICK_AFTER = time(9, 45)        # the first 15 minutes are left to settle, as on the app
MAX_DAYS = 31
POOLS = (POOL_1DTE, POOL_0DTE)


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


# ── 1. the stock ────────────────────────────────────────────────────────────

def stock_period(bars: pd.DataFrame | None, start: date, end: date) -> dict[str, Any]:
    """Open, high, low and close over [start, end] from daily bars, plus one row per day.

    Open is the first day's open and close the last day's close. Empty dict when no bar
    falls in the period. ``bars`` needs Open/High/Low/Close and a date-like index."""
    if bars is None or getattr(bars, "empty", True):
        return {}
    need = ["Open", "High", "Low", "Close"]
    if not set(need) <= set(bars.columns):
        return {}
    df = bars[need].copy()
    df.index = pd.to_datetime(df.index).date
    df = df[(df.index >= start) & (df.index <= end)].dropna()
    if df.empty:
        return {}
    o, c = float(df["Open"].iloc[0]), float(df["Close"].iloc[-1])
    days = df.reset_index().rename(columns={"index": "day"})
    days["change"] = days["Close"] - days["Open"]
    return {"open": o, "high": float(df["High"].max()), "low": float(df["Low"].min()),
            "close": c, "change": c - o, "change_pct": (c - o) / o if o else None,
            "high_day": df["High"].idxmax(), "low_day": df["Low"].idxmin(),
            "first_day": df.index[0], "last_day": df.index[-1], "days": days}


def trading_days(start: date, end: date, stock: dict[str, Any] | None = None) -> list[date]:
    """Days in the period with a price bar; weekdays when no bars were available."""
    if stock and "days" in stock:
        return list(stock["days"]["day"])
    return [d.date() for d in pd.date_range(start, end, freq="B")]


# ── 2. the scanner's top picks ──────────────────────────────────────────────

def archives_for(ticker: str, day: date, archive_dir: str | None = None) -> list[str]:
    """That day's scan archives for the ticker, oldest first (by the time in the name)."""
    pattern = os.path.join(archive_dir or ARCHIVE_DIR,
                           f"{ticker.upper()}_{day:%Y%m%d}_*.json")
    return sorted(glob.glob(pattern))


def _stamp(path: str) -> datetime | None:
    try:
        raw = os.path.basename(path).rsplit(".", 1)[0].split("_")
        return datetime.strptime(raw[-2] + raw[-1], "%Y%m%d%H%M%S").replace(tzinfo=ET)
    except (ValueError, IndexError):
        return None


def _load(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def first_picks(ticker: str, day: date, archive_dir: str | None = None) -> list[dict[str, Any]]:
    """The top-ranked contract of each DTE pool in the day's first scan at or after 9:45
    that ranked that pool. Read from the ranking the scan stored; nothing is re-scored."""
    found: dict[str, dict[str, Any]] = {}
    for path in archives_for(ticker, day, archive_dir):
        ts = _stamp(path)
        if ts is None or ts.time() < PICK_AFTER:
            continue
        payload = _load(path)
        rows = ((payload or {}).get("best_value") or {}).get("rows") or []
        for pool in POOLS:
            if pool in found:
                continue
            ranked = [r for r in rows if r.get("pool") == pool and _f(r.get("Value_Score")) is not None]
            if not ranked:
                continue
            top = max(ranked, key=lambda r: float(r["Value_Score"]))
            found[pool] = {
                "day": day, "pool": pool, "picked_at": ts,
                "side": str(top.get("side") or "").upper(),
                "strike": _f(top.get("strike")), "expiry": str(top.get("expiry") or "")[:10],
                "score": float(top["Value_Score"]), "open": _f(top.get("last")),
            }
        if len(found) == len(POOLS):
            break
    return [found[p] for p in POOLS if p in found]


def _price(row: pd.Series) -> float | None:
    """A contract's price in one snapshot: the bid/ask midpoint, else the last trade."""
    bid, ask, last = _f(row.get("bid")), _f(row.get("ask")), _f(row.get("last"))
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        return (bid + ask) / 2.0
    return last if last is not None and last > 0 else None


def pick_path(ticker: str, pick: dict[str, Any], db_path: str | None = None) -> dict[str, Any]:
    """Open, high, low and close of a pick's premium for the rest of its day, from the
    chain snapshots. Open is the price stored with the pick; the rest come from every
    snapshot at or after the pick. Missing values stay None."""
    out = {**pick, "high": None, "low": None, "close": None, "snapshots": 0,
           "change_pct": None}
    if pick.get("strike") is None or not pick.get("expiry"):
        return out
    hist = vh.contract_history(ticker, pick["side"], pick["strike"], pick["expiry"],
                               db_path=db_path)
    if hist.empty:
        return out
    hist = hist[hist["session_date"] == f"{pick['day']:%Y-%m-%d}"].copy()
    hist["ts"] = pd.to_datetime(hist["ts_et"], utc=True)
    hist = hist[hist["ts"] >= pd.Timestamp(pick["picked_at"]).tz_convert("UTC")]
    prices = [p for p in (_price(r) for _, r in hist.iterrows()) if p is not None]
    if not prices:
        return out
    series = ([pick["open"]] if pick.get("open") else []) + prices
    out.update(high=max(series), low=min(series), close=prices[-1], snapshots=len(prices))
    if pick.get("open"):
        out["change_pct"] = (prices[-1] - pick["open"]) / pick["open"]
    return out


def picks_report(ticker: str, days: list[date], archive_dir: str | None = None,
                 db_path: str | None = None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for day in days:
        for pick in first_picks(ticker, day, archive_dir):
            out.append(pick_path(ticker, pick, db_path))
    return out


# ── 3. gamma ────────────────────────────────────────────────────────────────

def _session_scans(ticker: str, day: date, db_path: str | None) -> list[tuple[str, str]]:
    path = db_path or vh.DB_PATH
    if not os.path.exists(path):
        return []
    with closing(sqlite3.connect(path, timeout=30)) as con:
        rows = con.execute(
            "SELECT DISTINCT scan_id, ts_et FROM contract_scans WHERE ticker=? AND "
            "session_date=? ORDER BY ts_et", (ticker.upper(), f"{day:%Y-%m-%d}")).fetchall()
    return [(str(a), str(b)) for a, b in rows]


def _scan_rows(ticker: str, scan_id: str, expiry: str, db_path: str | None) -> pd.DataFrame:
    with closing(sqlite3.connect(db_path or vh.DB_PATH, timeout=30)) as con:
        return pd.read_sql_query(
            "SELECT side, strike, expiry, volume, open_interest, bid, ask, last, iv, ts_et,"
            " scan_id FROM contract_scans WHERE ticker=? AND scan_id=? AND expiry=?",
            con, params=(ticker.upper(), scan_id, expiry))


def _nearest_expiry(ticker: str, scan_id: str, day: date, db_path: str | None) -> str | None:
    with closing(sqlite3.connect(db_path or vh.DB_PATH, timeout=30)) as con:
        row = con.execute(
            "SELECT MIN(expiry) FROM contract_scans WHERE ticker=? AND scan_id=? AND expiry>=?",
            (ticker.upper(), scan_id, f"{day:%Y-%m-%d}")).fetchone()
    return row[0] if row and row[0] else None


def gamma_day(ticker: str, day: date, archive_dir: str | None = None,
              db_path: str | None = None) -> dict[str, Any]:
    """Net gamma exposure through one day for the nearest expiry: its high and its low
    (with the time of each), the last reading, and the walls at the last reading, all in
    dollars of dealer hedging per $1 move.

    Each chain snapshot is valued at the stock price its own scan recorded. A snapshot
    whose scan archive is missing is skipped and counted. Empty dict with no readings."""
    readings: list[dict[str, Any]] = []
    skipped = 0
    for scan_id, ts_et in _session_scans(ticker, day, db_path):
        payload = _load(os.path.join(archive_dir or ARCHIVE_DIR, f"{scan_id}.json"))
        spot = _f((payload or {}).get("spot"))
        expiry = _nearest_expiry(ticker, scan_id, day, db_path)
        if spot is None or spot <= 0 or expiry is None:
            skipped += 1
            continue
        as_of = datetime.fromisoformat(ts_et)
        s = gex.summary(_scan_rows(ticker, scan_id, expiry, db_path), spot=spot, as_of=as_of,
                        today=day)
        if s is None:
            skipped += 1
            continue
        readings.append({"at": as_of, "spot": spot, **s})
    if not readings:
        return {}
    hi = max(readings, key=lambda r: r["net"])
    lo = min(readings, key=lambda r: r["net"])
    last = readings[-1]
    return {
        "day": day, "expiry": last["expiry"], "readings": len(readings), "skipped": skipped,
        "net_high": hi["net"], "net_high_at": hi["at"],
        "net_low": lo["net"], "net_low_at": lo["at"],
        "net_last": last["net"], "last_at": last["at"],
        "call_wall": last["call_wall"], "call_wall_gex": last["call_wall_gex"],
        "put_wall": last["put_wall"], "put_wall_gex": last["put_wall_gex"],
        "support": last["support"], "resistance": last["resistance"],
    }


def gamma_report(ticker: str, days: list[date], archive_dir: str | None = None,
                 db_path: str | None = None) -> list[dict[str, Any]]:
    out = []
    for day in days:
        g = gamma_day(ticker, day, archive_dir, db_path)
        if g:
            out.append(g)
    return out


# ── 4. your trades ──────────────────────────────────────────────────────────

def trades_in(trades: pd.DataFrame | None, start: date, end: date,
              ticker: str | None = None) -> pd.DataFrame:
    """Closed trades entered within [start, end], optionally for one ticker."""
    if trades is None or trades.empty:
        return pd.DataFrame(columns=th.TRADE_COLS)
    d = trades["entry"].dt.date
    out = trades[(d >= start) & (d <= end)]
    if ticker:
        out = out[out["underlying"] == ticker.upper()]
    return out


def scorecard_period(trades: pd.DataFrame) -> dict[str, Any]:
    """Trades, wins, losses, break-even trades, dollars gained and lost, the net, and the
    realized reward to risk. Empty dict when there are no trades."""
    s = th.summary(trades)
    if not s:
        return {}
    gained = float(trades.loc[trades["pnl"] > 0, "pnl"].sum())
    lost = float(trades.loc[trades["pnl"] < 0, "pnl"].sum())
    p = th.payoff(trades)
    return {"trades": s["trades"], "wins": s["wins"], "losses": s["losses"],
            "scratches": s["scratches"], "win_rate": s["win_rate"], "gained": gained,
            "lost": lost, "net": s["pnl"],
            "reward_to_risk": p.get("reward_to_risk"),
            "reward_to_risk_needed": p.get("reward_to_risk_needed")}
