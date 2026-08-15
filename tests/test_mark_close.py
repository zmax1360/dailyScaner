"""Session-close horizon: due at 16:15 same day, stale next session, 0DTE intrinsic."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from attribution import (
    CLOSE_MARK_TIME,
    UNMARKABLE_BEFORE,
    _db,
    _ensure_schema,
    backfill_unmarkable_close_seals,
    backfill_unmarkable_expiry_seals,
    due_for_marking,
    write_mark,
    SEAL_UNMARKABLE_CLOSE_SQL,
    SEAL_UNMARKABLE_EXPIRY_SQL,
)
from mark_runner import (
    HORIZON_PRIORITY,
    _fetch_close_mark,
    _mark_horizon,
    in_close_quote_window,
    in_mark_window,
    is_close_stale,
    rotate_due_rows,
    save_horizon_cursor,
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
    assert HORIZON_PRIORITY == ("t15m", "t30m", "t1h", "t1d", "close", "expiry")
    assert HORIZON_PRIORITY.index("close") < HORIZON_PRIORITY.index("expiry")
    assert HORIZON_PRIORITY.index("t15m") < HORIZON_PRIORITY.index("t1h")


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
    monkeypatch.setattr(
        "mark_runner.fetch_option_mid",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )
    _a, w, _s = _mark_horizon("close", dry_run=False, as_of=as_of, db_path=db)
    assert w == 1
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_close, close_method, marked_close_at, notes "
            "FROM flags WHERE flag_id=?",
            (fid,),
        ).fetchone()
    assert row["mark_close"] is None
    assert row["close_method"] == "unavailable"
    assert row["marked_close_at"] is not None
    # Never retry
    due = due_for_marking("close", db_path=db, as_of=as_of)
    assert all(int(r["flag_id"]) != fid for r in due)
    assert write_mark(
        fid, "close", None, db_path=db, close_method="unavailable",
    ) is False
    # Price write must not un-seal
    assert write_mark(fid, "close", 1.25, db_path=db, close_method="quote") is False


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


def test_fetch_close_mark_forwards_shared_source(monkeypatch):
    seen: dict = {}

    def fake_mid(ticker, side, strike, expiry, *, source=None):
        seen["source"] = source
        return 1.25

    monkeypatch.setattr("mark_runner.fetch_option_mid", fake_mid)
    src = object()
    row = {
        "ticker": "AAPL",
        "side": "CALL",
        "strike": 200.0,
        "expiry": "2026-08-21",
        "ts_et": "2026-08-10T10:00:00-04:00",
        "dte": 5,
    }
    mid, method = _fetch_close_mark(row, cache={}, source=src)
    assert seen["source"] is src
    assert method == "quote"
    assert mid == pytest.approx(1.25)


def test_close_pass_http_calls_collapse_with_shared_source(monkeypatch):
    """Before: 1 HTTP per contract. After: 1 HTTP per (ticker, expiry)."""
    from types import SimpleNamespace

    import pandas as pd
    from sources.yahoo import YahooSource

    calls: list[tuple[str, str]] = []

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def option_chain(self, expiry):
            calls.append((str(self.ticker).upper(), str(expiry)[:10]))
            leg = pd.DataFrame([{
                "strike": 200.0, "lastPrice": 1.30, "bid": 1.25, "ask": 1.35,
                "volume": 1, "openInterest": 1, "impliedVolatility": 0.3,
            }])
            return SimpleNamespace(calls=leg.copy(), puts=leg.copy())

    monkeypatch.setattr("sources.yahoo.yf.Ticker", FakeTicker)

    expiries = ["2026-08-14", "2026-08-15", "2026-08-21"]
    n_contracts = 0

    # BEFORE: new source per contract (what close did when source=None)
    before_calls = 0
    for ticker in ("AAPL", "NVDA"):
        for i in range(250):
            src = YahooSource()
            row = {
                "ticker": ticker, "side": "CALL", "strike": 200.0,
                "expiry": expiries[i % 3],
                "ts_et": "2026-08-10T10:00:00-04:00", "dte": 5,
            }
            _fetch_close_mark(row, cache={}, source=src)
            n_contracts += 1
            before_calls += src.option_chain_fetches
    assert n_contracts == 500
    assert before_calls == 500

    # AFTER: one source for the whole close pass
    calls.clear()
    src = YahooSource()
    n = 0
    for ticker in ("AAPL", "NVDA"):
        for i in range(250):
            row = {
                "ticker": ticker, "side": "CALL", "strike": 200.0,
                "expiry": expiries[i % 3],
                "ts_et": "2026-08-10T10:00:00-04:00", "dte": 5,
            }
            _fetch_close_mark(row, cache={}, source=src)
            n += 1
    assert n == 500
    assert src.option_chain_fetches == 6  # 2 tickers × 3 expiries
    assert len(calls) == 6


def test_rotate_due_rows_resumes_after_cursor(tmp_path):
    rows = [
        {"ticker": "AAPL", "expiry": "2026-08-14", "flag_id": i}
        for i in (1, 2, 3, 4, 5)
    ]
    path = str(tmp_path / "cursor.json")
    save_horizon_cursor("close", rows[1], path=path)  # last finished = 2
    out = rotate_due_rows(rows, "close", path=path)
    assert [r["flag_id"] for r in out] == [3, 4, 5, 1, 2]


def test_backfill_close_sql_cannot_match_window_start():
    """The live UPDATE predicate is a strict prefix < 2026-08-10."""
    sql = " ".join(SEAL_UNMARKABLE_CLOSE_SQL.split())
    assert "mark_close IS NULL" in sql
    assert "close_method IS NULL" in sql
    assert "substr(ts_et, 1, 10) < ?" in sql
    assert "mark_close =" not in sql.replace("mark_close IS NULL", "")
    assert UNMARKABLE_BEFORE == "2026-08-10"


def test_backfill_close_leaves_window_rows_untouched(tmp_path):
    db = str(tmp_path / "bound.db")
    fid_old = _seed(db, ts=_et(2026, 7, 31, 10, 0), run_id="old")
    fid_start = _seed(
        db, ts=_et(2026, 8, 10, 10, 0), expiry="2026-08-21",
        strike=210.0, run_id="win",
    )
    fid_later = _seed(
        db, ts=_et(2026, 8, 12, 10, 0), expiry="2026-08-21",
        strike=220.0, run_id="later",
    )
    n = backfill_unmarkable_close_seals(db_path=db, marked_at="2026-08-15T09:00:00-04:00")
    assert n == 1
    with _db(db) as c:
        rows = {
            int(r["flag_id"]): r
            for r in c.execute(
                "SELECT flag_id, mark_close, close_method, substr(ts_et,1,10) d "
                "FROM flags"
            )
        }
    assert rows[fid_old]["close_method"] == "unavailable"
    assert rows[fid_old]["mark_close"] is None
    assert rows[fid_start]["close_method"] is None
    assert rows[fid_start]["mark_close"] is None
    assert rows[fid_later]["close_method"] is None
    with _db(db) as c:
        n_window = c.execute(
            """
            SELECT COUNT(*) FROM flags
            WHERE mark_close IS NULL
              AND close_method IS NULL
              AND substr(ts_et, 1, 10) < ?
              AND substr(ts_et, 1, 10) >= ?
            """,
            (UNMARKABLE_BEFORE, UNMARKABLE_BEFORE),
        ).fetchone()[0]
    assert n_window == 0


def test_backfill_expiry_sql_requires_both_date_bounds():
    sql = " ".join(SEAL_UNMARKABLE_EXPIRY_SQL.split())
    assert "mark_expiry IS NULL" in sql
    assert "substr(expiry, 1, 10) < ?" in sql
    assert "substr(ts_et, 1, 10) < ?" in sql
    assert "mark_expiry =" not in sql.replace("mark_expiry IS NULL", "")


def test_backfill_expiry_skips_july_flag_with_august_expiry(tmp_path):
    db = str(tmp_path / "exp.db")
    fid_old = _seed(
        db, ts=_et(2026, 7, 31, 10, 0), expiry="2026-07-31", run_id="old",
    )
    fid_keep = _seed(
        db, ts=_et(2026, 7, 31, 11, 0), expiry="2026-08-21",
        strike=210.0, run_id="keep",
    )
    fid_win = _seed(
        db, ts=_et(2026, 8, 10, 10, 0), expiry="2026-08-14",
        strike=220.0, run_id="win",
    )
    n = backfill_unmarkable_expiry_seals(db_path=db, marked_at="2026-08-15T09:00:00-04:00")
    assert n == 1
    with _db(db) as c:
        rows = {
            int(r["flag_id"]): r
            for r in c.execute(
                "SELECT flag_id, mark_expiry, notes FROM flags"
            )
        }
    assert rows[fid_old]["mark_expiry"] is None
    assert "unavailable:expiry" in str(rows[fid_old]["notes"])
    assert rows[fid_keep]["notes"] in (None, "")
    assert rows[fid_win]["notes"] in (None, "")
    due = due_for_marking("expiry", db_path=db, as_of=_et(2026, 8, 22, 10, 0))
    ids = {int(r["flag_id"]) for r in due}
    assert fid_old not in ids
    assert fid_keep in ids  # August expiry still markable via underlying close
    assert fid_win in ids
