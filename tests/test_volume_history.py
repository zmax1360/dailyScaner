"""Full-chain volume/OI recorder (volume_history)."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import volume_history as vh

ET = ZoneInfo("America/New_York")


def _leg(rows):
    cols = ["strike", "lastPrice", "volume", "openInterest", "impliedVolatility", "bid", "ask", "expiry", "dte"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows])


CALLS = _leg([
    (340.0, 4.60, 1200, 5000, 0.203, 4.45, 4.65, "2026-10-02", 5),
    (350.0, 0.98, 800, float("nan"), 0.193, 0.95, 0.98, "2026-10-02", 5),   # OI unknown → NULL
    (360.0, 11.85, 0, 900, 0.258, 11.75, 12.0, "2027-01-15", 110),          # no volume → skipped
    (365.0, 9.00, float("nan"), 400, 0.25, 8.9, 9.1, "2027-01-15", 110),    # unknown volume → skipped
])
PUTS = _leg([(335.0, 2.10, 950, 3000, 0.21, 2.05, 2.15, "2026-10-02", 5)])


def _ts(d, hm):
    return datetime.strptime(f"{d} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)


def _rows(db):
    with sqlite3.connect(db) as con:
        return con.execute("SELECT side, strike, expiry, volume, open_interest FROM contract_scans"
                           " ORDER BY side, strike").fetchall()


def test_records_traded_contracts_and_keeps_unknowns_null(tmp_path):
    db = str(tmp_path / "vh.db")
    n = vh.record_scan(CALLS, PUTS, ticker="aapl", scan_id="AAPL_20260928_104500",
                       ts=_ts("2026-09-28", "10:45"), source="yahoo", db_path=db)
    assert n == 3
    rows = _rows(db)
    assert ("CALL", 350.0, "2026-10-02", 800.0, None) in rows      # NaN OI stored as NULL, not 0
    assert not any(r[1] in (360.0, 365.0) for r in rows)             # zero / unknown volume skipped


def test_same_scan_twice_does_not_duplicate(tmp_path):
    db = str(tmp_path / "vh.db")
    kw = dict(ticker="AAPL", scan_id="AAPL_20260928_104500", ts=_ts("2026-09-28", "10:45"), db_path=db)
    vh.record_scan(CALLS, PUTS, **kw)
    vh.record_scan(CALLS, PUTS, **kw)
    assert len(_rows(db)) == 3


def test_recording_never_raises(tmp_path):
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x")
    assert vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id="s", ts=_ts("2026-09-28", "10:45"),
                          db_path=str(blocker / "vh.db")) == 0
    naive = datetime(2026, 9, 28, 10, 45)
    assert vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id="s", ts=naive,
                          db_path=str(tmp_path / "vh.db")) == 0


def test_history_and_daily_summary_use_last_scan_of_each_session(tmp_path):
    db = str(tmp_path / "vh.db")
    for d, hm, vol in [("2026-09-28", "10:00", 300), ("2026-09-28", "15:45", 1200),
                       ("2026-09-29", "10:00", 500)]:
        c = CALLS.copy()
        c.loc[0, "volume"] = vol
        vh.record_scan(c, PUTS, ticker="AAPL", scan_id=f"AAPL_{d}_{hm}", ts=_ts(d, hm), db_path=db)
    hist = vh.contract_history("AAPL", "CALL", 340.0, "2026-10-02", db_path=db)
    assert hist["volume"].tolist() == [300, 1200, 500]
    daily = vh.daily_summary("AAPL", db_path=db)
    c340 = daily[(daily.side == "CALL") & (daily.strike == 340.0)]
    assert c340["volume"].tolist() == [1200, 500]                       # end-of-session, per day


# ── building positions ───────────────────────────────────────────────────────

def _daily(rows):
    return pd.DataFrame(rows, columns=["side", "strike", "expiry", "session_date", "volume",
                                       "open_interest", "last", "iv"])


D = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01"]


def test_rising_oi_is_flagged_as_building():
    rows = [("CALL", 360.0, "2027-01-15", d, 900, oi, 1.0, 0.25) for d, oi in zip(D, [1000, 1400, 1900, 2600])]
    out = vh.building_positions(_daily(rows), min_days=3)
    assert len(out) == 1
    r = out.iloc[0]
    assert r.oi_up_days == 3 and r.oi_change == 1600


@pytest.mark.parametrize("ois", [
    [1000, 1400, 1300, 2600],             # dipped → streak only 1
    [1000, 1000, 1000, 1000],             # flat
    [1000, 1400, float("nan"), 2600],     # unknown breaks the streak
])
def test_flat_falling_or_unknown_oi_is_not_building(ois):
    rows = [("CALL", 360.0, "2027-01-15", d, 900, oi, 1.0, 0.25) for d, oi in zip(D, ois)]
    assert vh.building_positions(_daily(rows), min_days=3).empty


def test_a_day_without_trades_breaks_the_streak():
    other = [("PUT", 300.0, "2026-10-02", d, 50, 10, 1.0, 0.3) for d in D]   # defines the sessions
    rows = [("CALL", 360.0, "2027-01-15", d, 900, oi, 1.0, 0.25)
            for d, oi in [(D[0], 1000), (D[1], 1400), (D[3], 2600)]]         # no row on D[2]
    assert vh.building_positions(_daily(rows + other), min_days=3).empty


# ── scanner integration ──────────────────────────────────────────────────────

def test_scanner_helper_records_rows_with_archive_timestamp(tmp_path, monkeypatch):
    import dailyScaner

    db = str(tmp_path / "vh.db")
    monkeypatch.setattr(vh, "DB_PATH", db)
    dailyScaner._record_volume_history("archive/AAPL_20260928_104502.json", CALLS, PUTS,
                                       source_name="yahoo")
    with sqlite3.connect(db) as con:
        n, ts, scan = con.execute("SELECT COUNT(*), MIN(ts_et), MIN(scan_id) FROM contract_scans").fetchone()
    assert n == 3
    assert ts == "2026-09-28T10:45:02-04:00" and scan == "AAPL_20260928_104502"


def test_volume_history_is_not_a_scoring_input():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for mod in ("best_value.py", "strategy_engine.py", "scoring_pool.py", "config.py"):
        assert "volume_history" not in (root / mod).read_text(), mod


# ── recording throttle ───────────────────────────────────────────────────────

def test_throttle_records_at_most_every_interval_but_always_eod(tmp_path):
    db = str(tmp_path / "vh.db")
    written = []
    for hm in ["10:00", "10:03", "10:06", "10:15", "10:18", "15:57"]:
        ts = _ts("2026-09-28", hm)
        written.append(vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id=f"AAPL_{hm}", ts=ts,
                                      db_path=db, min_interval_min=15))
    assert [w > 0 for w in written] == [True, False, False, True, False, True]
    eod = vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id="AAPL_eod", ts=_ts("2026-09-28", "16:00"),
                         db_path=db, min_interval_min=15, force=True)
    assert eod > 0                                                   # EOD always recorded
    nxt = vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id="AAPL_next", ts=_ts("2026-09-29", "09:45"),
                         db_path=db, min_interval_min=15)
    assert nxt > 0                                                   # new session starts fresh


def test_throttle_is_per_ticker(tmp_path):
    db = str(tmp_path / "vh.db")
    a = vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id="a", ts=_ts("2026-09-28", "10:00"),
                       db_path=db, min_interval_min=15)
    n = vh.record_scan(CALLS, PUTS, ticker="NVDA", scan_id="n", ts=_ts("2026-09-28", "10:03"),
                       db_path=db, min_interval_min=15)
    assert a > 0 and n > 0


def test_scanner_helper_applies_throttle_and_eod_force(tmp_path, monkeypatch):
    import dailyScaner

    db = str(tmp_path / "vh.db")
    monkeypatch.setattr(vh, "DB_PATH", db)
    dailyScaner._record_volume_history("archive/AAPL_20260928_100000.json", CALLS, PUTS, source_name="y")
    dailyScaner._record_volume_history("archive/AAPL_20260928_100300.json", CALLS, PUTS, source_name="y")
    dailyScaner._record_volume_history("archive/AAPL_20260928_100600.json", CALLS, PUTS, source_name="y",
                                       force=True)
    with sqlite3.connect(db) as con:
        scans = [r[0] for r in con.execute("SELECT DISTINCT scan_id FROM contract_scans ORDER BY 1")]
    assert scans == ["AAPL_20260928_100000", "AAPL_20260928_100600"]
