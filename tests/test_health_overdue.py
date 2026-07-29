"""Window-aware overdue_t1h checks (Task 3)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from mark_runner import count_overdue_t1h, is_t1h_overdue

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_late_day_flag_not_reported_overdue():
    # Flag 15:30 → due 16:30 (outside window). At 18:00 same day: not overdue.
    ts = _et(2026, 7, 20, 15, 30)  # Monday
    as_of = _et(2026, 7, 20, 18, 0)
    assert is_t1h_overdue(ts, as_of) is False


def test_genuinely_stale_flag_is_reported():
    # Flag 10:00 → due 11:00 (inside window). At 15:00 with no mark: overdue.
    ts = _et(2026, 7, 20, 10, 0)
    as_of = _et(2026, 7, 20, 15, 0)
    assert is_t1h_overdue(ts, as_of) is True


def test_overnight_flag_becomes_overdue_next_session():
    # Flag 15:30 Fri/Mon → due outside window; next session 11:00 with no mark.
    ts = _et(2026, 7, 20, 15, 30)  # Monday
    as_of = _et(2026, 7, 21, 11, 0)  # Tuesday
    assert is_t1h_overdue(ts, as_of) is True


def test_count_overdue_t1h_respects_window(tmp_path):
    import sqlite3

    db = str(tmp_path / "od.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE flags (
            flag_id INTEGER PRIMARY KEY,
            ts_et TEXT NOT NULL,
            mark_t1h REAL
        )
        """
    )
    # Late-day unmarked — should not count at 18:00
    conn.execute(
        "INSERT INTO flags VALUES (1, ?, NULL)",
        (_et(2026, 7, 20, 15, 30).isoformat(timespec="seconds"),),
    )
    # Mid-day unmarked — should count at 15:00
    conn.execute(
        "INSERT INTO flags VALUES (2, ?, NULL)",
        (_et(2026, 7, 20, 10, 0).isoformat(timespec="seconds"),),
    )
    conn.commit()
    assert count_overdue_t1h(conn, as_of=_et(2026, 7, 20, 18, 0)) == 1
    assert count_overdue_t1h(conn, as_of=_et(2026, 7, 20, 15, 0)) == 1
    conn.close()
