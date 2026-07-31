"""Regression tests for eod_report bucketing / staleness (reporting layer only)."""

from __future__ import annotations

import io
import sqlite3
from contextlib import redirect_stdout

import pytest

from eod_report import (
    DTE_BUCKET_SQL,
    MAX_HOURS_T1D,
    MAX_HOURS_T1H,
    ReportFilter,
    clustered_bucket_rows,
    count_late_marks,
    section_buckets,
    section_verdict,
    verdict_rows,
)


SCHEMA = """
CREATE TABLE v_outcomes (
    flag_id INTEGER PRIMARY KEY,
    run_id TEXT,
    ts_et TEXT,
    ticker TEXT,
    side TEXT,
    strike REAL,
    expiry TEXT,
    dte INTEGER,
    score REAL,
    rank INTEGER,
    is_control INTEGER,
    mid REAL,
    mark_t1h REAL,
    mark_t1d REAL,
    mark_expiry REAL,
    marked_t1h_at TEXT,
    marked_t1d_at TEXT,
    marked_exp_at TEXT,
    config_hash TEXT,
    ret_t1h REAL,
    ret_t1d REAL,
    ret_expiry REAL,
    hours_t1h REAL,
    hours_t1d REAL,
    hours_expiry REAL
);
CREATE TABLE flags (
    flag_id INTEGER PRIMARY KEY,
    ts_et TEXT,
    ticker TEXT,
    rank INTEGER,
    is_control INTEGER,
    open_interest INTEGER,
    notes TEXT
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


def _ins(conn, **kw):
    cols = [
        "flag_id", "run_id", "ts_et", "ticker", "side", "strike", "expiry",
        "dte", "score", "rank", "is_control", "mid",
        "mark_t1h", "mark_t1d", "mark_expiry",
        "ret_t1h", "ret_t1d", "ret_expiry",
        "hours_t1h", "hours_t1d", "hours_expiry",
        "config_hash",
    ]
    defaults = {
        "run_id": "r1",
        "ts_et": "2026-07-20T10:00:00-04:00",
        "ticker": "AAPL",
        "side": "CALL",
        "strike": 200.0,
        "expiry": "2026-07-25",
        "dte": 5,
        "score": 0.5,
        "rank": 1,
        "is_control": 0,
        "mid": 1.0,
        "mark_t1h": 1.1,
        "mark_t1d": 1.2,
        "mark_expiry": None,
        "ret_t1h": 0.10,
        "ret_t1d": 0.20,
        "ret_expiry": None,
        "hours_t1h": 1.0,
        "hours_t1d": 24.0,
        "hours_expiry": None,
        "config_hash": "h",
    }
    defaults.update(kw)
    if "flag_id" not in defaults:
        defaults["flag_id"] = conn.execute(
            "SELECT COALESCE(MAX(flag_id),0)+1 FROM v_outcomes"
        ).fetchone()[0]
    conn.execute(
        f"INSERT INTO v_outcomes ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [defaults[c] for c in cols],
    )


def test_dte_bucket_sql_names_unknown():
    assert "UNKNOWN" in DTE_BUCKET_SQL
    assert "dte IS NULL" in DTE_BUCKET_SQL


def test_null_dte_reported_as_unknown():
    conn = _conn()
    _ins(conn, dte=None, rank=1, ret_t1h=0.05, hours_t1h=1.0, strike=100.0)
    _ins(conn, dte=0, rank=1, ret_t1h=-0.10, hours_t1h=1.0, strike=101.0)
    _ins(conn, dte=5, rank=1, ret_t1h=0.02, hours_t1h=1.0, strike=102.0)

    rows = clustered_bucket_rows(conn, "1=1", [], "t1h")
    by = {(d, r): n for d, r, n, *_ in rows}
    assert ("UNKNOWN", "01-03") in by
    assert ("0DTE", "01-03") in by
    assert ("1DTE+", "01-03") in by
    assert by[("UNKNOWN", "01-03")] == 1
    assert by[("0DTE", "01-03")] == 1
    assert by[("1DTE+", "01-03")] == 1
    # NULL must not inflate 1DTE+
    assert sum(n for (d, _), n in by.items() if d == "1DTE+") == 1


def test_bucket_and_verdict_agree():
    """Regression: section_buckets 1DTE+ 01-03 n == section_verdict TOP3 n."""
    conn = _conn()
    # NULL dte — must NOT count in 1DTE+ / TOP3
    for i in range(3):
        _ins(
            conn, dte=None, rank=1, strike=150.0 + i,
            ret_t1h=0.01, hours_t1h=1.0, is_control=0,
        )
    # 0DTE top3
    _ins(conn, dte=0, rank=2, strike=160.0, ret_t1h=-0.2, hours_t1h=1.0)
    # 1DTE+ top3 — three distinct contracts
    for i in range(3):
        _ins(
            conn, dte=5, rank=1 + (i % 3), strike=170.0 + i,
            ret_t1h=0.03, hours_t1h=1.0, is_control=0,
        )
    # 1DTE+ control
    _ins(
        conn, dte=5, rank=99, strike=180.0, ret_t1h=0.01, hours_t1h=1.0,
        is_control=1,
    )

    buckets = clustered_bucket_rows(conn, "1=1", [], "t1h")
    n_0103 = next(
        n for d, r, n, *_ in buckets if d == "1DTE+" and r == "01-03"
    )
    v = {b: n for b, n, _ in verdict_rows(conn, "1=1", [])}
    assert v["TOP3"] == n_0103
    assert n_0103 == 3


def test_t1d_staleness_filter_applied():
    conn = _conn()
    _ins(
        conn, dte=5, rank=1, strike=200.0,
        ret_t1d=0.50, hours_t1d=72.0,  # late — must exclude
    )
    _ins(
        conn, dte=5, rank=1, strike=201.0,
        ret_t1d=0.10, hours_t1d=20.0,  # ok
    )
    rows = clustered_bucket_rows(conn, "1=1", [], "t1d")
    total_n = sum(r[2] for r in rows)
    assert total_n == 1
    assert count_late_marks(conn, "1=1", [], "t1d") == 1
    assert MAX_HOURS_T1D == 30.0


def test_t1d_excluded_count_reported():
    conn = _conn()
    _ins(conn, dte=5, rank=1, strike=210.0, ret_t1d=1.5, hours_t1d=72.0)
    _ins(conn, dte=5, rank=1, strike=211.0, ret_t1d=0.1, hours_t1d=10.0)
    buf = io.StringIO()
    with redirect_stdout(buf):
        section_buckets(conn, "1=1", [], "t1d")
    text = buf.getvalue()
    assert f"hours_t1d <= {MAX_HOURS_T1D}" in text
    assert "excluded 1 marks taken > 30.0h late" in text or "excluded 1 marks" in text
    assert "<= any" not in text


def test_clustering_collapses_repeated_scans():
    conn = _conn()
    # Same contract, 20 intraday flags → one clustered observation
    for i in range(20):
        _ins(
            conn,
            ts_et=f"2026-07-20T{10 + i // 60:02d}:{i % 60:02d}:00-04:00",
            dte=5, rank=1, strike=250.0, expiry="2026-07-25",
            side="CALL", ticker="AAPL",
            ret_t1h=0.01 * (i + 1), hours_t1h=1.0,
        )
    rows = clustered_bucket_rows(conn, "1=1", [], "t1h")
    n = next(r[2] for r in rows if r[0] == "1DTE+" and r[1] == "01-03")
    assert n == 1


def test_report_filter_builds_explicit_where():
    f = ReportFilter(since="2026-07-01", ticker="AAPL")
    sql, args = f.where_sql()
    assert "date(ts_et) >= ?" in sql
    assert "ticker = ?" in sql
    assert args == ["2026-07-01", "AAPL"]
    sql_f, args_f = f.where_sql(table_alias="f")
    assert "date(f.ts_et) >= ?" in sql_f
    assert args_f == args


def test_unknown_warning_printed():
    conn = _conn()
    _ins(conn, dte=None, rank=1, strike=300.0, ret_t1h=0.0, hours_t1h=1.0)
    buf = io.StringIO()
    with redirect_stdout(buf):
        section_buckets(conn, "1=1", [], "t1h")
    assert "UNKNOWN" in buf.getvalue()
    assert "predate the dte column" in buf.getvalue()
