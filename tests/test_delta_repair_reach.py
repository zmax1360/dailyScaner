"""scripts/delta_repair_reach.py — read-only replay of the engine-v1.5 delta repair."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import volume_history
from config import SCORING
from greeks import bs_price
from scoring_pool import POOL_0DTE, POOL_1DTE

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "delta_repair_reach.py"
_spec = importlib.util.spec_from_file_location("delta_repair_reach", _SCRIPT)
reach = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(reach)

SPOT = 333.0
R = float(SCORING["risk_free_rate"])
SESSION = "2026-10-02"
SCAN = "AAPL_20261002_100000"
TS = "2026-10-02T10:00:00-04:00"   # 6h to the close -> 0.25 days for 0DTE


def _row(side, strike, expiry, t_days, *, vendor_iv, true_iv=0.30, volume=5000,
         scan_id=SCAN, quote=True):
    p = bs_price(side, SPOT, strike, t_days, true_iv, r=R)
    bid, ask = (p - 0.03, p + 0.03) if quote else (None, None)
    return ("AAPL", scan_id, TS, SESSION, side, strike, expiry, volume, 1000.0,
            bid, ask, p, vendor_iv, "yahoo")


@pytest.fixture
def world(tmp_path):
    db = tmp_path / "volume_history.db"
    arch = tmp_path / "archive"
    arch.mkdir()
    (arch / f"{SCAN}.json").write_text(json.dumps({"spot": SPOT}))
    rows = [
        _row("CALL", SPOT / 1.01, SESSION, 0.25, vendor_iv=0.08),        # 0DTE junk -> repaired
        _row("CALL", 335.0, SESSION, 0.25, vendor_iv=0.30),              # 0DTE consistent
        _row("CALL", SPOT / 1.05, SESSION, 0.25, vendor_iv=0.30),        # legit saturation
        _row("PUT", 330.0, SESSION, 0.25, vendor_iv=None),               # no vendor IV
        _row("CALL", SPOT / 1.02, "2026-10-05", 3.0, vendor_iv=0.05),    # 1DTE+ junk -> repaired
        _row("CALL", 340.0, "2026-10-05", 3.0, vendor_iv=0.30),          # 1DTE+ consistent
        _row("CALL", 336.0, "2026-10-05", 3.0, vendor_iv=0.08, quote=False),  # no quote
        _row("CALL", 337.0, "2026-10-05", 3.0, vendor_iv=0.08, volume=10),    # below min volume
        _row("CALL", 338.0, "2026-10-05", 3.0, vendor_iv=0.08, scan_id="AAPL_20261002_101500"),
    ]
    with closing(volume_history._connect(str(db))) as con, con:
        con.executemany(
            "INSERT INTO contract_scans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
    return db, arch


def _run(world):
    db, arch = world
    return reach.replay(str(db), str(arch), ticker="AAPL", since=None, min_volume=500)


def test_pools_are_reported_separately(world):
    res = _run(world)
    assert set(res["pools"]) == {POOL_0DTE, POOL_1DTE}
    assert res["pools"][POOL_0DTE]["rows"] == 4
    assert res["pools"][POOL_1DTE]["rows"] == 3


def test_statuses_per_pool(world):
    res = _run(world)
    assert res["pools"][POOL_0DTE]["status"] == {
        reach.REPAIRED: 1, reach.UNCHANGED: 2, reach.NO_VENDOR_DELTA: 1,
    }
    # No usable quote -> vendor IV kept -> counted unchanged.
    assert res["pools"][POOL_1DTE]["status"] == {reach.REPAIRED: 1, reach.UNCHANGED: 2}


def test_repaired_junk_rows_leave_saturation_and_legit_one_stays(world):
    res = _run(world)
    assert res["pools"][POOL_0DTE]["unsaturated"] == 1
    assert res["pools"][POOL_1DTE]["unsaturated"] == 1
    assert len(res["pools"][POOL_0DTE]["contracts_touched"]) == 1
    assert all(0 < s < 1 for p in res["pools"].values() for s in p["abs_shift"])


def test_rows_without_spot_or_volume_are_skipped_not_defaulted(world):
    res = _run(world)
    assert res["skipped"]["below_min_volume"] == 1
    assert res["skipped"]["no_spot"] == 1      # scan with no archive file


def test_repair_needed_but_unsolvable_is_counted_as_dropped():
    status, d0, d1 = reach.classify(
        "CALL", SPOT, 333.0, 7.0, 0.30, SPOT + 1.0, SPOT + 2.0, R
    )
    assert status == reach.DROPPED and d0 is not None and d1 is None


def test_database_is_opened_read_only_and_left_untouched(world):
    db, _ = world
    before = db.read_bytes()
    _run(world)
    assert db.read_bytes() == before
    with closing(sqlite3.connect(db)) as con:
        assert con.execute("SELECT COUNT(*) FROM contract_scans").fetchone()[0] == 9


def test_replay_does_not_touch_scoring_config(world):
    snapshot = dict(SCORING)
    _run(world)
    assert SCORING == snapshot
    assert "delta_iv_source" not in SCORING


def test_report_renders_and_cli_handles_missing_db(world, tmp_path, capsys):
    text = reach.format_report(_run(world), ticker="AAPL", min_volume=500)
    assert "== 0DTE ==" in text and "== 1DTE+ ==" in text
    assert reach.main(["--db", str(tmp_path / "absent.db")]) == 2
    db, arch = world
    assert reach.main(["--db", str(db), "--archive-dir", str(arch)]) == 0
    assert "Delta repair reach" in capsys.readouterr().out
