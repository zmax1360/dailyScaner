"""contract_view — one option contract over time: price, IV, delta, gamma, and what
moved its price between two moments (spot, time, implied volatility).

Reads the scanner's recorded snapshots (``data/volume_history.db``: a contract is
recorded on a scan only when it has traded that day, at most every 15 minutes) and
each scan's stock price from its archive (``archive/<scan_id>.json`` "spot").
Display only; not a scoring input. Missing data is NaN and left out, never defaulted.

Price is the bid/ask mid when both sides are quoted, else the last trade (flagged).
IV is solved from that price at the scan's spot with the exact time to the 16:00 ET
close (``gex.years_to_expiry``); when it cannot be solved the vendor IV is used
(flagged). Greeks are Black-Scholes at that IV.

Attribution walks the snapshots in order. For each step from A to B, re-pricing with
Black-Scholes one input at a time:
    time    = BS(S_A, iv_A, T_B) - BS(S_A, iv_A, T_A)
    IV      = BS(S_A, iv_B, T_B) - BS(S_A, iv_A, T_B)
    spot    = BS(S_B, iv_B, T_B) - BS(S_A, iv_B, T_B), split into
              delta = delta(S_A, iv_B, T_B) x (S_B - S_A) and gamma = spot - delta
    residual = actual price change - (time + IV + spot)
The steps are summed. With IV solved from each price the residual is near zero; it
grows where IV came from the vendor or a price is a stale last trade.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any, Callable

import pandas as pd

import gex
from config import SCORING
from greeks import bs_delta, bs_price, implied_vol
from zero_dte_gex import bs_gamma

_BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE, "data", "volume_history.db")
ARCHIVE_DIR = os.path.join(_BASE, "archive")
YEAR_DAYS = 365.0
COLS = ["ts", "scan_id", "spot", "bid", "ask", "last", "price", "price_from",
        "volume", "open_interest", "iv", "iv_from", "delta", "gamma", "theta", "vega"]
TERMS = ["delta", "gamma", "theta", "vega", "residual"]


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def archive_spot(scan_id: str, archive_dir: str = ARCHIVE_DIR) -> float | None:
    """The stock price a scan recorded, or None (missing or unreadable archive)."""
    try:
        with open(os.path.join(archive_dir, f"{scan_id}.json")) as fh:
            v = _f(json.load(fh).get("spot"))
    except (OSError, ValueError, AttributeError):
        return None
    return v if v and v > 0 else None


def contracts(ticker: str, *, db_path: str | None = None) -> pd.DataFrame:
    """Every recorded contract: side, strike, expiry, snapshots, last seen."""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return pd.DataFrame(columns=["side", "strike", "expiry", "snapshots", "last_ts"])
    with closing(sqlite3.connect(path, timeout=30)) as con:
        return pd.read_sql_query(
            "SELECT side, strike, expiry, COUNT(*) AS snapshots, MAX(ts_et) AS last_ts"
            " FROM contract_scans WHERE ticker=? GROUP BY side, strike, expiry",
            con, params=(ticker.upper(),))


def raw_history(ticker: str, side: str, strike: float, expiry: str, *,
                db_path: str | None = None) -> pd.DataFrame:
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return pd.DataFrame()
    with closing(sqlite3.connect(path, timeout=30)) as con:
        return pd.read_sql_query(
            "SELECT ts_et, scan_id, volume, open_interest, bid, ask, last, iv"
            " FROM contract_scans WHERE ticker=? AND side=? AND strike=? AND expiry=?"
            " ORDER BY ts_et",
            con, params=(ticker.upper(), side.upper(), float(strike), str(expiry)[:10]))


def _price(bid: Any, ask: Any, last: Any) -> tuple[float | None, str]:
    b, a, lt = _f(bid), _f(ask), _f(last)
    if b is not None and a is not None and b > 0 and a >= b:
        return 0.5 * (b + a), "mid"
    if lt is not None and lt > 0:
        return lt, "last"
    return None, ""


def _greeks(side: str, spot: float, strike: float, days: float, iv: float, r: float):
    d = bs_delta(side, spot, strike, days, iv, r=r)
    g = bs_gamma(spot, strike, iv, t_years=days / YEAR_DAYS, r=r)
    p0 = bs_price(side, spot, strike, days, iv, r=r)
    one_day = min(1.0, days * 0.999)
    p_t = bs_price(side, spot, strike, days - one_day, iv, r=r) if days > one_day else None
    p_v = bs_price(side, spot, strike, days, iv + 0.01, r=r)
    theta = None if p0 is None or p_t is None else (p_t - p0) / one_day * 1.0
    vega = None if p0 is None or p_v is None else p_v - p0
    return d, g, theta, vega


def history(raw: pd.DataFrame, *, side: str, strike: float, expiry: str,
            spot_of: Callable[[str], float | None]) -> pd.DataFrame:
    """Per snapshot: spot, price, IV and greeks. Rows after expiry are dropped.
    ``theta`` is $ per share per calendar day; ``vega`` $ per share per 1 IV point."""
    if raw is None or raw.empty:
        return pd.DataFrame(columns=COLS)
    r = float(SCORING.get("risk_free_rate", 0.045))
    min_iv = float(SCORING.get("min_iv_usable", 0.01))
    rows = []
    for rec in raw.to_dict(orient="records"):
        ts = datetime.fromisoformat(str(rec["ts_et"]))
        t = gex.years_to_expiry(expiry, ts)
        if t is None:
            continue
        days = t * YEAR_DAYS
        spot = spot_of(str(rec["scan_id"]))
        price, price_from = _price(rec.get("bid"), rec.get("ask"), rec.get("last"))
        iv, iv_from = None, ""
        if spot is not None and price is not None:
            iv = implied_vol(side, spot, strike, days, price, r=r)
            iv_from = "solved" if iv is not None else ""
        if iv is None:
            v = _f(rec.get("iv"))
            if v is not None and v >= min_iv:
                iv, iv_from = v, "vendor"
        d = g = th = vg = None
        if spot is not None and iv is not None:
            d, g, th, vg = _greeks(side, spot, strike, days, iv, r)
        nan = float("nan")
        rows.append({
            "ts": ts, "scan_id": rec["scan_id"], "spot": nan if spot is None else spot,
            "bid": _f(rec.get("bid")) or nan, "ask": _f(rec.get("ask")) or nan,
            "last": _f(rec.get("last")) or nan,
            "price": nan if price is None else price, "price_from": price_from,
            "volume": _f(rec.get("volume")) if _f(rec.get("volume")) is not None else nan,
            "open_interest": _f(rec.get("open_interest")) if _f(rec.get("open_interest")) is not None else nan,
            "iv": nan if iv is None else iv, "iv_from": iv_from,
            "delta": nan if d is None else d, "gamma": nan if g is None else g,
            "theta": nan if th is None else th, "vega": nan if vg is None else vg,
            "_days": days,
        })
    out = pd.DataFrame(rows)
    return out


def attribution(hist: pd.DataFrame, *, side: str, strike: float,
                start: Any = None, end: Any = None) -> dict | None:
    """Price change from ``start`` to ``end`` (timestamps; default first/last usable
    snapshot) split into delta, gamma, theta (time), vega (IV) and residual, $ per share.
    None when fewer than two usable snapshots fall in the window."""
    if hist is None or hist.empty:
        return None
    h = hist.dropna(subset=["spot", "price", "iv"])
    if start is not None:
        h = h[h["ts"] >= start]
    if end is not None:
        h = h[h["ts"] <= end]
    if len(h) < 2:
        return None
    r = float(SCORING.get("risk_free_rate", 0.045))
    terms = {k: 0.0 for k in TERMS}
    rows = h.to_dict(orient="records")
    for a, b in zip(rows[:-1], rows[1:]):
        sa, sb, va, vb, ta, tb = a["spot"], b["spot"], a["iv"], b["iv"], a["_days"], b["_days"]
        p_aa = bs_price(side, sa, strike, ta, va, r=r)
        p_time = bs_price(side, sa, strike, tb, va, r=r)
        p_iv = bs_price(side, sa, strike, tb, vb, r=r)
        p_spot = bs_price(side, sb, strike, tb, vb, r=r)
        dl = bs_delta(side, sa, strike, tb, vb, r=r)
        actual = b["price"] - a["price"]
        if None in (p_aa, p_time, p_iv, p_spot, dl):
            terms["residual"] += actual           # unexplained step: all of it is residual
            continue
        theta, vega, spot_move = p_time - p_aa, p_iv - p_time, p_spot - p_iv
        delta = dl * (sb - sa)
        terms["theta"] += theta
        terms["vega"] += vega
        terms["delta"] += delta
        terms["gamma"] += spot_move - delta
        terms["residual"] += actual - (theta + vega + spot_move)
    first, last = rows[0], rows[-1]
    return {"start_ts": first["ts"], "end_ts": last["ts"], "start_price": first["price"],
            "end_price": last["price"], "change": last["price"] - first["price"],
            "steps": len(rows) - 1, **terms}
