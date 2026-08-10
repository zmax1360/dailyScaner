"""Expiry intrinsic marks, permanent-fail notes, horizon priority, runtime cap."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from attribution import _db, _ensure_schema, due_for_marking, write_mark
from mark_runner import (
    HORIZON_PRIORITY,
    _mark_horizon,
    fetch_underlying_close,
    option_intrinsic,
    t1h_mark_health_ok,
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
    strike: float = 250.0,
    expiry: str = "2026-07-10",
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
                score, rank, multipliers, mid, is_control
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                1.0, 1, '{}', 2.5, 0
            )
            """,
            (
                run_id,
                ts.isoformat(timespec="seconds"),
                ticker, side, strike, expiry,
            ),
        )
        return int(c.execute("SELECT MAX(flag_id) FROM flags").fetchone()[0])


def test_option_intrinsic_call_put():
    assert option_intrinsic("CALL", 100.0, 105.0) == pytest.approx(5.0)
    assert option_intrinsic("CALL", 100.0, 95.0) == pytest.approx(0.0)
    assert option_intrinsic("PUT", 100.0, 95.0) == pytest.approx(5.0)
    assert option_intrinsic("PUT", 100.0, 105.0) == pytest.approx(0.0)


def test_expiry_uses_intrinsic_not_option_chain(tmp_path, monkeypatch):
    db = str(tmp_path / "intr.db")
    ts = _et(2026, 7, 1, 10, 0)
    fid = _seed(db, ts=ts, strike=340.0, expiry="2026-07-10", side="PUT")
    as_of = _et(2026, 7, 20, 12, 0)

    monkeypatch.setattr(
        "mark_runner.fetch_underlying_close",
        lambda *a, **k: 330.0,  # PUT 340 → intrinsic 10
    )
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("must not fetch option mid for expiry")

    monkeypatch.setattr("mark_runner.fetch_option_mid", boom)
    monkeypatch.setattr("attribution.now_et", lambda: as_of)
    _a, w, _s = _mark_horizon("expiry", dry_run=False, as_of=as_of, db_path=db)
    assert calls["n"] == 0
    assert w == 1
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_expiry FROM flags WHERE flag_id=?", (fid,),
        ).fetchone()
    assert row["mark_expiry"] == pytest.approx(10.0)


def test_expiry_otm_writes_zero(tmp_path, monkeypatch):
    db = str(tmp_path / "otm.db")
    ts = _et(2026, 7, 1, 10, 0)
    fid = _seed(db, ts=ts, strike=250.0, expiry="2026-07-10", side="CALL")
    as_of = _et(2026, 7, 20, 12, 0)
    monkeypatch.setattr(
        "mark_runner.fetch_underlying_close",
        lambda *a, **k: 240.0,
    )
    monkeypatch.setattr("attribution.now_et", lambda: as_of)
    _mark_horizon("expiry", dry_run=False, as_of=as_of, db_path=db)
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_expiry FROM flags WHERE flag_id=?", (fid,),
        ).fetchone()
    assert row["mark_expiry"] == pytest.approx(0.0)
    # write_mark still rejects 0 for live horizons
    assert write_mark(fid, "t1h", 0.0, db_path=db) is False


def test_permanent_valueerror_noted_not_retried(tmp_path, monkeypatch):
    db = str(tmp_path / "fail.db")
    ts = _et(2026, 7, 20, 10, 0)
    fid = _seed(db, ts=ts, expiry="2026-08-21")
    as_of = _et(2026, 7, 20, 12, 0)

    def boom(*_a, **_k):
        raise ValueError("expiry not found")

    monkeypatch.setattr("mark_runner.fetch_option_mid", boom)
    monkeypatch.setattr("attribution.now_et", lambda: as_of)
    _mark_horizon("t1h", dry_run=False, as_of=as_of, db_path=db)
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_t1h, notes FROM flags WHERE flag_id=?", (fid,),
        ).fetchone()
    assert row["mark_t1h"] is None
    assert "fail:t1h:" in str(row["notes"])
    # Second pass: due_for_marking must exclude the failed row
    due = due_for_marking("t1h", db_path=db, as_of=as_of)
    assert all(int(r["flag_id"]) != fid for r in due)


def test_underlying_close_cached(monkeypatch):
    cache: dict = {}
    n = {"calls": 0}

    def _fake(ticker, day):
        n["calls"] += 1
        return 100.0

    monkeypatch.setattr(
        "sources.yahoo.fetch_underlying_close_on", _fake,
    )
    from datetime import date

    a = fetch_underlying_close("AAPL", date(2026, 7, 10), cache=cache)
    b = fetch_underlying_close("AAPL", date(2026, 7, 10), cache=cache)
    assert a == pytest.approx(100.0)
    assert b == pytest.approx(100.0)
    assert n["calls"] == 1


def test_horizon_priority_order():
    assert HORIZON_PRIORITY == ("t15m", "t30m", "t1h", "t1d", "close", "expiry")


def test_runtime_cap_stops_early(tmp_path):
    db = str(tmp_path / "cap.db")
    ts = _et(2026, 7, 1, 10, 0)
    for i in range(5):
        _seed(
            db, ts=ts, expiry="2026-07-10", strike=250.0 + i,
            run_id=f"r{i}",
        )
    as_of = _et(2026, 7, 20, 12, 0)
    # Deadline already in the past → stop before any work
    _a, _w, stopped = _mark_horizon(
        "expiry",
        dry_run=False,
        as_of=as_of,
        db_path=db,
        deadline=0.0,
    )
    assert stopped is True


def test_t1h_health_fails_when_stale(tmp_path):
    db = str(tmp_path / "health.db")
    ts = _et(2026, 7, 20, 10, 0)
    fid = _seed(db, ts=ts)
    with _db(db) as c:
        c.execute(
            "UPDATE flags SET marked_t1h_at=?, mark_t1h=1.0 WHERE flag_id=?",
            (_et(2026, 7, 20, 10, 5).isoformat(timespec="seconds"), fid),
        )
    as_of = _et(2026, 7, 20, 12, 0)  # inside mark window, age ~115m
    with _db(db) as c:
        ok, detail = t1h_mark_health_ok(c, as_of=as_of, max_age_min=90.0)
    assert ok is False
    assert "age=" in detail
