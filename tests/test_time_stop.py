"""Time-stop math: now-relative windows, 15:45 cap, expiry cap, hours not minutes."""

from datetime import datetime
from zoneinfo import ZoneInfo

from time_stop import (
    compute_time_stop,
    format_exit_by_cell,
    hold_hours_for_dte,
    window_minutes_for_dte,
)

ET = ZoneInfo("America/New_York")


def _at(hhmm: str, day: str = "2026-08-28") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=ET)


def test_window_and_hold_hours_not_minutes():
    assert window_minutes_for_dte(0) == 30
    assert window_minutes_for_dte(1) == 60
    assert window_minutes_for_dte(5) == 60
    assert hold_hours_for_dte(0) == 0.5
    assert hold_hours_for_dte(1) == 1.0
    assert hold_hours_for_dte(2) == 1.0
    assert hold_hours_for_dte(0) not in (30, 60)
    assert hold_hours_for_dte(1) not in (30, 60)


def test_0dte_is_now_plus_30():
    now = _at("10:00")
    r = compute_time_stop(now=now, dte=0, expiry="2026-08-28")
    assert r["capped"] is False
    assert r["window_minutes"] == 30
    assert r["hold_hours"] == 0.5
    assert r["exit_at"].strftime("%H:%M") == "10:30"
    assert r["label"] == "exit by 10:30 (30 min)"
    assert "capped" not in r["label"]


def test_1dte_is_now_plus_60():
    now = _at("10:00")
    r = compute_time_stop(now=now, dte=1, expiry="2026-08-29")
    assert r["capped"] is False
    assert r["window_minutes"] == 60
    assert r["hold_hours"] == 1.0
    assert r["exit_at"].strftime("%H:%M") == "11:00"
    assert r["label"] == "exit by 11:00 (60 min)"


def test_1545_hard_cap():
    now = _at("15:20")
    r = compute_time_stop(now=now, dte=0, expiry="2026-08-28")
    assert r["capped"] is True
    assert r["exit_at"].strftime("%H:%M") == "15:45"
    assert r["remaining_minutes"] == 25
    assert r["label"] == "exit by 15:45 (25 min - capped)"
    assert r["exit_at"] > now


def test_after_1545_no_past_timestamp():
    now = _at("15:50")
    r = compute_time_stop(now=now, dte=0, expiry="2026-08-28")
    assert r["label"] == "no time left today"
    assert r["exit_at"] is None
    assert "15:" not in r["label"]
    cell = format_exit_by_cell(0, "2026-08-28", now=now)
    assert cell == "no time left today"
    r1 = compute_time_stop(now=now, dte=1, expiry="2026-08-29")
    assert r1["label"] == "no time left today"


def test_exactly_1545_no_time_left():
    now = _at("15:45")
    r = compute_time_stop(now=now, dte=1, expiry="2026-08-29")
    assert r["label"] == "no time left today"
    assert r["exit_at"] is None


def test_expiry_cap_before_1545():
    now = _at("15:00")
    # Expiry session end 16:00 is after 15:45, so 15:30 window still fits.
    r = compute_time_stop(now=now, dte=0, expiry="2026-08-28")
    assert r["label"] == "exit by 15:30 (30 min)"
    # If expiry were already the cap (same-day 15:45 binds first at 15:20+).
    late = compute_time_stop(now=_at("15:30"), dte=0, expiry="2026-08-28")
    assert late["label"] == "exit by 15:45 (15 min - capped)"


def test_fully_extrinsic_flag_appended():
    now = _at("10:00")
    cell = format_exit_by_cell(0, "2026-08-28", now=now, fully_extrinsic=True)
    assert cell == "exit by 10:30 (30 min) · expires worthless if held"
    late = format_exit_by_cell(0, "2026-08-28", now=_at("15:50"), fully_extrinsic=True)
    assert late == "no time left today · expires worthless if held"
