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


def _rec(db, scan_id, ts, rows):
    calls = _leg([r[1:] for r in rows if r[0] == "C"])
    puts = _leg([r[1:] for r in rows if r[0] == "P"]) if any(r[0] == "P" for r in rows) else None
    vh.record_scan(calls, puts, ticker="AAPL", scan_id=scan_id, ts=ts, db_path=db)


def test_volume_bursts_ranks_jumps_and_drops_carryover_and_stale(tmp_path):
    db = str(tmp_path / "vh.db")
    exp = "2026-11-04"
    # previous session closes at 10,000 on the 362.5C
    _rec(db, "AAPL_20261029_160000", _ts("2026-10-29", "16:00"),
         [("C", 362.5, 1.6, 10000, 100, 0.4, 1.55, 1.65, exp, 6)])
    # 09:30 opening scan (ignored): carry-over and a stale pre-open quote
    _rec(db, "AAPL_20261030_093000", _ts("2026-10-30", "09:30"),
         [("C", 362.5, 1.6, 10000, 10100, 0.4, 0.05, 3.30, exp, 5),
          ("C", 355.0, 3.6, 4000, 8000, 0.4, 1.69, 5.60, exp, 5)])
    # 09:45 first real scan: 362.5C still the previous close (carry-over), 355C 4,000
    _rec(db, "AAPL_20261030_094500", _ts("2026-10-30", "09:45"),
         [("C", 362.5, 1.6, 10000, 10100, 0.4, 1.55, 1.65, exp, 5),
          ("C", 355.0, 3.6, 4000, 8000, 0.4, 3.55, 3.65, exp, 5),
          ("C", 300.0, 45.0, 900, 100, 0.4, 44.9, 45.1, exp, 5)])
    # 11:00: 355C +6,000, 362.5C +100
    _rec(db, "AAPL_20261030_110000", _ts("2026-10-30", "11:00"),
         [("C", 362.5, 1.4, 10100, 10100, 0.4, 1.35, 1.45, exp, 5),
          ("C", 355.0, 4.0, 10000, 8000, 0.4, 3.95, 4.05, exp, 5),
          ("C", 300.0, 45.0, 900, 100, 0.4, 44.9, 45.1, exp, 5)])
    b = vh.volume_bursts("AAPL", db_path=db, spot=345.0)
    assert list(zip(b["strike"], b["added"])) == [(355.0, 6000.0), (355.0, 4000.0), (362.5, 100.0)]
    assert b["premium"].iloc[0] == pytest.approx(6000 * 4.0 * 100)
    # deep in-the-money 300C (900 x $45) shows only when the filter is off
    assert 300.0 in vh.volume_bursts("AAPL", db_path=db, spot=345.0, itm_limit=None)["strike"].tolist()
    assert vh.volume_bursts("AAPL", db_path=db, sort_by="added", top=1)["strike"].tolist() == [355.0]


# ── storage window, latest_chain, session anchor ─────────────────────────────

WIN = {"strike_pct": 2.0, "max_dte": 30, "anchor": "session_open"}
FAR = _leg([
    (345.0, 1.0, 50, 10, 0.2, 0.9, 1.1, "2026-12-31", 94),     # inside strikes, too far out
    (300.0, 45.0, 10, 10, 0.4, 44.0, 46.0, "2026-10-02", 4),   # far below spot
])


def _hist(db):
    with sqlite3.connect(db) as con:
        return con.execute("SELECT side, strike, expiry FROM contract_scans ORDER BY side, strike"
                           ).fetchall()


def test_window_keeps_near_strikes_and_short_expiries(tmp_path):
    db = str(tmp_path / "vh.db")
    calls = pd.concat([CALLS, FAR], ignore_index=True)
    n = vh.record_scan(calls, PUTS, ticker="AAPL", scan_id="AAPL_20260928_104500",
                       ts=_ts("2026-09-28", "10:45"), db_path=db, spot=345.0, window_cfg=WIN)
    # ±2% of 345 = 338.10 .. 351.90 and ≤ 30 days: 340C and 350C stay; 335P, 300C, Dec 345C go
    assert n == 2
    assert _hist(db) == [("CALL", 340.0, "2026-10-02"), ("CALL", 350.0, "2026-10-02")]


def test_latest_chain_keeps_the_full_chain(tmp_path):
    db = str(tmp_path / "vh.db")
    calls = pd.concat([CALLS, FAR], ignore_index=True)
    vh.record_scan(calls, PUTS, ticker="AAPL", scan_id="AAPL_20260928_104500",
                   ts=_ts("2026-09-28", "10:45"), db_path=db, spot=345.0, window_cfg=WIN)
    latest = vh.latest_scan("AAPL", db_path=db)
    assert len(latest) == 5                                     # every traded contract
    assert set(latest["strike"]) == {340.0, 350.0, 345.0, 300.0, 335.0}


def test_session_open_anchor_is_kept_all_day(tmp_path):
    db = str(tmp_path / "vh.db")
    vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id="AAPL_20260928_100000",
                   ts=_ts("2026-09-28", "10:00"), db_path=db, spot=345.0, window_cfg=WIN)
    # Spot jumps to 360: with the session-open anchor, 340/350 are still recorded.
    n = vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id="AAPL_20260928_110000",
                       ts=_ts("2026-09-28", "11:00"), db_path=db, spot=360.0, window_cfg=WIN)
    assert n == 2
    with sqlite3.connect(db) as con:
        assert con.execute("SELECT spot FROM session_anchor").fetchall() == [(345.0,)]


def test_each_scan_anchor_moves_with_price(tmp_path):
    db = str(tmp_path / "vh.db")
    win = dict(WIN, anchor="each_scan")
    n = vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id="AAPL_20260928_110000",
                       ts=_ts("2026-09-28", "11:00"), db_path=db, spot=360.0, window_cfg=win)
    assert n == 0                                               # 340/350 are > 2% from 360


def test_skipped_interval_still_refreshes_latest_chain(tmp_path):
    db = str(tmp_path / "vh.db")
    kw = dict(ticker="AAPL", db_path=db, min_interval_min=15, spot=345.0, window_cfg=WIN)
    vh.record_scan(CALLS, PUTS, scan_id="AAPL_20260928_100000", ts=_ts("2026-09-28", "10:00"), **kw)
    n = vh.record_scan(CALLS, PUTS, scan_id="AAPL_20260928_100500", ts=_ts("2026-09-28", "10:05"), **kw)
    assert n == 0                                               # history skipped (< 15 min)
    assert set(vh.latest_scan("AAPL", db_path=db)["scan_id"]) == {"AAPL_20260928_100500"}


def test_older_scan_does_not_overwrite_latest_chain(tmp_path):
    db = str(tmp_path / "vh.db")
    kw = dict(ticker="AAPL", db_path=db, spot=345.0, window_cfg=WIN)
    vh.record_scan(CALLS, PUTS, scan_id="AAPL_20260928_110000", ts=_ts("2026-09-28", "11:00"), **kw)
    vh.record_scan(CALLS, PUTS, scan_id="AAPL_20260928_100000", ts=_ts("2026-09-28", "10:00"), **kw)
    assert set(vh.latest_scan("AAPL", db_path=db)["scan_id"]) == {"AAPL_20260928_110000"}


def test_no_spot_means_no_filtering(tmp_path):
    db = str(tmp_path / "vh.db")
    n = vh.record_scan(CALLS, PUTS, ticker="AAPL", scan_id="AAPL_20260928_104500",
                       ts=_ts("2026-09-28", "10:45"), db_path=db, window_cfg=WIN)
    assert n == 3


def test_config_window_per_ticker_and_default(monkeypatch):
    import config
    monkeypatch.setattr(config, "VOLUME_HISTORY", {
        "anchor": "session_open", "default": {"strike_pct": 10.0, "max_dte": 30},
        "tickers": {"SPY": {"strike_pct": 5.0, "max_dte": 14}}})
    assert vh.window("spy") == {"strike_pct": 5.0, "max_dte": 14, "anchor": "session_open"}
    assert vh.window("IBIT") == {"strike_pct": 10.0, "max_dte": 30, "anchor": "session_open"}


def test_parity_spot_estimates_the_stock_price():
    chain = pd.DataFrame([
        # spot 101: C − P = S − K for each strike
        {"side": "CALL", "strike": 100.0, "expiry": "2026-10-02", "bid": 1.9, "ask": 2.1, "last": 2.0},
        {"side": "PUT", "strike": 100.0, "expiry": "2026-10-02", "bid": 0.9, "ask": 1.1, "last": 1.0},
        {"side": "CALL", "strike": 101.0, "expiry": "2026-10-02", "bid": 1.4, "ask": 1.6, "last": 1.5},
        {"side": "PUT", "strike": 101.0, "expiry": "2026-10-02", "bid": 1.4, "ask": 1.6, "last": 1.5},
        {"side": "CALL", "strike": 102.0, "expiry": "2026-10-02", "bid": 0.9, "ask": 1.1, "last": 1.0},
        {"side": "PUT", "strike": 102.0, "expiry": "2026-10-02", "bid": 1.9, "ask": 2.1, "last": 2.0},
    ])
    assert vh.parity_spot(chain) == pytest.approx(101.0)
    assert vh.parity_spot(pd.DataFrame()) is None


# ── one-time cleanup script ──────────────────────────────────────────────────

def test_cleanup_dry_run_then_apply(tmp_path, monkeypatch):
    import importlib.util
    import json as _json

    import config
    spec = importlib.util.spec_from_file_location(
        "vh_cleanup", vh.os.path.join(vh._BASE, "scripts", "volume_history_cleanup.py"))
    cl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cl)
    monkeypatch.setattr(config, "VOLUME_HISTORY", {
        "anchor": "session_open", "default": {"strike_pct": 2.0, "max_dte": 30}, "tickers": {}})

    db = str(tmp_path / "vh.db")
    calls = pd.concat([CALLS, FAR], ignore_index=True)
    for hm in ("10:00", "11:00"):                              # old-style: no window, no spot
        vh.record_scan(calls, PUTS, ticker="AAPL", scan_id=f"AAPL_20260928_{hm.replace(':', '')}00",
                       ts=_ts("2026-09-28", hm), db_path=db, window_cfg={})
    arch = tmp_path / "archive"
    arch.mkdir()
    (arch / "AAPL_20260928_100000.json").write_text(_json.dumps({"spot": 345.0}))

    assert cl.main(["--db", db, "--archive-dir", str(arch)]) == 0          # dry run
    assert len(_rows(db)) == 10                                            # untouched

    assert cl.main(["--db", db, "--archive-dir", str(arch), "--apply"]) == 0
    assert len(_hist(db)) == 4                                             # 340C + 350C, 2 scans
    assert len(vh.latest_scan("AAPL", db_path=db)) == 5                    # full chain kept
    pruned = tmp_path / "archive" / "volume_history_pruned_AAPL.csv.gz"
    import gzip
    with gzip.open(pruned, "rt") as f:
        assert len(f.read().strip().splitlines()) == 1 + 6                 # header + 3 rows x 2
