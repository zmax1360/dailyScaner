"""htf_trend — the 1-hour and 4-hour trend, for context (display only; never scoring).

For each timeframe:
    up     EMA 21 above EMA 50, and the last swing low is higher than the one before
    down   EMA 21 below EMA 50, and the last swing high is lower than the one before
    range  anything else (the EMAs and the swings disagree, or the swings are flat)

A swing low is a bar whose low is the lowest of the SWING_BARS bars on each side of it
(a swing high mirrors it), so the newest SWING_BARS bars can never be one. Too few bars
gives "unknown", never a guess.

The Game Plan shows it as one line: the higher timeframes, and whether the allowed side
runs with them or against them. It does not block a trade or move a target. It is saved
with every scan so it can be tested against what price did next before it does more.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

TIMEFRAMES = ("1H", "4H")
SWING_BARS = 2
MIN_BARS = 55                 # EMA 50 plus a little warm-up

UP, DOWN, RANGE, UNKNOWN = "up", "down", "range", "unknown"


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def swing_points(df: pd.DataFrame, k: int = SWING_BARS) -> tuple[list[float], list[float]]:
    """(swing lows, swing highs), oldest first."""
    lows = pd.to_numeric(df["Low"], errors="coerce").tolist()
    highs = pd.to_numeric(df["High"], errors="coerce").tolist()
    sl, sh = [], []
    for i in range(k, len(df) - k):
        lo_win = lows[i - k:i + k + 1]
        hi_win = highs[i - k:i + k + 1]
        if any(x != x for x in lo_win + hi_win):          # NaN in the window
            continue
        if lows[i] == min(lo_win) and lo_win.count(lows[i]) == 1:
            sl.append(float(lows[i]))
        if highs[i] == max(hi_win) and hi_win.count(highs[i]) == 1:
            sh.append(float(highs[i]))
    return sl, sh


def describe(df: pd.DataFrame | None) -> dict[str, Any]:
    """The archive fields for one timeframe: trend, EMA 21 and 50, the last two swings."""
    out: dict[str, Any] = {"trend": UNKNOWN, "ema21": None, "ema50": None,
                           "swing_low": None, "prev_swing_low": None,
                           "swing_high": None, "prev_swing_high": None}
    if df is None or len(df) < MIN_BARS or not {"High", "Low", "Close"} <= set(df.columns):
        return out
    close = pd.to_numeric(df["Close"], errors="coerce").dropna()
    if len(close) < MIN_BARS:
        return out
    e21 = float(close.ewm(span=21, adjust=False).mean().iloc[-1])
    e50 = float(close.ewm(span=50, adjust=False).mean().iloc[-1])
    sl, sh = swing_points(df)
    out.update(ema21=round(e21, 4), ema50=round(e50, 4))
    if len(sl) >= 2:
        out.update(swing_low=round(sl[-1], 4), prev_swing_low=round(sl[-2], 4))
    if len(sh) >= 2:
        out.update(swing_high=round(sh[-1], 4), prev_swing_high=round(sh[-2], 4))
    out["trend"] = classify(out)
    return out


def classify(d: dict[str, Any] | None) -> str:
    d = d or {}
    e21, e50 = _num(d.get("ema21")), _num(d.get("ema50"))
    if e21 is None or e50 is None:
        return UNKNOWN
    lo, plo = _num(d.get("swing_low")), _num(d.get("prev_swing_low"))
    hi, phi = _num(d.get("swing_high")), _num(d.get("prev_swing_high"))
    if e21 > e50 and lo is not None and plo is not None and lo > plo:
        return UP
    if e21 < e50 and hi is not None and phi is not None and hi < phi:
        return DOWN
    return RANGE


def from_archive(payload: dict | None) -> dict[str, str]:
    """{"1H": trend, "4H": trend} from a saved scan. Older scans give "unknown"."""
    tfs = (payload or {}).get("timeframes") or {}
    return {tf: (classify(tfs.get(tf)) if tfs.get(tf) else UNKNOWN) for tf in TIMEFRAMES}


def plan_line(side: str | None, trends: dict[str, str] | None) -> str:
    """One sentence for the Game Plan. ``side`` is "CALLS", "PUTS" or anything else (no
    side allowed, or a hold): then only the trends are named."""
    t = {tf: (trends or {}).get(tf, UNKNOWN) for tf in TIMEFRAMES}
    head = "Higher timeframes: " + " · ".join(f"{tf} {t[tf]}" for tf in TIMEFRAMES) + "."
    if side not in ("CALLS", "PUTS") or all(v == UNKNOWN for v in t.values()):
        return head
    want = UP if side == "CALLS" else DOWN
    against = DOWN if side == "CALLS" else UP
    word = "calls" if side == "CALLS" else "puts"
    known = [v for v in t.values() if v != UNKNOWN]
    if all(v == want for v in known):
        return head + f" The {word} plan runs with them."
    if any(v == against for v in known):
        return (head + f" The {word} plan runs against the higher trend: treat it as a "
                "pullback trade, take target 1 and do not hold for the far targets.")
    return head + " The higher timeframes are ranging: expect the range edges to hold."
