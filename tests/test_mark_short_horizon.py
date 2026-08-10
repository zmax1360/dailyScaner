"""Short-horizon exit marks (t15m / t30m) — bid fill, idempotent, 16:00 rule."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from attribution import (
    _db,
    _ensure_schema,
    due_for_marking,
    write_mark,
)
from mark_runner import (
    _mark_horizon,
    short_mark_due_after_cash_close,
)

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _seed(db: str, *, ts: datetime, flag_id: int = 1) -> int:
    with _db(db) as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO runs (run_id, ts_et, ticker, n_scored, config_hash, run_kind)
            VALUES ('r1', ?, 'AAPL', 1, 'testhash', 'intraday')
            """,
            (ts.isoformat(timespec="seconds"),),
        )
        conn.execute(
            """
            INSERT INTO flags (
                flag_id, run_id, ts_et, ticker, side, strike, expiry, mid, score, rank
            ) VALUES (?, 'r1', ?, 'AAPL', 'CALL', 200.0, '2026-08-21', 1.50, 0.5, 1)
            """,
            (flag_id, ts.isoformat(timespec="seconds")),
        )
    return flag_id


def test_write_mark_t15m_is_idempotent(tmp_path):
    db = str(tmp_path / "idem.db")
    fid = _seed(db, ts=_et(2026, 8, 8, 10, 0))
    assert write_mark(fid, "t15m", 1.25, db_path=db, mark_method="quote") is True
    assert write_mark(fid, "t15m", 9.99, db_path=db, mark_method="quote") is False
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_t15m, method_t15m, marked_t15m_at FROM flags WHERE flag_id=?",
            (fid,),
        ).fetchone()
    assert row["mark_t15m"] == pytest.approx(1.25)
    assert row["method_t15m"] == "quote"
    assert row["marked_t15m_at"] is not None


def test_marker_rerun_does_not_overwrite_t15m(tmp_path, monkeypatch):
    """Full _mark_horizon path: second pass must leave the first bid untouched."""
    db = str(tmp_path / "rerun.db")
    ts = _et(2026, 8, 8, 10, 0)
    fid = _seed(db, ts=ts)
    as_of = _et(2026, 8, 8, 10, 20)

    calls = {"n": 0}

    def _exit(*_a, **_k):
        calls["n"] += 1
        # First call returns 1.10; later calls would return a different bid
        return (1.10 if calls["n"] == 1 else 7.77), "quote"

    monkeypatch.setattr("mark_runner.fetch_option_exit", _exit)

    a1, w1, _ = _mark_horizon("t15m", dry_run=False, as_of=as_of, db_path=db)
    assert a1 >= 1 and w1 == 1
    a2, w2, _ = _mark_horizon("t15m", dry_run=False, as_of=as_of, db_path=db)
    assert w2 == 0
    # Due set empty after seal — second pass should not re-fetch
    due = due_for_marking("t15m", db_path=db, as_of=as_of)
    assert all(int(r["flag_id"]) != fid for r in due)

    with _db(db) as c:
        row = c.execute(
            "SELECT mark_t15m, method_t15m FROM flags WHERE flag_id=?", (fid,),
        ).fetchone()
    assert row["mark_t15m"] == pytest.approx(1.10)
    assert row["method_t15m"] == "quote"


def test_due_after_1600_seals_unavailable(tmp_path, monkeypatch):
    """t15m due 16:05 ET → method=unavailable, mark NULL; never quote/close."""
    from attribution import CASH_CLOSE_TIME
    from mark_runner import due_datetime

    db = str(tmp_path / "unavail.db")
    # Flag 15:50 → t15m due 16:05 → after cash close
    ts = _et(2026, 8, 8, 15, 50)
    fid = _seed(db, ts=ts)
    due = due_datetime(ts, "t15m")
    assert due.time() >= CASH_CLOSE_TIME
    assert short_mark_due_after_cash_close(ts, "t15m") is True

    monkeypatch.setattr(
        "mark_runner.fetch_option_exit",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    as_of = _et(2026, 8, 8, 16, 10)
    _mark_horizon("t15m", dry_run=False, as_of=as_of, db_path=db)
    with _db(db) as c:
        row = c.execute(
            """
            SELECT mark_t15m, method_t15m, mark_close, close_method
            FROM flags WHERE flag_id=?
            """,
            (fid,),
        ).fetchone()
    assert row["mark_t15m"] is None
    assert row["method_t15m"] == "unavailable"
    assert row["method_t15m"] not in ("quote", "trade", "stale")
    assert row["mark_close"] is None  # not clamped to close
    assert row["close_method"] is None
    # Idempotent seal
    assert write_mark(
        fid, "t15m", None, db_path=db, mark_method="unavailable",
    ) is False


def test_ret_t15m_uses_ask_not_mid(tmp_path):
    """Primary short-horizon return is ask-entry / bid-exit."""
    db = str(tmp_path / "ret.db")
    ts = _et(2026, 8, 8, 10, 0)
    fid = _seed(db, ts=ts)
    with _db(db) as c:
        c.execute(
            "UPDATE flags SET mid=1.00, ask=1.10 WHERE flag_id=?", (fid,),
        )
    assert write_mark(fid, "t15m", 1.21, db_path=db, mark_method="quote") is True
    with _db(db) as c:
        row = c.execute(
            "SELECT ret_t15m, ret_t15m_mid FROM v_outcomes WHERE flag_id=?",
            (fid,),
        ).fetchone()
    # (1.21 - 1.10) / 1.10 ≈ 0.1 ; mid-based (1.21 - 1.00) / 1.00 = 0.21
    assert row["ret_t15m"] == pytest.approx(0.1, rel=1e-9)
    assert row["ret_t15m_mid"] == pytest.approx(0.21, rel=1e-9)


def test_resolve_market_data_source_logs_error_when_missing(monkeypatch, caplog):
    import logging
    from attribution import resolve_market_data_source_name
    from config import SCORING

    monkeypatch.setitem(SCORING, "market_data_source", SCORING["market_data_source"])
    # Remove key temporarily
    saved = SCORING.pop("market_data_source")
    try:
        with caplog.at_level(logging.ERROR, logger="attribution"):
            name = resolve_market_data_source_name()
        assert name == "yahoo"
        assert any(
            "market_data_source" in r.message and "falling back" in r.message
            for r in caplog.records
        )
    finally:
        SCORING["market_data_source"] = saved


def test_yahoo_stale_last_trade_rejected():
    from datetime import timedelta
    import pandas as pd
    from sources.yahoo import _yahoo_last_trade_is_fresh

    now = _et(2026, 8, 8, 11, 0)
    fresh = pd.Series({
        "lastTradeDate": pd.Timestamp(now - timedelta(minutes=2)).tz_convert("UTC"),
    })
    stale = pd.Series({
        "lastTradeDate": pd.Timestamp(now - timedelta(minutes=30)).tz_convert("UTC"),
    })
    missing = pd.Series({"lastPrice": 1.0})
    assert _yahoo_last_trade_is_fresh(
        fresh, mark_now=now, max_age=timedelta(minutes=5),
    )
    assert not _yahoo_last_trade_is_fresh(
        stale, mark_now=now, max_age=timedelta(minutes=5),
    )
    assert not _yahoo_last_trade_is_fresh(
        missing, mark_now=now, max_age=timedelta(minutes=5),
    )


def test_massive_exit_chain_cached_per_ticker(monkeypatch):
    import pandas as pd
    from sources.massive import MassiveSource

    calls = {"n": 0}
    frame = pd.DataFrame([
        {
            "side": "CALL", "strike": 200.0, "expiry": "2026-08-15", "dte": 6,
            "bid": 1.25, "ask": 1.35, "last": 1.30, "volume": 10,
            "openInterest": 100, "iv": 0.3, "delta": 0.5,
        }
    ])

    src = MassiveSource(api_key="test-key-not-used")
    monkeypatch.setattr(
        src, "fetch_chain",
        lambda ticker, *, max_dte: (calls.__setitem__("n", calls["n"] + 1) or frame),
    )
    a = src.fetch_option_exit("AAPL", "CALL", 200.0, "2026-08-15")
    b = src.fetch_option_exit("AAPL", "CALL", 200.0, "2026-08-15")
    assert a == (1.25, "quote") and b == (1.25, "quote")
    assert calls["n"] == 1
    # max_dte=7 on the cached path
    monkeypatch.setattr(
        src, "fetch_chain",
        lambda ticker, *, max_dte: (_ for _ in ()).throw(
            AssertionError(f"should use cache, got max_dte={max_dte}")
        ),
    )
    assert src.fetch_option_exit("AAPL", "CALL", 200.0, "2026-08-15") == (1.25, "quote")


def test_migration_adds_short_mark_columns(tmp_path):
    db = str(tmp_path / "mig.db")
    conn = sqlite3.connect(db)
    # Pre-v1.2-style flags table (has t1h indexes' columns, lacks short marks).
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY, ts_et TEXT, ticker TEXT,
            n_scored INTEGER, config_hash TEXT
        );
        CREATE TABLE flags (
            flag_id INTEGER PRIMARY KEY, run_id TEXT, ts_et TEXT,
            ticker TEXT, side TEXT, strike REAL, expiry TEXT,
            score REAL, rank INTEGER, multipliers TEXT DEFAULT '{}',
            mid REAL, is_control INTEGER DEFAULT 0,
            mark_t1h REAL, mark_t1d REAL, mark_expiry REAL
        );
        INSERT INTO runs VALUES ('r','2026-08-08T10:00:00-04:00','AAPL',0,'h');
        INSERT INTO flags (flag_id, run_id, ts_et, ticker, side, strike, expiry)
        VALUES (1,'r','2026-08-08T10:00:00-04:00','AAPL','CALL',200,'2026-08-21');
        """
    )
    conn.commit()
    before = conn.execute("SELECT flag_id, ticker FROM flags").fetchone()
    assert "mark_t15m" not in {r[1] for r in conn.execute("PRAGMA table_info(flags)")}
    _ensure_schema(conn)
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(flags)")}
    for col in (
        "mark_t15m", "marked_t15m_at", "method_t15m",
        "mark_t30m", "marked_t30m_at", "method_t30m",
    ):
        assert col in cols
    after = conn.execute("SELECT flag_id, ticker FROM flags").fetchone()
    assert after == before
    conn.close()
