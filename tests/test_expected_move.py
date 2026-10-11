"""Expected move: the at-the-money straddle of the nearest expiries, fixed for the day."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

import expected_move as em
import volume_history as vh

ROOT = Path(__file__).resolve().parents[1]
FRI, MON, WED = "2026-10-09", "2026-10-12", "2026-10-14"


def _leg(side, strike, expiry, bid, ask):
    return {"side": side, "strike": strike, "expiry": expiry, "bid": bid, "ask": ask}


def _chain(*legs):
    return pd.DataFrame(list(legs))


# ── the straddle ────────────────────────────────────────────────────────────
def test_straddle_is_call_mid_plus_put_mid_at_the_nearest_strike():
    chain = _chain(_leg("CALL", 335.0, MON, 2.00, 2.10), _leg("PUT", 335.0, MON, 1.40, 1.50),
                   _leg("CALL", 337.5, MON, 0.90, 1.00), _leg("PUT", 337.5, MON, 2.80, 2.90))
    out = em.atm_straddle(chain, 335.6, MON)
    assert out["strike"] == 335.0 and out["move"] == pytest.approx(2.05 + 1.45)
    assert em.atm_straddle(chain, 337.0, MON)["strike"] == 337.5


def test_a_strike_without_a_two_sided_quote_is_skipped():
    chain = _chain(_leg("CALL", 335.0, MON, 0.0, 2.10), _leg("PUT", 335.0, MON, 1.40, 1.50),
                   _leg("CALL", 337.5, MON, 0.90, 1.00), _leg("PUT", 337.5, MON, 2.80, 2.90))
    assert em.atm_straddle(chain, 335.1, MON)["strike"] == 337.5      # 335 has no call bid
    crossed = _chain(_leg("CALL", 335.0, MON, 2.2, 2.1), _leg("PUT", 335.0, MON, 1.4, 1.5))
    assert em.atm_straddle(crossed, 335.0, MON) is None


def test_straddle_reads_only_the_expiry_asked_for():
    chain = _chain(_leg("CALL", 335.0, MON, 2.0, 2.1), _leg("PUT", 335.0, MON, 1.4, 1.5),
                   _leg("CALL", 335.0, WED, 3.0, 3.2), _leg("PUT", 335.0, WED, 2.4, 2.6))
    assert em.atm_straddle(chain, 335.0, WED)["move"] == pytest.approx(3.1 + 2.5)
    assert em.atm_straddle(chain, 335.0, "2026-10-16") is None
    assert em.atm_straddle(pd.DataFrame(), 335.0, MON) is None
    assert em.atm_straddle(chain, 0, MON) is None


# ── which snapshot the band is set from ─────────────────────────────────────
@pytest.fixture
def store(tmp_path):
    db = tmp_path / "v.db"
    with sqlite3.connect(db) as con:
        con.executescript(vh.SCHEMA if hasattr(vh, "SCHEMA") else vh._SCHEMA)
    arch = tmp_path / "archive"
    arch.mkdir()

    def add(scan_id, ts, spot, legs, ticker="AAPL"):
        with sqlite3.connect(db) as con:
            for leg in legs:
                con.execute(
                    "INSERT INTO contract_scans (ticker, scan_id, ts_et, session_date, side,"
                    " strike, expiry, bid, ask) VALUES (?,?,?,?,?,?,?,?,?)",
                    (ticker, scan_id, ts, ts[:10], leg["side"], leg["strike"], leg["expiry"],
                     leg["bid"], leg["ask"]))
        if spot is not None:
            (arch / f"{scan_id}.json").write_text(json.dumps({"spot": spot}))

    return str(db), str(arch), add


def _legs(strike, expiry, call_mid, put_mid):
    return [_leg("CALL", strike, expiry, call_mid - 0.05, call_mid + 0.05),
            _leg("PUT", strike, expiry, put_mid - 0.05, put_mid + 0.05)]


def test_band_comes_from_the_previous_sessions_last_snapshot_by_the_close(store):
    db, arch, add = store
    add("AAPL_20261009_150000", f"{FRI}T15:00:00-04:00", 338.0, _legs(337.5, MON, 3.0, 2.5))
    add("AAPL_20261009_154800", f"{FRI}T15:48:00-04:00", 336.6,
        _legs(337.5, MON, 1.50, 2.40) + _legs(337.5, WED, 2.40, 3.30) + _legs(337.5, FRI, 0.1, 1.0))
    add("AAPL_20261009_163000", f"{FRI}T16:30:00-04:00", 336.6, _legs(337.5, MON, 9.0, 9.0))
    add("AAPL_20261012_100000", f"{MON}T10:00:00-04:00", 330.0, _legs(330.0, MON, 0.5, 0.5))
    moves = em.expected_moves("aapl", date(2026, 10, 12), db_path=db, archive_dir=arch)
    assert [m["expiry"] for m in moves] == [MON, WED]             # Friday's has expired
    first = moves[0]
    assert first["move"] == 3.90 and first["anchor"] == 336.6
    assert (first["low"], first["high"]) == (332.70, 340.50)
    assert first["as_of"].strftime("%H:%M") == "15:48" and first["same_day"] is False
    assert moves[1]["move"] == 5.70


def test_a_weekend_rescan_is_not_a_session(store):
    db, arch, add = store
    add("AAPL_20261009_154800", f"{FRI}T15:48:00-04:00", 336.6, _legs(337.5, MON, 1.5, 2.4))
    add("AAPL_20261011_120000", "2026-10-11T12:00:00-04:00", 336.6, _legs(337.5, MON, 5.0, 5.0))
    moves = em.expected_moves("AAPL", date(2026, 10, 12), db_path=db, archive_dir=arch)
    assert moves[0]["move"] == 3.90


def test_first_snapshot_today_when_there_is_no_earlier_session(store):
    db, arch, add = store
    add("SPY_20261012_095000", f"{MON}T09:50:00-04:00", 740.0, _legs(740.0, MON, 2.0, 1.9),
        ticker="SPY")
    add("SPY_20261012_101500", f"{MON}T10:15:00-04:00", 742.0, _legs(742.0, MON, 1.0, 1.0),
        ticker="SPY")
    moves = em.expected_moves("SPY", date(2026, 10, 12), db_path=db, archive_dir=arch)
    assert moves[0]["move"] == 3.90 and moves[0]["same_day"] is True
    assert "today's first snapshot at 09:50" in em.caption(moves)


def test_nothing_is_drawn_without_a_stored_spot_or_a_database(store, tmp_path):
    db, arch, add = store
    add("AAPL_20261009_154800", f"{FRI}T15:48:00-04:00", None, _legs(337.5, MON, 1.5, 2.4))
    assert em.expected_moves("AAPL", date(2026, 10, 12), db_path=db, archive_dir=arch) == []
    assert em.expected_moves("AAPL", date(2026, 10, 12), db_path=str(tmp_path / "no.db")) == []
    assert em.expected_moves("NVDA", date(2026, 10, 12), db_path=db, archive_dir=arch) == []


# ── what the chart gets ─────────────────────────────────────────────────────
def test_chart_levels_and_caption(store):
    db, arch, add = store
    add("AAPL_20261009_154800", f"{FRI}T15:48:00-04:00", 336.6,
        _legs(337.5, MON, 1.50, 2.40) + _legs(337.5, WED, 2.40, 3.30))
    moves = em.expected_moves("AAPL", date(2026, 10, 12), db_path=db, archive_dir=arch)
    levels = em.chart_levels(moves)
    assert [(lv["name"], lv["price"]) for lv in levels] == [
        ("Exp move Oct 12 high", 340.5), ("Exp move Oct 12 low", 332.7),
        ("Exp move Oct 14 high", 342.3), ("Exp move Oct 14 low", 330.9)]
    assert levels[0]["color"] == levels[1]["color"] != levels[2]["color"]
    assert {"key", "name", "price", "color"} <= set(levels[0])
    text = em.caption(moves)
    assert text.startswith("Expected move: Oct 12 ±$3.90 ($332.70 to $340.50) · Oct 14 ±$5.70")
    assert "Fri Oct 9 15:48 snapshot" in text and "centred on $336.60" in text
    assert em.caption([]) == "" and em.chart_levels([]) == []


def test_module_needs_no_app_code_and_no_network():
    src = (ROOT / "expected_move.py").read_text()
    assert "streamlit" not in src and "requests" not in src and "yfinance" not in src
