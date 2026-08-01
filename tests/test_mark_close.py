"""Session-close horizon: due at 16:15 same day, stale next session, 0DTE intrinsic."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from attribution import (
    CLOSE_MARK_TIME,
    _db,
    _ensure_schema,
    due_for_marking,
    note_stale_horizon,
    write_mark,
)
from mark_runner import (
    HORIZON_PRIORITY,
    _fetch_close_mark,
    _mark_horizon,
    in_close_quote_window,
    in_mark_window,
    is_close_stale,
)

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _seed(
    db: str,
    *,
    ts: datetime,
    ticker: str = "AAPL",
    side: str = "CALL",
    strike: float = 302.5,
    expiry: str = "2026-07-31",
    dte: int | None = 0,
    run_id: str = "r1",
) -> int:
    with _db(db) as c:
        _ensure_schema(c)
        c.execute(
            """
            INSERT OR IGNORE INTO runs (
                run_id, ts_et, ticker, n_scored, config_hash
            ) VALUES (?, ?, ?, 1, 'hash')
            """,
            (run_id, ts.isoformat(timespec="seconds"), ticker),
        )
        c.execute(
            """
            INSERT INTO flags (
                run_id, ts_et, ticker, side, strike, expiry,
                score, rank, multipliers, mid, is_control, dte
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                1.0, 1, '{}', 2.5, 0, ?
            )
            """,
            (
                run_id,
                ts.isoformat(timespec="seconds"),
                ticker, side, strike, expiry, dte,
            ),
        )
        return int(c.execute("SELECT MAX(flag_id) FROM flags").fetchone()[0])


def test_horizon_priority_close_before_expiry():
    assert HORIZON_PRIORITY == ("t1h", "t1d", "close", "expiry")
    assert HORIZON_PRIORITY.index("close") < HORIZON_PRIORITY.index("expiry")


def test_mark_window_excludes_close_quote_band():
    """t1h/t1d gate ends at 16:15; close-quote window is 16:15–17:00."""
    at_1615 = _et(2026, 7, 31, 16, 15)
    at_1630 = _et(2026, 7, 31, 16, 30)
    assert in_mark_window(at_1615) is False
    assert in_close_quote_window(at_1615) is True
    assert in_close_quote_window(at_1630) is True
    assert in_mark_window(at_1630) is False
    assert CLOSE_MARK_TIME.hour == 16 and CLOSE_MARK_TIME.minute == 15


def test_due_same_session_after_1615(tmp_path):
    db = str(tmp_path / "due.db")
    ts = _et(2026, 7, 31, 9, 35)
    fid = _seed(db, ts=ts)
    before = due_for_marking("close", db_path=db, as_of=_et(2026, 7, 31, 16, 0))
    assert all(int(r["flag_id"]) != fid for r in before)
    after = due_for_marking("close", db_path=db, as_of=_et(2026, 7, 31, 16, 15))
    assert any(int(r["flag_id"]) == fid for r in after)
    # 15:50 flag also due at same session close
    fid2 = _seed(db, ts=_et(2026, 7, 31, 15, 50), strike=300.0, run_id="r2")
    after2 = due_for_marking("close", db_path=db, as_of=_et(2026, 7, 31, 16, 20))
    ids = {int(r["flag_id"]) for r in after2}
    assert fid in ids and fid2 in ids


def test_close_stale_next_session(tmp_path, monkeypatch):
    db = str(tmp_path / "stale.db")
    ts = _et(2026, 7, 31, 10, 0)
    fid = _seed(db, ts=ts)
    as_of = _et(2026, 8, 1, 10, 0)
    assert is_close_stale(ts, as_of) is True
    monkeypatch.setattr("attribution.now_et", lambda: as_of)
    _a, w, _s = _mark_horizon("close", dry_run=False, as_of=as_of, db_path=db)
    assert w == 0
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_close, notes FROM flags WHERE flag_id=?", (fid,),
        ).fetchone()
    assert row["mark_close"] is None
    assert "stale:close" in str(row["notes"])
    # Never retry
    due = due_for_marking("close", db_path=db, as_of=as_of)
    assert all(int(r["flag_id"]) != fid for r in due)
    assert note_stale_horizon(fid, "close", db_path=db) is False


def test_write_mark_close_rejects_zero_and_records_method(tmp_path):
    db = str(tmp_path / "wm.db")
    fid = _seed(db, ts=_et(2026, 7, 31, 10, 0))
    assert write_mark(fid, "close", 0.0, db_path=db, close_method="quote") is False
    assert write_mark(fid, "close", 1.25, db_path=db, close_method="quote") is True
    assert write_mark(fid, "close", 9.99, db_path=db, close_method="intrinsic") is False
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_close, close_method, marked_close_at FROM flags WHERE flag_id=?",
            (fid,),
        ).fetchone()
    assert row["mark_close"] == pytest.approx(1.25)
    assert row["close_method"] == "quote"
    assert row["marked_close_at"] is not None


def test_write_mark_close_requires_method(tmp_path):
    db = str(tmp_path / "nomethod.db")
    fid = _seed(db, ts=_et(2026, 7, 31, 10, 0))
    assert write_mark(fid, "close", 1.0, db_path=db) is False


def test_0dte_prefers_intrinsic_on_divergence(monkeypatch):
    row = {
        "ticker": "AAPL",
        "side": "CALL",
        "strike": 302.5,
        "expiry": "2026-07-31",
        "ts_et": "2026-07-31T10:00:00-04:00",
        "dte": 0,
    }
    monkeypatch.setattr("mark_runner.fetch_option_mid", lambda *a, **k: 6.85)
    monkeypatch.setattr(
        "mark_runner.fetch_underlying_close",
        lambda *a, **k: 308.91,
    )
    mid, method = _fetch_close_mark(row, cache={})
    assert method == "intrinsic"
    assert mid == pytest.approx(6.41, abs=1e-6)


def test_0dte_quote_when_agrees(monkeypatch):
    row = {
        "ticker": "AAPL",
        "side": "CALL",
        "strike": 300.0,
        "expiry": "2026-07-31",
        "ts_et": "2026-07-31T10:00:00-04:00",
        "dte": 0,
    }
    monkeypatch.setattr("mark_runner.fetch_option_mid", lambda *a, **k: 8.90)
    monkeypatch.setattr(
        "mark_runner.fetch_underlying_close",
        lambda *a, **k: 308.91,  # intrinsic 8.91
    )
    mid, method = _fetch_close_mark(row, cache={})
    assert method == "quote"
    assert mid == pytest.approx(8.90)
