"""scanner_mcp — a read-only MCP server over what the scan saved and the broker export.

Gives an assistant (Claude Desktop) the same numbers the app and the Telegram push show:
the Game Plan, the gamma read, the magnets and the scan's freshness, read from the newest
``archive/{TICKER}_*.json``, plus a daily guard built from the newest Wealthsimple export
in ``data/broker/``.

Read only. No network, no writes, no scoring: every number is the one the scan saved
(``plan_report.saved_plan`` / ``saved_gamma``), never worked out again. Missing data is
returned as ``None`` with a reason, never a default.

Run:  python scanner_mcp.py        (needs the optional ``mcp`` extra: ``mcp>=1.2,<2``)
"""

from __future__ import annotations

import glob
import html
import json
import math
import os
import re
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

import ema_stack
import game_plan
import plan_report
import trade_history

ET = ZoneInfo("America/New_York")
ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_DIR = os.path.join(ROOT, "archive")
BROKER_DIR = os.path.join(ROOT, "data", "broker")

# The daily guard (the "Small but consistent" sheet). Changing these changes the rules.
PROFIT_TARGET = 20.0          # +$20 realized: done for the day
LOSS_LIMIT = -20.0            # -$20 realized: done for the day
MAX_ENTRIES = 4               # buys per day, counted at entry
MAX_CONTRACTS = 2             # contracts per buy
FIRST_ENTRY = game_plan.ENTRY_OPEN     # no entries before 10:00 ET

_TICKER = re.compile(r"^[A-Z][A-Z.]{0,9}$")
_ARCHIVE_NAME = re.compile(r"^([A-Z.]+)_(\d{8})_(\d{4,6})\.json$")
_TAG = re.compile(r"<[^>]+>")


# ── helpers ─────────────────────────────────────────────────────────────────────

def _clean(v: Any) -> Any:
    """JSON-safe copy: NaN/inf become None (missing, not zero); datetimes become ISO."""
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if isinstance(v, dict):
        return {str(k): _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if hasattr(v, "item"):                       # numpy / pandas scalar
        return _clean(v.item())
    return v


def _text(lines: list[str]) -> str:
    """Telegram HTML lines as plain text."""
    return "\n".join(html.unescape(_TAG.sub("", line)) for line in lines)


def _norm_ticker(ticker: str) -> str:
    t = str(ticker or "").strip().upper()
    if not _TICKER.match(t):
        raise ValueError(f"not a ticker: {ticker!r}")
    return t


def _archives(ticker: str, archive_dir: str | None = None) -> list[str]:
    """Scan archives for ``ticker``, newest first (only ``TICKER_YYYYMMDD_HHMM[SS].json``)."""
    folder = archive_dir or ARCHIVE_DIR
    out = [p for p in glob.glob(os.path.join(folder, f"{ticker}_*.json"))
           if (m := _ARCHIVE_NAME.match(os.path.basename(p))) and m.group(1) == ticker]
    return sorted(out, reverse=True)


def _read(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _latest(ticker: str, archive_dir: str | None = None) -> tuple[dict | None, dict | None, str | None]:
    """(newest readable payload, the one before it, newest path)."""
    files = _archives(ticker, archive_dir)
    curr = prev = None
    path = None
    for p in files:
        d = _read(p)
        if d is None:
            continue
        if curr is None:
            curr, path = d, p
        else:
            prev = d
            break
    return curr, prev, path


def _scan_time(payload: dict | None) -> datetime | None:
    try:
        ts = datetime.fromisoformat(str((payload or {}).get("timestamp") or ""))
    except ValueError:
        return None
    return ts.astimezone(ET) if ts.tzinfo is not None else None


def _no_scan(ticker: str) -> dict:
    return {"ticker": ticker, "available": False,
            "reason": f"no readable scan archive for {ticker} in {ARCHIVE_DIR}"}


# ── tools (plain functions; the MCP layer below only registers them) ───────────

def list_tickers(archive_dir: str | None = None) -> dict:
    """Tickers that have scan archives, with the newest scan time of each."""
    folder = archive_dir or ARCHIVE_DIR
    newest: dict[str, str] = {}
    for p in glob.glob(os.path.join(folder, "*.json")):
        m = _ARCHIVE_NAME.match(os.path.basename(p))
        if m:
            stamp = f"{m.group(2)}_{m.group(3)}"
            if stamp > newest.get(m.group(1), ""):
                newest[m.group(1)] = stamp
    return {"tickers": [{"ticker": t, "newest_archive": s} for t, s in sorted(newest.items())]}


def scan_status(ticker: str, *, now: datetime | None = None,
                archive_dir: str | None = None) -> dict:
    """How fresh the newest scan is. Stale after ``ema_stack.STALE_AFTER_MIN`` minutes."""
    t = _norm_ticker(ticker)
    curr, prev, path = _latest(t, archive_dir)
    if curr is None:
        return _no_scan(t)
    now = now or datetime.now(ET)
    ts, prev_ts = _scan_time(curr), _scan_time(prev)
    age = (now - ts).total_seconds() / 60.0 if ts else None
    return _clean({
        "ticker": t, "available": True,
        "scan_time": ts, "age_minutes": round(age, 1) if age is not None else None,
        "stale": (age > ema_stack.STALE_AFTER_MIN) if age is not None else None,
        "stale_after_minutes": ema_stack.STALE_AFTER_MIN,
        "spot": curr.get("spot"), "direction": curr.get("direction"),
        "is_eod": curr.get("is_eod"), "previous_scan_time": prev_ts,
        "file": os.path.basename(path) if path else None,
    })


def game_plan_report(ticker: str, *, now: datetime | None = None,
                     archive_dir: str | None = None) -> dict:
    """The Game Plan the newest scan saved: side, reason, holds, levels, trade plan."""
    t = _norm_ticker(ticker)
    curr, _, path = _latest(t, archive_dir)
    if curr is None:
        return _no_scan(t)
    plan = plan_report.saved_plan(curr, now or datetime.now(ET))
    if plan is None:
        return {"ticker": t, "available": False, "file": os.path.basename(path),
                "reason": "this scan has no saved Game Plan (scans before 2026-10-17)"}
    return _clean({
        "ticker": t, "available": True, "file": os.path.basename(path),
        "side": plan.side, "side_label": game_plan.SIDE_LABEL.get(plan.side, plan.side),
        "stale": plan.stale, "text": _text(game_plan.plan_lines(plan)),
        "plan": game_plan.plan_to_json(plan),
    })


def gamma_report(ticker: str, *, strikes_each_side: int = 6,
                 archive_dir: str | None = None) -> dict:
    """The gamma read the newest scan saved (nearest expiry), with strikes around spot."""
    t = _norm_ticker(ticker)
    curr, _, path = _latest(t, archive_dir)
    if curr is None:
        return _no_scan(t)
    s = plan_report.saved_gamma(curr)
    if not s:
        return {"ticker": t, "available": False, "file": os.path.basename(path),
                "reason": "this scan has no saved gamma read"}
    n = max(1, min(int(strikes_each_side), 25))
    strikes = sorted((s.get("by_strike") or {}).items())
    spot = float(s["spot"])
    below = [kv for kv in strikes if kv[0] <= spot][-n:]
    above = [kv for kv in strikes if kv[0] > spot][:n]
    fields = ("expiry", "spot", "net", "call_wall", "call_wall_gex", "put_wall",
              "put_wall_gex", "support", "support_gex", "resistance", "resistance_gex",
              "first_negative_below_spot", "gamma_flip", "gamma_flip_nearest",
              "net_all_strikes", "net_full_chain", "as_of", "coverage")
    return _clean({
        "ticker": t, "available": True, "file": os.path.basename(path),
        **{k: s.get(k) for k in fields},
        "regime": "positive" if float(s["net"]) >= 0 else "negative",
        "top": [{"strike": k, "gex": v} for k, v in s.get("top") or []],
        "near_spot": [{"strike": k, "gex": v} for k, v in below + above],
        "text": _text(plan_report.gamma_lines(s)),
    })


def _contract(c: dict) -> dict:
    vol, oi = c.get("volume"), c.get("openInterest")
    ok = all(isinstance(x, (int, float)) and math.isfinite(x) for x in (vol, oi))
    return _clean({
        "strike": c.get("strike"), "expiry": c.get("expiry"), "dte": c.get("dte"),
        "last": c.get("lastPrice"), "bid": c.get("bid"), "ask": c.get("ask"),
        "volume": vol, "open_interest": oi,
        "vol_oi": round(vol / oi, 2) if ok and oi > 0 else None,
        "iv": c.get("impliedVolatility"),
    })


def magnets_report(ticker: str, *, top_n: int = 5, archive_dir: str | None = None) -> dict:
    """Top calls/puts by volume, the call/put magnets and what changed since the last scan."""
    t = _norm_ticker(ticker)
    curr, prev, path = _latest(t, archive_dir)
    if curr is None:
        return _no_scan(t)
    n = max(1, min(int(top_n), 20))
    vol = curr.get("volume") or {}
    mags = curr.get("signal_magnets") or {}
    changes: list[str] = []
    if prev:
        pv, pm = prev.get("volume") or {}, prev.get("signal_magnets") or {}
        for label, a, b in (("spot", prev.get("spot"), curr.get("spot")),
                            ("P/C", pv.get("pc_ratio"), vol.get("pc_ratio"))):
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                changes.append(f"{label} {a:.2f} -> {b:.2f} ({b - a:+.2f})")
        for side in ("call", "put"):
            a, b = (pm.get(side) or {}).get("strike"), (mags.get(side) or {}).get("strike")
            if a is not None and b is not None and a != b:
                changes.append(f"{side} magnet ${a:g} -> ${b:g} (strike change)")
    return _clean({
        "ticker": t, "available": True, "file": os.path.basename(path),
        "scan_time": _scan_time(curr), "previous_scan_time": _scan_time(prev),
        "spot": curr.get("spot"), "pc_ratio": vol.get("pc_ratio"),
        "total_call_volume": vol.get("total_call_vol"),
        "total_put_volume": vol.get("total_put_vol"),
        "call_magnet": mags.get("call"), "put_magnet": mags.get("put"),
        "top_calls": [_contract(c) for c in (vol.get("top_calls") or [])[:n]],
        "top_puts": [_contract(c) for c in (vol.get("top_puts") or [])[:n]],
        "changes_vs_previous_scan": changes,
    })


def _latest_export(broker_dir: str | None) -> str | None:
    folder = broker_dir or BROKER_DIR
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".csv")]
    except OSError:
        return None
    return max(files, key=os.path.getmtime) if files else None


def _day_check(acts, trades, day: date) -> dict:
    """Rules for one day from option trades in the export (times are as exported, ET)."""
    a = acts[acts["activity_type"] == "Trade"].copy()
    a = a[a["symbol"].map(lambda s: trade_history.parse_occ(s) is not None)]
    a["ts"] = pd.to_datetime(a["effective_date"].astype(str) + " "
                             + a["effective_time"].astype(str), errors="coerce")
    a = a[a["ts"].dt.date == day].sort_values("ts", kind="stable")
    buys = a[a["quantity"] > 0]

    closed = trades[trades["exit"].dt.date == day].sort_values("exit") if len(trades) else trades
    pnl = float(closed["pnl"].sum()) if len(closed) else 0.0

    broken: list[str] = []
    early = [b for b in buys["ts"] if b.time() < FIRST_ENTRY]
    if early:
        broken.append(f"{len(early)} entr{'y' if len(early) == 1 else 'ies'} before "
                      f"{FIRST_ENTRY:%H:%M}")
    if len(buys) > MAX_ENTRIES:
        broken.append(f"{len(buys)} entries (limit {MAX_ENTRIES})")
    big = buys[buys["quantity"] > MAX_CONTRACTS]
    if len(big):
        broken.append(f"{len(big)} buy(s) over {MAX_CONTRACTS} contracts "
                      f"(max {int(big['quantity'].max())})")

    # Adding to a position that is losing: a buy of a symbol already held, at a lower price.
    held: dict[str, list[float]] = {}
    adds = 0
    for _, r in a.iterrows():
        q, px = float(r["quantity"]), abs(float(r["net_cash_amount"])) / abs(float(r["quantity"]))
        lots = held.setdefault(r["symbol"], [])
        if q > 0:
            if lots and px < min(lots):
                adds += 1
            lots.extend([px] * int(q))
        else:
            del lots[:int(-q)]
    if adds:
        broken.append(f"{adds} add(s) to a losing position")

    # Entries after the day was done: once realized P&L touches +$20 or -$20 the day is
    # over, even if later trades bring it back inside the band.
    after = 0
    limit_hit_at = None
    running = 0.0
    for _, c in closed.iterrows() if len(closed) else []:
        running += float(c["pnl"])
        if running >= PROFIT_TARGET or running <= LOSS_LIMIT:
            limit_hit_at = c["exit"]
            break
    if limit_hit_at is not None:
        after = int((buys["ts"] > limit_hit_at).sum())
    if after:
        broken.append(f"{after} entr{'y' if after == 1 else 'ies'} after the "
                      f"+${PROFIT_TARGET:g}/-${-LOSS_LIMIT:g} limit")

    done = pnl >= PROFIT_TARGET or pnl <= LOSS_LIMIT or len(buys) >= MAX_ENTRIES
    return {"date": day.isoformat(), "entries": int(len(buys)),
            "max_contracts": int(buys["quantity"].max()) if len(buys) else 0,
            "realized_pnl": round(pnl, 2), "closed_trades": int(len(closed)),
            "rules_followed": not broken, "rules_broken": broken,
            "status": "DONE" if done or limit_hit_at is not None
                      else ("NO TRADES" if not len(buys) else "OPEN"),
            "limit_hit_at": limit_hit_at.strftime("%H:%M") if limit_hit_at is not None else None,
            "first_entry": buys["ts"].iloc[0].strftime("%H:%M") if len(buys) else None,
            "last_entry": buys["ts"].iloc[-1].strftime("%H:%M") if len(buys) else None}


def daily_guard(day: str | None = None, *, broker_dir: str | None = None,
                now: datetime | None = None) -> dict:
    """Today's entries, realized P&L against +$20/-$20, rules broken, and the streak of
    trading days with every rule followed, from the newest broker export."""
    path = _latest_export(broker_dir)
    if path is None:
        return {"available": False, "reason": f"no broker export (.csv) in {broker_dir or BROKER_DIR}"}
    try:
        acts = trade_history.load_activities(path)
    except (OSError, ValueError) as exc:
        return {"available": False, "file": os.path.basename(path), "reason": str(exc)}
    trades, notes = trade_history.closed_trades(acts)
    now = now or datetime.now(ET)
    target = date.fromisoformat(day) if day else now.date()
    exported = datetime.fromtimestamp(os.path.getmtime(path), ET)

    today = _day_check(acts, trades, target)
    dates = pd.to_datetime(acts.loc[acts["activity_type"] == "Trade", "effective_date"],
                           errors="coerce").dropna()
    trade_days = sorted({d.date() for d in dates}, reverse=True)
    streak = 0
    for d in trade_days:
        if d > target:
            continue
        if not _day_check(acts, trades, d)["rules_followed"]:
            break
        streak += 1

    if today["status"] == "DONE":
        verdict = "DONE for today: no new entries."
    elif today["status"] == "NO TRADES":
        verdict = "No trades yet today."
    else:
        verdict = (f"{MAX_ENTRIES - today['entries']} entr"
                   f"{'y' if MAX_ENTRIES - today['entries'] == 1 else 'ies'} left; "
                   f"done at +${PROFIT_TARGET:g} or -${-LOSS_LIMIT:g}.")
    return _clean({
        "available": True, "file": os.path.basename(path), "export_time": exported,
        "note": "Only trades in the export count: download a fresh export for the latest.",
        "rules": {"profit_target": PROFIT_TARGET, "loss_limit": LOSS_LIMIT,
                  "max_entries": MAX_ENTRIES, "max_contracts": MAX_CONTRACTS,
                  "first_entry": FIRST_ENTRY.strftime("%H:%M")},
        **today, "verdict": verdict, "streak_days": streak,
        "open_contracts_in_export": notes.get("open_contracts"),
    })


def summary_report(ticker: str, *, now: datetime | None = None,
                   archive_dir: str | None = None, broker_dir: str | None = None) -> dict:
    """Everything at once: scan status, Game Plan, gamma, magnets and the daily guard."""
    t = _norm_ticker(ticker)
    return {"status": scan_status(t, now=now, archive_dir=archive_dir),
            "game_plan": game_plan_report(t, now=now, archive_dir=archive_dir),
            "gamma": gamma_report(t, archive_dir=archive_dir),
            "magnets": magnets_report(t, archive_dir=archive_dir),
            "daily_guard": daily_guard(broker_dir=broker_dir, now=now)}


# ── MCP registration ─────────────────────────────────────────────────────────────

def build_server():
    """The FastMCP server with every tool registered. Imports ``mcp`` only here."""
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("dailyscaner")

    @server.tool(name="list_tickers")
    def _list_tickers() -> dict:
        """Tickers dailyScaner has scans for, with each one's newest archive."""
        return list_tickers()

    @server.tool(name="scan_status")
    def _scan_status(ticker: str = "AAPL") -> dict:
        """When the newest scan ran, how old it is and whether it is stale (over 30 min)."""
        return scan_status(ticker)

    @server.tool(name="game_plan")
    def _game_plan(ticker: str = "AAPL") -> dict:
        """The Game Plan the newest scan saved: allowed side (calls only / puts only /
        stand aside) and why, holds and conflicts, levels, entry, stop and targets."""
        return game_plan_report(ticker)

    @server.tool(name="gamma")
    def _gamma(ticker: str = "AAPL", strikes_each_side: int = 6) -> dict:
        """The gamma read the newest scan saved for the nearest expiry: net gamma and
        regime, call/put walls, support/resistance, flip, largest strikes, strikes near spot."""
        return gamma_report(ticker, strikes_each_side=strikes_each_side)

    @server.tool(name="magnets")
    def _magnets(ticker: str = "AAPL", top_n: int = 5) -> dict:
        """Top calls and puts by volume with vol/OI, the call and put magnets, and what
        changed since the previous scan (spot, P/C, magnet strike changes)."""
        return magnets_report(ticker, top_n=top_n)

    @server.tool(name="daily_guard")
    def _daily_guard(day: str | None = None) -> dict:
        """The trader's daily rules from the newest Wealthsimple export: entries (max 4),
        realized P&L (done at +$20 or -$20), contracts per buy (max 2), no entries before
        10:00, no adding to a loser, streak of rule-following days. Check this before
        discussing any new trade; if status is DONE, the answer is done for today."""
        return daily_guard(day)

    @server.tool(name="summary")
    def _summary(ticker: str = "AAPL") -> dict:
        """Scan status, Game Plan, gamma, magnets and the daily guard in one call."""
        return summary_report(ticker)

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
