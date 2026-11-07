"""rules — the rule library. Pure functions: a market context in, a signal (or None) out.

Context (``market.Context``): ``bars`` 15-minute bars (Close, VWAP; index = bar START,
naive ET) holding only bars complete at ``now``; ``first5`` the day's first 5-minute bar
(Open, High, Low, Close) or None; ``rvol`` that bar's volume / its 14-day average;
``orange`` today's opening range (``opening_range.summarize``) or None.

A signal is ``{"side": "CALL"|"PUT", "reason": str, "stop_spot": float|None}``.
Every rule sees only the newest complete bar inside its window; the recorder asks at each
scan and takes the first signal of the day (one entry per ticker per day).
"""

from __future__ import annotations

from datetime import time, timedelta
from typing import Any, Callable

import pandas as pd

CALL, PUT = "CALL", "PUT"


def parse_window(text: str | None, default: str) -> tuple[time, time]:
    a, b = (text or default).split("-")
    h1, m1 = (int(x) for x in a.strip().split(":"))
    h2, m2 = (int(x) for x in b.strip().split(":"))
    return time(h1, m1), time(h2, m2)


def _last_bar(ctx, window: tuple[time, time]):
    """Newest complete 15-min bar whose CLOSE time lies inside ``window``, else None."""
    bars = ctx.bars
    if bars is None or bars.empty:
        return None
    end = (pd.Timestamp(bars.index[-1]) + timedelta(minutes=15)).time()
    if not (window[0] <= end <= window[1]):
        return None
    return bars.iloc[-1]


def _ema(ctx, n: int) -> float | None:
    c = ctx.bars["Close"] if ctx.bars is not None else pd.Series(dtype=float)
    if len(c) < n:
        return None
    return float(c.ewm(span=n, adjust=False).mean().iloc[-1])


def ema_stack(ctx, *, fast=9, mid=21, slow=50, gap=0.0, window="11:00-12:30", **_):
    """Follow the stack: fast > mid > slow calls, fast < mid < slow puts (each gap > ``gap``)."""
    if _last_bar(ctx, parse_window(window, "11:00-12:30")) is None:
        return None
    a, b, c = _ema(ctx, fast), _ema(ctx, mid), _ema(ctx, slow)
    if None in (a, b, c):
        return None
    txt = f"EMA {fast}/{mid}/{slow} {a:.2f}/{b:.2f}/{c:.2f}"
    if a - b > gap and b - c > gap:
        return {"side": CALL, "reason": f"stacked up: {txt}", "stop_spot": None}
    if b - a > gap and c - b > gap:
        return {"side": PUT, "reason": f"stacked down: {txt}", "stop_spot": None}
    return None


def fade_midday_stack(ctx, *, fast=9, mid=21, slow=50, gap=0.0, window="11:00-12:30", **_):
    """The opposite of ``ema_stack``: AAPL's midday stack has tended to reverse."""
    s = ema_stack(ctx, fast=fast, mid=mid, slow=slow, gap=gap, window=window)
    if not s:
        return None
    side = PUT if s["side"] == CALL else CALL
    return {"side": side, "reason": f"fade: {s['reason']}", "stop_spot": None}


def ema_pair(ctx, *, fast=9, slow=89, window="11:00-12:30", **_):
    """Follow fast vs slow EMA when price is on the same side of the slow EMA."""
    bar = _last_bar(ctx, parse_window(window, "11:00-12:30"))
    if bar is None:
        return None
    a, c = _ema(ctx, fast), _ema(ctx, slow)
    if None in (a, c):
        return None
    px = float(bar["Close"])
    txt = f"EMA {fast} {a:.2f} vs EMA {slow} {c:.2f}, close {px:.2f}"
    if a > c and px > c:
        return {"side": CALL, "reason": f"above: {txt}", "stop_spot": None}
    if a < c and px < c:
        return {"side": PUT, "reason": f"below: {txt}", "stop_spot": None}
    return None


def vwap_first_side(ctx, *, buffer=0.05, window="10:00-12:30", **_):
    """The side of VWAP the newest 15-min close is on (by more than ``buffer``)."""
    bar = _last_bar(ctx, parse_window(window, "10:00-12:30"))
    if bar is None or pd.isna(bar.get("VWAP")):
        return None
    px, vw = float(bar["Close"]), float(bar["VWAP"])
    if px > vw + buffer:
        return {"side": CALL, "reason": f"close {px:.2f} above VWAP {vw:.2f}", "stop_spot": None}
    if px < vw - buffer:
        return {"side": PUT, "reason": f"close {px:.2f} below VWAP {vw:.2f}", "stop_spot": None}
    return None


def orb_in_play(ctx, *, rvol_min=1.5, window="09:35-10:30", **_):
    """5-minute opening range breakout, only when the first bar's volume is at least
    ``rvol_min`` x its 14-day average. Direction of the first candle; stop at its far end."""
    lo, hi = parse_window(window, "09:35-10:30")
    if not (lo <= ctx.now.time() <= hi):
        return None
    f = ctx.first5
    if f is None or ctx.rvol is None or ctx.rvol < rvol_min:
        return None
    o, c = float(f["Open"]), float(f["Close"])
    if c > o:
        return {"side": CALL, "reason": f"first 5-min candle up {o:.2f}->{c:.2f}, "
                f"volume {ctx.rvol:.1f}x normal", "stop_spot": float(f["Low"])}
    if c < o:
        return {"side": PUT, "reason": f"first 5-min candle down {o:.2f}->{c:.2f}, "
                f"volume {ctx.rvol:.1f}x normal", "stop_spot": float(f["High"])}
    return None


def orb_20(ctx, *, minutes=20, max_age=10, allow_wide=False, skip_against_sneaky=True, **_):
    """Opening range breakout on the first ``minutes`` (``opening_range``): the first
    5-minute close outside the range, in its direction, only while that break is at most
    ``max_age`` minutes old (a late scan does not chase it) and, unless ``allow_wide``,
    only when the range is not wide against recent days and, unless
    ``skip_against_sneaky`` is off, not against the first candle after a sneaky second
    candle (``opening_range.against_sneaky``). Stop at the range's far side."""
    from ticker_rules import opening_range
    s = getattr(ctx, "orange", None)
    if not s or s.get("minutes") != minutes or s["status"] not in ("break_up", "break_down"):
        return None
    if s.get("wide") and not allow_wide:
        return None
    if skip_against_sneaky and opening_range.against_sneaky(s):
        return None
    h, m = (int(x) for x in s["break_at"].split(":"))
    age = (ctx.now.hour * 60 + ctx.now.minute) - (h * 60 + m)
    if age < 0 or age > max_age:
        return None
    width = f"{s['rel']:.1f}x normal width" if s.get("rel") is not None else "width n/a"
    if s["status"] == "break_up":
        return {"side": CALL, "reason": f"5-min close {s['break_close']:.2f} above the "
                f"{minutes}-min high {s['high']:.2f} ({width})", "stop_spot": s["low"]}
    return {"side": PUT, "reason": f"5-min close {s['break_close']:.2f} below the "
            f"{minutes}-min low {s['low']:.2f} ({width})", "stop_spot": s["high"]}


# name -> (function, default params, label, which slot it belongs in)
RULES: dict[str, dict[str, Any]] = {
    "ema_stack": {"fn": ema_stack, "label": "Follow EMA stack",
                  "defaults": {"fast": 9, "mid": 21, "slow": 50, "gap": 0.05,
                               "window": "11:00-12:30"}, "slots": ("normal",)},
    "fade_midday_stack": {"fn": fade_midday_stack, "label": "Fade midday EMA stack",
                          "defaults": {"fast": 9, "mid": 21, "slow": 50, "gap": 0.0,
                                       "window": "11:00-12:30"}, "slots": ("normal",)},
    "ema_pair": {"fn": ema_pair, "label": "Follow EMA pair",
                 "defaults": {"fast": 9, "slow": 89, "window": "11:00-12:30"},
                 "slots": ("normal",)},
    "vwap_first_side": {"fn": vwap_first_side, "label": "First VWAP side",
                        "defaults": {"buffer": 0.05, "window": "10:00-12:30"},
                        "slots": ("normal",)},
    "orb_in_play": {"fn": orb_in_play, "label": "Opening range breakout (in play)",
                    "defaults": {"rvol_min": 1.5, "window": "09:35-10:30"},
                    "slots": ("in_play",)},
    "orb_20": {"fn": orb_20, "label": "20-min opening range breakout (skip wide opens)",
               "defaults": {"minutes": 20, "max_age": 10, "allow_wide": False,
                            "skip_against_sneaky": True},
               "slots": ("normal", "in_play")},
}


def evaluate(rule: str, params: dict | None, ctx) -> dict | None:
    spec = RULES.get(rule)
    if spec is None:
        return None
    p = dict(spec["defaults"])
    p.update(params or {})
    fn: Callable = spec["fn"]
    return fn(ctx, **p)
