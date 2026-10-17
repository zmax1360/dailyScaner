"""gamma_shift — alert when the call wall or put wall moves to another strike.

After each scan the scheduler passes the gamma read (``gex.summary``, nearest live
expiry) to ``update``. It compares the walls with the ones last seen today and returns
one event per wall that moved, e.g. call wall $345 → $342.5. The day's path is kept so a
message can say "today: $345 → $342.5 → $340 → $345".

Noise guard: a new strike counts only when its gamma beats the old wall strike's gamma
*now* by ``MIN_LEAD`` (10%). Two near-equal strikes trading places every scan do not
alert; a clear handover does.

Why walls move during the day: open interest is fixed from the morning, so the walls
move because spot moved (gamma is largest near the money), IV changed, or a strike
traded for the first time today (``volume_history.latest_scan`` holds traded contracts
only). Display / alert only; nothing here feeds scoring. No network, no Streamlit.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import gex

ET = ZoneInfo("America/New_York")
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                          "gamma_walls.json")
MIN_LEAD = 0.10
WALLS = (("call_wall", "Call wall"), ("put_wall", "Put wall"))


def _strength(summary: dict, strike: float | None) -> float:
    """|GEX| at ``strike`` in this read; 0 when the strike is not in it."""
    if strike is None:
        return 0.0
    v = (summary.get("by_strike") or {}).get(float(strike))
    return abs(float(v)) if v is not None else 0.0


def _seed(summary: dict, day: str, hhmm: str) -> dict:
    state = {"day": day, "expiry": summary["expiry"], "path": {}}
    for key, _ in WALLS:
        state[key] = summary.get(key)
        state["path"][key] = [[hhmm, summary[key]]] if summary.get(key) is not None else []
    return state


def detect(prev: dict | None, summary: dict | None, *,
           min_lead: float = MIN_LEAD) -> tuple[dict | None, list[dict]]:
    """(new state, events) for one ticker. ``prev`` is that ticker's saved state.

    No events on the first read of a day or of a new expiry (it only sets the baseline),
    nor when a wall disappears from the read (the old one is kept)."""
    if not summary or not summary.get("expiry"):
        return prev, []
    as_of = summary["as_of"].astimezone(ET)
    day, hhmm = f"{as_of:%Y-%m-%d}", f"{as_of:%H:%M}"
    if not prev or prev.get("day") != day or prev.get("expiry") != summary["expiry"]:
        return _seed(summary, day, hhmm), []

    state = json.loads(json.dumps(prev))           # deep copy, JSON-safe
    events = []
    for key, label in WALLS:
        old, new = prev.get(key), summary.get(key)
        if new is None or (old is not None and float(new) == float(old)):
            continue
        path = state["path"].setdefault(key, [])
        if old is None:                            # first time this wall exists today
            state[key] = new
            path.append([hhmm, new])
            continue
        new_gex, old_now = _strength(summary, new), _strength(summary, old)
        if old_now > 0 and new_gex < old_now * (1 + min_lead):
            continue                               # too close to call; keep the old wall
        state[key] = new
        path.append([hhmm, new])
        events.append({
            "key": key, "label": label, "old": float(old), "new": float(new),
            "new_gex": summary.get(f"{key}_gex"),
            "old_gex_now": (summary.get("by_strike") or {}).get(float(old)),
            "path": [float(k) for _, k in path],
            "returned": float(new) in [float(k) for _, k in path[:-1]],
        })
    return state, events


def load_state(path: str | None = None) -> dict:
    try:
        with open(path or STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict, path: str | None = None) -> None:
    target = path or STATE_PATH
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, target)


def update(ticker: str, summary: dict | None, *, path: str | None = None,
           min_lead: float = MIN_LEAD) -> list[dict]:
    """Compare ``summary`` with ``ticker``'s saved walls, save the new state, return the
    events (empty when nothing moved)."""
    ticker = str(ticker or "").upper()
    all_state = load_state(path)
    new_state, events = detect(all_state.get(ticker), summary, min_lead=min_lead)
    if new_state is not None and new_state != all_state.get(ticker):
        all_state[ticker] = new_state
        save_state(all_state, path)
    return events


def _money(v: Any) -> str:
    return gex.fmt_money(v) or "n/a"


def message(ticker: str, events: list[dict], summary: dict) -> str:
    """Telegram (HTML) alert for the events of one scan."""
    as_of = summary["as_of"].astimezone(ET)
    d = datetime.strptime(summary["expiry"], "%Y-%m-%d")
    lines = [f"🧱 <b>{str(ticker).upper()} gamma shift</b> · {d:%b} {d.day} expiry · "
             f"{as_of:%H:%M} ET"]
    for e in events:
        back = " (back to an earlier level)" if e["returned"] else ""
        lines.append(f"{e['label']} <b>${e['old']:g} → ${e['new']:g}</b>{back}")
        lines.append(f"  ${e['new']:g} {_money(e['new_gex'])} vs ${e['old']:g} "
                     f"{_money(e['old_gex_now'])} now")
        lines.append("  Today: " + " → ".join(f"${k:g}" for k in e["path"]))
    lines.append(f"Spot <b>${float(summary['spot']):.2f}</b>")
    lines.append("<i>dollars of hedging per $1 move · OI is from the morning, so a wall "
                 "moves mostly because price moved</i>")
    return "\n".join(lines)
