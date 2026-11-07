"""opening_range — the day's first N minutes (default 20: 09:30-09:50 ET) as a range,
its width against recent days, and the first 5-minute close outside it.

From a 2026-11-06 replay of 60 AAPL sessions (Aug 14 - Nov 6, Yahoo 5-minute bars): a
plain breakout was close to a coin flip, but two things held for every range length
tested (5/10/15/20 minutes):

  * a WIDE opening range (more than ``WIDE`` x the average of the previous ``LOOKBACK``
    sessions) broke out and failed: 15-36% winners, against 62-82% for a normal range;
  * the 20-minute range had the fewest fake-outs (13% vs 25% for 5 minutes) and the best
    30 minutes after the break (64% up in the break's direction).

60 sessions is a small sample, so this is shown and recorded as paper only.

Bars are 5-minute OHLC, regular session, index = bar START in naive ET wall-clock time
(``market.build`` hands them over that way), complete at ``now`` only.
"""

from __future__ import annotations

import html
from datetime import datetime, time, timedelta
from typing import Any

import pandas as pd

MINUTES = 20
LOOKBACK = 10            # previous sessions the width is compared with
MIN_HISTORY = 5          # fewer complete past ranges: no width verdict
WIDE = 1.2               # width / average above this: breakouts tend to fail
CUTOFF = time(11, 30)    # a first close outside after this is not a breakout
OPEN = time(9, 30)

FORMING, SET, BREAK_UP, BREAK_DOWN, NO_BREAK = (
    "forming", "set", "break_up", "break_down", "no_break")


def _clock(minutes_after_open: int) -> time:
    """Wall-clock time ``minutes_after_open`` minutes after 09:30."""
    m = OPEN.hour * 60 + OPEN.minute + minutes_after_open
    return time(m // 60, m % 60)


def _ranges(five: pd.DataFrame, minutes: int) -> pd.DataFrame:
    """One row per session whose first ``minutes`` are all present: high, low, width."""
    n = max(1, minutes // 5)
    last_start = _clock(5 * (n - 1))
    df = five[(five.index.time >= OPEN) & (five.index.time <= last_start)]
    rows = []
    for day, g in df.groupby(df.index.date):
        if len(g) < n or g.index[0].time() != OPEN:
            continue
        rows.append({"day": day, "high": float(g["High"].max()),
                     "low": float(g["Low"].min())})
    out = pd.DataFrame(rows, columns=["day", "high", "low"])
    out["width"] = out["high"] - out["low"]
    return out


def summarize(five: pd.DataFrame | None, now: datetime, *, minutes: int = MINUTES,
              lookback: int = LOOKBACK, wide: float = WIDE,
              cutoff: time = CUTOFF) -> dict[str, Any] | None:
    """Today's opening range at ``now`` (naive ET wall clock or aware), or None before
    09:30 / with no bars today. ``status``: forming, set (complete, no break yet),
    break_up / break_down (first 5-minute close outside, by ``cutoff``), no_break
    (cutoff passed inside the range)."""
    if five is None or five.empty:
        return None
    now_n = pd.Timestamp(now)
    if now_n.tzinfo is not None:
        now_n = now_n.tz_convert("America/New_York").tz_localize(None)
    df = five[five.index + timedelta(minutes=5) <= now_n]
    today = now_n.date()
    todays = df[df.index.date == today]
    if todays.empty or todays.index[0].time() != OPEN:
        return None
    n = max(1, minutes // 5)
    ready = _clock(5 * n)
    base = {"day": today.isoformat(), "minutes": int(minutes),
            "ready_at": ready.strftime("%H:%M"), "cutoff": cutoff.strftime("%H:%M")}
    if len(todays) < n:
        part = todays.iloc[:n]
        return {**base, "status": FORMING, "high": float(part["High"].max()),
                "low": float(part["Low"].min()), "width": None, "avg_width": None,
                "rel": None, "wide": None, "break_at": None, "break_close": None}
    rng = todays.iloc[:n]
    hi, lo = float(rng["High"].max()), float(rng["Low"].min())
    width = hi - lo
    past = _ranges(df[df.index.date < today], minutes).tail(lookback)
    avg = float(past["width"].mean()) if len(past) >= MIN_HISTORY else None
    rel = (width / avg) if avg and avg > 0 else None
    status, at, px = SET, None, None
    for ts, bar in todays.iloc[n:].iterrows():
        end = (ts + timedelta(minutes=5)).time()
        if end > cutoff:
            break
        c = float(bar["Close"])
        if c > hi or c < lo:
            status = BREAK_UP if c > hi else BREAK_DOWN
            at, px = end.strftime("%H:%M"), c
            break
    if status == SET and now_n.time() > cutoff:
        status = NO_BREAK
    return {**base, "status": status, "high": round(hi, 2), "low": round(lo, 2),
            "width": round(width, 2), "avg_width": round(avg, 2) if avg else None,
            "rel": round(rel, 2) if rel is not None else None,
            "wide": (rel > wide) if rel is not None else None,
            "break_at": at, "break_close": round(px, 2) if px is not None else None}


def _verdict(s: dict) -> str:
    if s.get("wide") is None:
        return "width vs normal: not enough history"
    if s["wide"]:
        return f"⚠️ {s['rel']:.1f}x normal width: wide open, breakouts tend to fail. Skip them"
    return f"✅ {s['rel']:.1f}x normal width: breakout allowed"


def plan_line(s: dict | None) -> str | None:
    """One Game Plan line (HTML, Telegram-safe), or None when there is nothing to show."""
    if not s:
        return None
    m = s.get("minutes", MINUTES)
    if s["status"] == FORMING:
        return (f"⏱ {m}-min opening range: forming until {s['ready_at']} "
                f"(so far ${s['high']:.2f} / ${s['low']:.2f})")
    head = (f"⏱ {m}-min opening range: high ${s['high']:.2f} · low ${s['low']:.2f} · "
            f"width ${s['width']:.2f}")
    if s["status"] in (BREAK_UP, BREAK_DOWN):
        way = "UP" if s["status"] == BREAK_UP else "DOWN"
        tail = f"broke {way} at {s['break_at']} (close ${s['break_close']:.2f})"
    elif s["status"] == NO_BREAK:
        tail = f"no break by {s['cutoff']}"
    else:
        tail = f"no break yet (watching until {s['cutoff']})"
    return html.escape(f"{head}. {_verdict(s)}. {tail}", quote=False)


def alert_events(s: dict | None) -> list[str]:
    """Which once-a-day alerts this state allows: ``set`` once the range is complete,
    ``break`` once a breakout has happened."""
    if not s or s["status"] == FORMING:
        return []
    ev = ["set"]
    if s["status"] in (BREAK_UP, BREAK_DOWN):
        ev.append("break")
    return ev


def due_events(state: dict | None, ticker: str, s: dict | None) -> list[str]:
    """Events from ``alert_events`` not yet sent today for ``ticker``. ``state`` is the
    saved ``{ticker: {"day": "YYYY-MM-DD", "sent": [...]}}``."""
    if not s:
        return []
    own = (state or {}).get(ticker) or {}
    sent = set(own.get("sent") or []) if own.get("day") == s.get("day") else set()
    return [e for e in alert_events(s) if e not in sent]


def mark_sent(state: dict | None, ticker: str, day: str, events: list[str]) -> dict:
    """``state`` with ``events`` recorded as sent for ``ticker`` on ``day`` (new dict)."""
    out = dict(state or {})
    own = out.get(ticker) or {}
    sent = list(own.get("sent") or []) if own.get("day") == day else []
    out[ticker] = {"day": day, "sent": sent + [e for e in events if e not in sent]}
    return out


def message(ticker: str, s: dict, event: str) -> str:
    """Telegram HTML for one event (``set`` or ``break``)."""
    t = html.escape(ticker)
    m = s.get("minutes", MINUTES)
    if event == "set":
        lines = [f"⏱ <b>{t}</b> {m}-min opening range is set ({s['ready_at']} ET)",
                 f"High ${s['high']:.2f} · Low ${s['low']:.2f} · width ${s['width']:.2f}",
                 html.escape(_verdict(s), quote=False),
                 f"Alert set: a 5-min close above ${s['high']:.2f} or below ${s['low']:.2f} "
                 f"by {s['cutoff']}."]
    else:
        up = s["status"] == BREAK_UP
        lines = [f"{'🟢' if up else '🔴'} <b>{t}</b> broke {'UP' if up else 'DOWN'} out of "
                 f"the {m}-min opening range at {s['break_at']} ET",
                 f"5-min close ${s['break_close']:.2f} vs range "
                 f"${s['low']:.2f} – ${s['high']:.2f}",
                 html.escape(_verdict(s), quote=False)]
        if not s.get("wide"):
            far = s["low"] if up else s["high"]
            lines.append(f"Plan: {'calls' if up else 'puts'}, stop on a 5-min close back "
                         f"inside, last-resort stop ${far:.2f}; move to break-even fast, "
                         f"trail, out within ~30 min.")
    lines.append("<i>Paper idea, 60-session replay: a lead, not a proven edge.</i>")
    return "\n".join(lines)
