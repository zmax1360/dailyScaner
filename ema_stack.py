"""ema_stack — 15-minute EMA 9/21/50 trend rule (display only; never affects scoring).

Rule (owner's trading rule, 2026-09-27):
    EMA 9 > EMA 21 > EMA 50  → bullish: calls only
    EMA 9 < EMA 21 < EMA 50  → bearish: puts only
    anything else            → no trade

Missing or insufficient data is reported as UNKNOWN, never guessed.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

import pandas as pd

TIMEFRAME = "15M"
PERIODS = (9, 21, 50)
STALE_AFTER_MIN = 30

BULL = "BULL"
BEAR = "BEAR"
NO_TRADE = "NO_TRADE"
UNKNOWN = "UNKNOWN"


def compute_emas(close: pd.Series) -> dict[str, float | None]:
    """EMA 9/21/50 of the last bar. None when there are fewer bars than the period."""
    close = pd.to_numeric(close, errors="coerce").dropna()
    out: dict[str, float | None] = {}
    for p in PERIODS:
        if len(close) < p:
            out[f"ema{p}"] = None
            continue
        val = float(close.ewm(span=p, adjust=False).mean().iloc[-1])
        out[f"ema{p}"] = round(val, 4) if math.isfinite(val) else None
    return out


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def classify(ema9: Any, ema21: Any, ema50: Any) -> dict[str, Any]:
    """Return {state, allowed, headline, reason}. Ties count as no trade."""
    e9, e21, e50 = _num(ema9), _num(ema21), _num(ema50)
    vals = f"EMA 9 {_fmt(e9)} · EMA 21 {_fmt(e21)} · EMA 50 {_fmt(e50)}"
    if None in (e9, e21, e50):
        missing = [n for n, v in (("9", e9), ("21", e21), ("50", e50)) if v is None]
        return {
            "state": UNKNOWN, "allowed": None,
            "headline": "EMA trend unknown",
            "reason": f"15-min EMA {', '.join(missing)} not available in the latest scan "
                      f"(not enough bars, or the scan predates this rule). {vals}",
        }
    if e9 > e21 > e50:
        return {
            "state": BULL, "allowed": "CALL",
            "headline": "Bullish — calls only",
            "reason": f"15-min EMAs stacked up: 9 above 21 above 50. {vals}",
        }
    if e9 < e21 < e50:
        return {
            "state": BEAR, "allowed": "PUT",
            "headline": "Bearish — puts only",
            "reason": f"15-min EMAs stacked down: 9 below 21 below 50. {vals}",
        }
    return {
        "state": NO_TRADE, "allowed": None,
        "headline": "No trade — EMAs not stacked",
        "reason": f"15-min EMAs are mixed: {_why_mixed(e9, e21, e50)}. {vals}",
    }


def _why_mixed(e9: float, e21: float, e50: float) -> str:
    rel = lambda a, b: "above" if a > b else ("below" if a < b else "equal to")
    return f"9 is {rel(e9, e21)} 21, and 21 is {rel(e21, e50)} 50"


def _fmt(v: float | None) -> str:
    return "n/a" if v is None else f"{v:.2f}"


def banner_for_archive(payload: dict | None, *, now: datetime | None = None) -> dict[str, Any]:
    """Banner content for the latest scan archive: classify() plus scan time and staleness."""
    tf = ((payload or {}).get("timeframes") or {}).get(TIMEFRAME) or {}
    out = classify(tf.get("ema9"), tf.get("ema21"), tf.get("ema50"))
    ts_raw = (payload or {}).get("timestamp")
    out["as_of"] = None
    out["stale"] = False
    if ts_raw:
        try:
            ts = datetime.fromisoformat(str(ts_raw))
        except ValueError:
            ts = None
        if ts is not None and ts.tzinfo is not None:
            out["as_of"] = ts
            if now is not None:
                out["stale"] = (now - ts).total_seconds() > STALE_AFTER_MIN * 60
    return out
