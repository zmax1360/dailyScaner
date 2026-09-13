"""
time_stop.py — scanner display time stop. No user input, no scoring.

Window is now-relative at render: DTE 0 → 30 min, DTE ≥ 1 → 60 min.
Hard-cap 15:45 ET; never past expiry (session close 16:00 ET on expiry date).
Never emits a clock time in the past.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

TIME_STOP_CAP = time(15, 45)
EXPIRY_SESSION_END = time(16, 0)
WINDOW_0DTE_MINUTES = 30
WINDOW_MULTI_DTE_MINUTES = 60


def _as_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def window_minutes_for_dte(dte: Any) -> int:
    try:
        d = int(dte)
    except (TypeError, ValueError):
        d = 0
    if d <= 0:
        return WINDOW_0DTE_MINUTES
    return WINDOW_MULTI_DTE_MINUTES


def hold_hours_for_dte(dte: Any) -> float:
    """Expected hold duration in hours (never raw minutes)."""
    return window_minutes_for_dte(dte) / 60.0


def _parse_expiry_date(expiry: Any) -> date | None:
    if expiry is None:
        return None
    if isinstance(expiry, datetime):
        return _as_et(expiry).date()
    if isinstance(expiry, date):
        return expiry
    s = str(expiry).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")[:10]).date()
    except ValueError:
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            return None


def expiry_session_end(expiry: Any, now: datetime) -> datetime | None:
    """Equity-option session close (16:00 ET) on the expiry date."""
    d = _parse_expiry_date(expiry)
    if d is None:
        return None
    now_et = _as_et(now)
    return datetime.combine(d, EXPIRY_SESSION_END, tzinfo=now_et.tzinfo)


def compute_time_stop(
    *,
    now: datetime,
    dte: Any,
    expiry: Any = None,
) -> dict[str, Any]:
    """
    Returns label, exit_at, window_minutes, remaining_minutes, capped, hold_hours.

    label is either ``exit by HH:MM (…)`` or ``no time left today``.
    """
    now_et = _as_et(now)
    window = window_minutes_for_dte(dte)
    hold = hold_hours_for_dte(dte)
    cap = now_et.replace(
        hour=TIME_STOP_CAP.hour,
        minute=TIME_STOP_CAP.minute,
        second=0,
        microsecond=0,
    )
    empty = {
        "label": "no time left today",
        "exit_at": None,
        "window_minutes": window,
        "remaining_minutes": None,
        "capped": True,
        "hold_hours": hold,
    }
    if now_et >= cap:
        return empty

    raw = now_et + timedelta(minutes=window)
    exit_at = raw
    capped = False
    if exit_at > cap:
        exit_at = cap
        capped = True

    exp_dt = expiry_session_end(expiry, now_et)
    if exp_dt is not None and exit_at > exp_dt:
        exit_at = exp_dt
        capped = True

    if exit_at <= now_et:
        return empty

    remaining = int(round((exit_at - now_et).total_seconds() / 60.0))
    if remaining <= 0:
        return empty

    clock = exit_at.strftime("%H:%M")
    if capped:
        label = f"exit by {clock} ({remaining} min - capped)"
    else:
        label = f"exit by {clock} ({window} min)"
    return {
        "label": label,
        "exit_at": exit_at,
        "window_minutes": window,
        "remaining_minutes": remaining,
        "capped": capped,
        "hold_hours": hold,
    }


def format_exit_by_cell(
    dte: Any,
    expiry: Any = None,
    *,
    now: datetime | None = None,
    fully_extrinsic: bool = False,
) -> str:
    """Table cell. Recompute with a fresh ``now`` on every rerun."""
    result = compute_time_stop(
        now=now or datetime.now(ET),
        dte=dte,
        expiry=expiry,
    )
    label = str(result["label"])
    if fully_extrinsic:
        return f"{label} · expires worthless if held"
    return label
