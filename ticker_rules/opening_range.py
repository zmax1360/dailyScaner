"""opening_range — the day's first N minutes (default 20: 09:30-09:50 ET) as a range,
its width against recent days, and the first 5-minute close outside it.

From a 2026-11-06 replay of 60 AAPL sessions (Aug 14 - Nov 6, Yahoo 5-minute bars): a
plain breakout was close to a coin flip, but two things held for every range length
tested (5/10/15/20 minutes):

  * a WIDE opening range (more than ``WIDE`` x the average of the previous ``LOOKBACK``
    sessions) broke out and failed: 15-36% winners, against 62-82% for a normal range;
  * the 20-minute range had the fewest fake-outs (13% vs 25% for 5 minutes) and the best
    30 minutes after the break (64% up in the break's direction).

Sneaky second candle (same replay): the first 5-minute candle has a real body (at least
``SNEAKY_MIN_BODY_PCT`` of the open) and the second is the opposite colour, takes back
40-100% of that body and does not break the first candle's far end. 11 of 60 days. The
first close beyond the FIRST candle's high/low then went the first candle's way on 4 days
(3 won over the next 30 minutes, +$0.77/share) and the other way on 6 (2 won, -$0.71).
So on a sneaky day only a break in the first candle's direction is shown as tradeable.

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
SNEAKY_MIN_BODY_PCT = 0.15   # first candle body, % of its open (AAPL ~$0.50)
SNEAKY_RETRACE = (0.40, 1.00)  # share of that body the second candle takes back
OPEN = time(9, 30)

FORMING, SET, BREAK_UP, BREAK_DOWN, NO_BREAK = (
    "forming", "set", "break_up", "break_down", "no_break")


def sneaky_open(todays: pd.DataFrame, *, cutoff: time = CUTOFF) -> dict[str, Any] | None:
    """The first two 5-minute candles of ``todays`` (complete bars from 09:30): the first
    candle's direction and extremes, whether the second is a sneaky candle, and (when it
    is) the first later close beyond the first candle's high / low by ``cutoff``:
    ``resume`` (the first candle's way) or ``reverse``. None before the second candle closes."""
    if len(todays) < 2 or todays.index[0].time() != OPEN:
        return None
    a, b = todays.iloc[0], todays.iloc[1]
    o, c = float(a["Open"]), float(a["Close"])
    body = abs(c - o)
    d = 1 if c > o else -1 if c < o else 0
    out = {"first_dir": {1: "up", -1: "down", 0: "flat"}[d],
           "first_high": round(float(a["High"]), 2), "first_low": round(float(a["Low"]), 2),
           "sneaky": False, "retrace": None, "first_break": None, "first_break_at": None,
           "first_break_close": None}
    if d == 0 or o <= 0 or body / o * 100 < SNEAKY_MIN_BODY_PCT:
        return out
    retr = (c - float(b["Close"])) * d / body
    out["retrace"] = round(retr, 2)
    opposite = (float(b["Close"]) - float(b["Open"])) * d < 0
    inside = float(b["Low"]) >= float(a["Low"]) if d > 0 else float(b["High"]) <= float(a["High"])
    out["sneaky"] = bool(opposite and inside and SNEAKY_RETRACE[0] <= retr <= SNEAKY_RETRACE[1])
    if not out["sneaky"]:
        return out
    hi, lo = float(a["High"]), float(a["Low"])
    for ts, bar in todays.iloc[2:].iterrows():
        end = (ts + timedelta(minutes=5)).time()
        if end > cutoff:
            break
        px = float(bar["Close"])
        if px > hi or px < lo:
            out["first_break"] = "resume" if (px > hi) == (d > 0) else "reverse"
            out["first_break_at"], out["first_break_close"] = end.strftime("%H:%M"), round(px, 2)
            break
    return out


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
    base["open"] = sneaky_open(todays, cutoff=cutoff)
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


def sneaky_note(s: dict | None) -> str | None:
    """Plain-text sneaky-candle warning for ``s`` (a summary), or None on a normal open."""
    op = (s or {}).get("open") or {}
    if not op.get("sneaky"):
        return None
    up = op["first_dir"] == "up"
    level = op["first_high"] if up else op["first_low"]
    note = (f"⚠️ Sneaky 2nd candle (took back {op['retrace'] * 100:.0f}% of the first): trade "
            f"only a 5-min close {'above' if up else 'below'} ${level:.2f} "
            f"({'calls' if up else 'puts'}); a break the other way failed 4 of 6 times")
    if op.get("first_break"):
        way = "the first candle's way ✅" if op["first_break"] == "resume" else "the other way ❌"
        note += f". First break: {way} at {op['first_break_at']}"
    return note


def against_sneaky(s: dict | None) -> bool:
    """True when the range broke against the first candle on a sneaky day."""
    op = (s or {}).get("open") or {}
    if not op.get("sneaky") or s.get("status") not in (BREAK_UP, BREAK_DOWN):
        return False
    return (s["status"] == BREAK_UP) != (op["first_dir"] == "up")


def plan_line(s: dict | None) -> str | None:
    """One Game Plan line (HTML, Telegram-safe), or None when there is nothing to show."""
    if not s:
        return None
    m = s.get("minutes", MINUTES)
    if s["status"] == FORMING:
        sn = sneaky_note(s)
        return html.escape(f"⏱ {m}-min opening range: forming until {s['ready_at']} "
                           f"(so far ${s['high']:.2f} / ${s['low']:.2f})"
                           + (f". {sn}" if sn else ""), quote=False)
    head = (f"⏱ {m}-min opening range: high ${s['high']:.2f} · low ${s['low']:.2f} · "
            f"width ${s['width']:.2f}")
    if s["status"] in (BREAK_UP, BREAK_DOWN):
        way = "UP" if s["status"] == BREAK_UP else "DOWN"
        tail = f"broke {way} at {s['break_at']} (close ${s['break_close']:.2f})"
    elif s["status"] == NO_BREAK:
        tail = f"no break by {s['cutoff']}"
    else:
        tail = f"no break yet (watching until {s['cutoff']})"
    sn = sneaky_note(s)
    return html.escape(f"{head}. {_verdict(s)}. {tail}" + (f". {sn}" if sn else ""),
                       quote=False)


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


LOG_KEEP_DAYS = 400


def log_update(log: dict | None, ticker: str, s: dict | None) -> dict:
    """``log`` (``{ticker: {day: summary}}``) with today's newest summary for ``ticker``;
    the last ``LOG_KEEP_DAYS`` days per ticker are kept. The day's final row is the one
    left after the last scan, so every open (sneaky or not) is on record for later study."""
    out = {k: dict(v) for k, v in (log or {}).items()}
    if not s or not s.get("day"):
        return out
    days = out.setdefault(ticker, {})
    days[s["day"]] = s
    for old in sorted(days)[:-LOG_KEEP_DAYS]:
        del days[old]
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
        sn = sneaky_note(s)
        if sn:
            lines.append(html.escape(sn, quote=False))
    else:
        up = s["status"] == BREAK_UP
        lines = [f"{'🟢' if up else '🔴'} <b>{t}</b> broke {'UP' if up else 'DOWN'} out of "
                 f"the {m}-min opening range at {s['break_at']} ET",
                 f"5-min close ${s['break_close']:.2f} vs range "
                 f"${s['low']:.2f} – ${s['high']:.2f}",
                 html.escape(_verdict(s), quote=False)]
        if against_sneaky(s):
            lines.append("⚠️ Against the first candle on a sneaky-candle day: in the replay "
                         "these breaks failed 4 of 6 times. Skip it.")
        elif not s.get("wide"):
            far = s["low"] if up else s["high"]
            lines.append(f"Plan: {'calls' if up else 'puts'}, stop on a 5-min close back "
                         f"inside, last-resort stop ${far:.2f}; move to break-even fast, "
                         f"trail, out within ~30 min.")
    lines.append("<i>Paper idea, 60-session replay: a lead, not a proven edge.</i>")
    return "\n".join(lines)
