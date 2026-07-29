"""EOD scan mode: volume convergence, run_kind, overdue exclusion."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from eod_settlement import (
    VolumeSnapshot,
    await_volume_convergence,
    check_convergence_pair,
    is_eod_slot,
    scan_window_end_et,
)
from mark_runner import count_overdue_t1h

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


# ── Convergence ───────────────────────────────────────────────────────────────

def test_convergence_pair_requires_gap_and_equal_vols():
    a = VolumeSnapshot(100, 50)
    b = VolumeSnapshot(100, 50)
    assert check_convergence_pair(a, b, elapsed_sec=600, min_gap_sec=600) is True
    assert check_convergence_pair(a, b, elapsed_sec=599, min_gap_sec=600) is False
    assert check_convergence_pair(
        a, VolumeSnapshot(100, 51), elapsed_sec=600, min_gap_sec=600
    ) is False


def test_await_converges_on_second_matching_read():
    seq = [
        VolumeSnapshot(10, 5),
        VolumeSnapshot(10, 5),  # match after gap
    ]
    sleeps: list[float] = []
    clock = {"t": 0.0}

    def mono():
        return clock["t"]

    def sleep(sec):
        sleeps.append(sec)
        clock["t"] += sec

    it = iter(seq)

    converged, last, snaps = await_volume_convergence(
        lambda: next(it),
        gap_sec=600.0,
        max_attempts=3,
        sleep_fn=sleep,
        monotonic=mono,
    )
    assert converged is True
    assert last == VolumeSnapshot(10, 5)
    assert len(snaps) == 2
    assert sleeps == [600.0]


def test_await_writes_unconverged_after_three_attempts():
    seq = [
        VolumeSnapshot(1, 1),
        VolumeSnapshot(2, 2),
        VolumeSnapshot(3, 3),
    ]
    clock = {"t": 0.0}

    def mono():
        return clock["t"]

    def sleep(sec):
        clock["t"] += sec

    it = iter(seq)
    converged, last, snaps = await_volume_convergence(
        lambda: next(it),
        gap_sec=10.0,
        max_attempts=3,
        sleep_fn=sleep,
        monotonic=mono,
    )
    assert converged is False
    assert last == VolumeSnapshot(3, 3)
    assert len(snaps) == 3


def test_eod_time_configurable_not_hardcoded():
    assert scan_window_end_et({"eod_time": "16:20"}).hour == 16
    assert scan_window_end_et({"eod_time": "16:20"}).minute == 20
    assert scan_window_end_et({"eod_time": "16:45"}).minute == 45
    # Fallback when eod_time omitted
    end = scan_window_end_et({"market_close": "16:00", "post_close_buffer_min": 20})
    assert (end.hour, end.minute) == (16, 20)
    assert is_eod_slot(_et(2026, 7, 20, 16, 20), {"eod_time": "16:20"}) is True
    assert is_eod_slot(_et(2026, 7, 20, 16, 19), {"eod_time": "16:20"}) is False


# ── run_kind persisted; EOD skips flags ───────────────────────────────────────

def test_log_run_persists_run_kind_and_skips_eod_flags(tmp_path):
    from attribution import log_run, _db

    db = str(tmp_path / "attr.db")
    scored = pd.DataFrame(
        [
            {
                "Type": "CALL",
                "Strike": 100.0,
                "Expiry": "2026-08-15",
                "Value_Score": 1.5,
                "Mid": 1.0,
                "Bid": 0.9,
                "Ask": 1.1,
                "nLev": 0.5,
                "nFlow": 0.5,
                "Base_Score": 1.0,
                "_multipliers": {"_base": 1.0},
                "DTE": 20,
                "Vol": 100,
                "OI": 50,
                "IV": 0.3,
            }
        ]
    )
    cfg = {"w_lev": 0.4, "w_flow": 0.6}

    rid_eod = log_run(
        ticker="AAPL",
        scored_df=scored,
        cfg=cfg,
        spot=100.0,
        db_path=db,
        ts_et=_et(2026, 7, 20, 16, 20),
        run_kind="eod",
    )
    rid_intra = log_run(
        ticker="AAPL",
        scored_df=scored,
        cfg=cfg,
        spot=100.0,
        db_path=db,
        ts_et=_et(2026, 7, 20, 12, 0),
        run_kind="intraday",
    )

    with _db(db) as conn:
        kinds = {
            r[0]: r[1]
            for r in conn.execute("SELECT run_id, run_kind FROM runs").fetchall()
        }
        assert kinds[rid_eod] == "eod"
        assert kinds[rid_intra] == "intraday"
        n_eod_flags = conn.execute(
            "SELECT COUNT(*) FROM flags WHERE run_id = ?", (rid_eod,)
        ).fetchone()[0]
        n_intra_flags = conn.execute(
            "SELECT COUNT(*) FROM flags WHERE run_id = ?", (rid_intra,)
        ).fetchone()[0]
    assert n_eod_flags == 0
    assert n_intra_flags >= 1


def test_invalid_run_kind_rejected(tmp_path):
    from attribution import log_run

    with pytest.raises(ValueError, match="run_kind"):
        log_run(
            ticker="AAPL",
            scored_df=pd.DataFrame(),
            cfg={},
            spot=1.0,
            db_path=str(tmp_path / "x.db"),
            run_kind="weekly",
        )


# ── EOD excluded from overdue ─────────────────────────────────────────────────

def test_eod_flags_excluded_from_overdue_count(tmp_path):
    """Even if a legacy EOD flag existed, overdue must ignore run_kind=eod."""
    import sqlite3

    db = str(tmp_path / "od.db")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            run_kind TEXT
        );
        CREATE TABLE flags (
            flag_id INTEGER PRIMARY KEY,
            run_id TEXT,
            ts_et TEXT NOT NULL,
            mark_t1h REAL,
            notes TEXT
        );
        """
    )
    # Mid-day intraday — overdue at 15:00
    conn.execute("INSERT INTO runs VALUES ('r1', 'intraday')")
    conn.execute(
        "INSERT INTO flags VALUES (1, 'r1', ?, NULL, NULL)",
        (_et(2026, 7, 20, 10, 0).isoformat(timespec="seconds"),),
    )
    # Same ts but EOD run — must NOT count
    conn.execute("INSERT INTO runs VALUES ('r2', 'eod')")
    conn.execute(
        "INSERT INTO flags VALUES (2, 'r2', ?, NULL, NULL)",
        (_et(2026, 7, 20, 10, 0).isoformat(timespec="seconds"),),
    )
    # n/a:eod note — must NOT count
    conn.execute("INSERT INTO runs VALUES ('r3', 'intraday')")
    conn.execute(
        "INSERT INTO flags VALUES (3, 'r3', ?, NULL, 'n/a:eod')",
        (_et(2026, 7, 20, 10, 0).isoformat(timespec="seconds"),),
    )
    conn.commit()

    as_of = _et(2026, 7, 20, 15, 0)
    assert count_overdue_t1h(conn, as_of=as_of) == 1
    conn.close()


def test_health_within_et_window():
    from health_check import within_et_window

    assert within_et_window("16:30", window_min=20, now=_et(2026, 7, 20, 16, 35))
    assert not within_et_window("16:30", window_min=20, now=_et(2026, 7, 20, 12, 0))
