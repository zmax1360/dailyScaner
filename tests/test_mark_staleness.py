"""Staleness ceiling for t1h/t1d marks (expiry exempt)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from attribution import (
    _db,
    _ensure_schema,
    note_stale_horizon,
)
from mark_runner import (
    _mark_horizon,
    is_past_staleness_ceiling,
    market_hours_between,
)

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


def _seed_flag(db: str, *, ts: datetime, expiry: str = "2026-07-10") -> int:
    """Minimal run+flag row for mark tests."""
    with _db(db) as c:
        _ensure_schema(c)
        c.execute(
            """
            INSERT INTO runs (
                run_id, ts_et, ticker, n_scored, config_hash
            ) VALUES ('r-stale', ?, 'TEST', 1, 'hash')
            """,
            (ts.isoformat(timespec="seconds"),),
        )
        c.execute(
            """
            INSERT INTO flags (
                run_id, ts_et, ticker, side, strike, expiry,
                score, rank, multipliers, mid, is_control
            ) VALUES (
                'r-stale', ?, 'TEST', 'CALL', 250.0, ?,
                1.0, 1, '{}', 2.5, 0
            )
            """,
            (ts.isoformat(timespec="seconds"), expiry),
        )
        return int(c.execute("SELECT flag_id FROM flags").fetchone()[0])


def test_stale_t1h_is_not_written(tmp_path, monkeypatch):
    """
    Flag 10:00 → due 11:00. By 16:00 same day, >4 market hours have elapsed
    since first_markable → must note stale, never write mark_t1h.
    """
    db = str(tmp_path / "stale_t1h.db")
    ts = _et(2026, 7, 20, 10, 0)  # Monday
    fid = _seed_flag(db, ts=ts)
    as_of = _et(2026, 7, 20, 16, 0)
    assert is_past_staleness_ceiling("t1h", ts, as_of) is True

    # Even if a mid is available, ceiling must block the write
    monkeypatch.setattr(
        "mark_runner.fetch_option_mid",
        lambda *a, **k: 9.99,
    )
    _mark_horizon("t1h", dry_run=False, as_of=as_of, db_path=db)

    with _db(db) as c:
        row = c.execute(
            "SELECT mark_t1h, marked_t1h_at, notes FROM flags WHERE flag_id=?",
            (fid,),
        ).fetchone()
    assert row["mark_t1h"] is None
    assert row["marked_t1h_at"] is None
    assert row["notes"] is not None and "stale:t1h" in row["notes"]


def test_expiry_mark_exempt_from_staleness(tmp_path, monkeypatch):
    """Expiry marks remain writable regardless of wall/market age."""
    db = str(tmp_path / "stale_exp.db")
    # Flag days ago; expiry already past
    ts = _et(2026, 7, 1, 10, 0)
    fid = _seed_flag(db, ts=ts, expiry="2026-07-10")
    as_of = _et(2026, 7, 20, 12, 0)
    assert is_past_staleness_ceiling("expiry", ts, as_of) is False

    monkeypatch.setattr(
        "mark_runner.fetch_option_mid",
        lambda *a, **k: 1.25,
    )
    monkeypatch.setattr("attribution.now_et", lambda: as_of)
    attempted, written = _mark_horizon(
        "expiry", dry_run=False, as_of=as_of, db_path=db
    )
    assert attempted >= 1
    assert written == 1
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_expiry, notes FROM flags WHERE flag_id=?",
            (fid,),
        ).fetchone()
    assert row["mark_expiry"] == pytest.approx(1.25)
    assert row["notes"] is None or "stale:" not in str(row["notes"])


def test_stale_row_is_noted_once(tmp_path, monkeypatch):
    db = str(tmp_path / "stale_once.db")
    ts = _et(2026, 7, 20, 10, 0)
    fid = _seed_flag(db, ts=ts)
    as_of = _et(2026, 7, 20, 16, 0)

    assert note_stale_horizon(fid, "t1h", db_path=db) is True
    assert note_stale_horizon(fid, "t1h", db_path=db) is False

    monkeypatch.setattr(
        "mark_runner.fetch_option_mid",
        lambda *a, **k: 3.0,
    )
    # Second runner pass must not rewrite notes or write a mark
    _mark_horizon("t1h", dry_run=False, as_of=as_of, db_path=db)
    # due_for_marking excludes stale — so mark path shouldn't touch it;
    # assert notes still a single tag and mark still null
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_t1h, notes FROM flags WHERE flag_id=?",
            (fid,),
        ).fetchone()
    assert row["mark_t1h"] is None
    assert row["notes"] == "stale:t1h"
    assert str(row["notes"]).count("stale:t1h") == 1


def test_market_hours_between_skips_overnight():
    # Mon 15:00 → Tue 10:30 = 1.25h Mon (15:00–16:15) + 1.0h Tue (09:30–10:30)
    start = _et(2026, 7, 20, 15, 0)
    end = _et(2026, 7, 21, 10, 30)
    assert market_hours_between(start, end) == pytest.approx(2.25, abs=1e-6)


def test_fresh_t1h_still_writable(tmp_path, monkeypatch):
    """Inside the 4h market ceiling, a normal mark still writes."""
    db = str(tmp_path / "fresh_t1h.db")
    ts = _et(2026, 7, 20, 10, 0)
    fid = _seed_flag(db, ts=ts)
    as_of = _et(2026, 7, 20, 13, 0)  # 2 market hours since 11:00 due
    assert is_past_staleness_ceiling("t1h", ts, as_of) is False
    monkeypatch.setattr(
        "mark_runner.fetch_option_mid",
        lambda *a, **k: 4.5,
    )
    monkeypatch.setattr("attribution.now_et", lambda: as_of)
    _mark_horizon("t1h", dry_run=False, as_of=as_of, db_path=db)
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_t1h, notes FROM flags WHERE flag_id=?",
            (fid,),
        ).fetchone()
    assert row["mark_t1h"] == pytest.approx(4.5)
    assert row["notes"] is None or "stale:t1h" not in str(row["notes"])
