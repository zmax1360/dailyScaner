"""SQLite must not reinterpret offset-aware ET timestamps via date()/time()."""

from __future__ import annotations

import sqlite3

from eod_report import et_clock_sql, session_date_sql


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE t (ts_et TEXT)")
    return c


def test_et_time_extracted_without_shift():
    conn = _conn()
    conn.execute("INSERT INTO t VALUES (?)", ("2026-07-31T16:19:37-04:00",))
    clock = conn.execute(
        f"SELECT {et_clock_sql('ts_et')} FROM t"
    ).fetchone()[0]
    broken = conn.execute("SELECT time(ts_et) FROM t").fetchone()[0]
    assert clock == "16:19:37"
    assert broken == "20:19:37"  # documents the SQLite pitfall we avoid


def test_session_date_not_shifted():
    conn = _conn()
    # 19:30 EDT → 23:30 UTC same calendar day; 20:30 EDT → next UTC day.
    conn.executemany(
        "INSERT INTO t VALUES (?)",
        [
            ("2026-07-31T19:30:00-04:00",),
            ("2026-07-31T20:30:00-04:00",),
        ],
    )
    rows = conn.execute(
        f"""
        SELECT ts_et,
               {session_date_sql('ts_et')} AS sess,
               date(ts_et) AS sqlite_date
        FROM t ORDER BY ts_et
        """
    ).fetchall()
    assert rows[0][1] == "2026-07-31"
    assert rows[1][1] == "2026-07-31"
    # Prove date() would mis-bucket the post-20:00 ET scan
    assert rows[1][2] == "2026-08-01"

    # GROUP BY session date must keep both under 2026-07-31
    grouped = conn.execute(
        f"""
        SELECT {session_date_sql('ts_et')} d, COUNT(*) n
        FROM t GROUP BY d
        """
    ).fetchall()
    assert grouped == [("2026-07-31", 2)]


def test_dst_boundary():
    conn = _conn()
    conn.executemany(
        "INSERT INTO t VALUES (?)",
        [
            ("2026-07-31T16:19:37-04:00",),  # EDT
            ("2026-01-15T16:19:37-05:00",),  # EST
            ("2026-01-15T19:30:00-05:00",),  # would shift under date()
        ],
    )
    rows = conn.execute(
        f"""
        SELECT ts_et,
               {session_date_sql('ts_et')},
               {et_clock_sql('ts_et')}
        FROM t ORDER BY ts_et
        """
    ).fetchall()
    by_ts = {r[0]: (r[1], r[2]) for r in rows}
    assert by_ts["2026-07-31T16:19:37-04:00"] == ("2026-07-31", "16:19:37")
    assert by_ts["2026-01-15T16:19:37-05:00"] == ("2026-01-15", "16:19:37")
    assert by_ts["2026-01-15T19:30:00-05:00"] == ("2026-01-15", "19:30:00")
    # SQLite date() would put the EST 19:30 row on the 16th
    assert conn.execute(
        "SELECT date('2026-01-15T19:30:00-05:00')"
    ).fetchone()[0] == "2026-01-16"
