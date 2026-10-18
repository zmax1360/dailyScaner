"""Contract over time (contract_view.py) and its page."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import contract_view as cv
import gex
from config import SCORING
from greeks import bs_delta, bs_price

ET = ZoneInfo("America/New_York")
EXP = "2026-10-23"
K = 345.0
R = float(SCORING.get("risk_free_rate", 0.045))
T0 = datetime(2026, 10, 19, 10, 0, tzinfo=ET)


def _px(spot, iv, ts, side="CALL"):
    days = gex.years_to_expiry(EXP, ts) * 365.0
    return bs_price(side, spot, K, days, iv, r=R)


def _raw(points, side="CALL", spread=0.0):
    """points: (ts, spot, iv). Quotes are the exact BS price (+/- half the spread)."""
    rows, spots = [], {}
    for i, (ts, spot, iv) in enumerate(points):
        p = _px(spot, iv, ts, side)
        sid = f"AAPL_{i}"
        spots[sid] = spot
        rows.append({"ts_et": ts.isoformat(), "scan_id": sid, "volume": 100.0,
                     "open_interest": 1000.0, "bid": p - spread / 2, "ask": p + spread / 2,
                     "last": p, "iv": 0.99})
    return pd.DataFrame(rows), spots


def _hist(points, side="CALL", **kw):
    raw, spots = _raw(points, side, **kw)
    return cv.history(raw, side=side, strike=K, expiry=EXP, spot_of=spots.get)


def test_iv_is_solved_from_the_mid_and_greeks_follow_it():
    h = _hist([(T0, 343.0, 0.25)])
    row = h.iloc[0]
    assert row["iv_from"] == "solved" and row["iv"] == pytest.approx(0.25, abs=1e-4)
    days = gex.years_to_expiry(EXP, T0) * 365.0
    assert row["delta"] == pytest.approx(bs_delta("CALL", 343.0, K, days, 0.25, r=R), abs=1e-4)
    assert row["gamma"] > 0 and row["theta"] < 0 and row["vega"] > 0
    assert row["price_from"] == "mid"


def test_time_alone_is_all_theta():
    h = _hist([(T0, 343.0, 0.25), (T0 + timedelta(hours=3), 343.0, 0.25)])
    a = cv.attribution(h, side="CALL", strike=K)
    assert a["change"] < 0
    assert a["theta"] == pytest.approx(a["change"], abs=1e-4)
    assert abs(a["delta"]) < 1e-9 and abs(a["gamma"]) < 1e-9 and abs(a["vega"]) < 1e-4


def test_iv_alone_is_all_vega():
    t1 = T0 + timedelta(seconds=1)
    h = _hist([(T0, 343.0, 0.25), (t1, 343.0, 0.30)])
    a = cv.attribution(h, side="CALL", strike=K)
    assert a["vega"] == pytest.approx(a["change"], rel=1e-3) and a["change"] > 0


def test_a_stock_move_splits_into_delta_and_gamma_and_everything_adds_up():
    pts = [(T0, 343.0, 0.25), (T0 + timedelta(minutes=15), 346.0, 0.24),
           (T0 + timedelta(minutes=30), 341.0, 0.27)]
    a = cv.attribution(_hist(pts), side="CALL", strike=K)
    total = sum(a[k] for k in cv.TERMS)
    assert total == pytest.approx(a["change"], abs=1e-9)
    assert abs(a["residual"]) < 1e-3
    assert a["gamma"] > 0                       # a long option gains from the swing
    assert a["steps"] == 2


def test_window_selects_snapshots_and_needs_two():
    pts = [(T0 + timedelta(minutes=15 * i), 343.0 + i, 0.25) for i in range(4)]
    h = _hist(pts)
    a = cv.attribution(h, side="CALL", strike=K, start=pts[1][0], end=pts[2][0])
    assert a["steps"] == 1 and a["start_ts"] == pts[1][0]
    assert cv.attribution(h, side="CALL", strike=K, start=pts[3][0]) is None
    assert cv.attribution(pd.DataFrame(), side="CALL", strike=K) is None


def test_missing_spot_gives_no_greeks_and_rows_after_expiry_are_dropped():
    raw, spots = _raw([(T0, 343.0, 0.25)])
    h = cv.history(raw, side="CALL", strike=K, expiry=EXP, spot_of=lambda _: None)
    assert math.isnan(h.iloc[0]["spot"]) and math.isnan(h.iloc[0]["delta"])
    late = datetime(2026, 10, 23, 16, 30, tzinfo=ET)
    raw2 = raw.assign(ts_et=late.isoformat())
    assert cv.history(raw2, side="CALL", strike=K, expiry=EXP, spot_of=spots.get).empty


def test_unsolvable_price_falls_back_to_vendor_iv_flagged():
    raw, spots = _raw([(T0, 360.0, 0.25)])                          # 15 in the money
    raw.loc[0, ["bid", "ask", "last"]] = [1.0, 1.0, 1.0]            # below intrinsic
    raw.loc[0, "iv"] = 0.3
    h = cv.history(raw, side="CALL", strike=K, expiry=EXP, spot_of=spots.get)
    assert h.iloc[0]["iv_from"] == "vendor" and h.iloc[0]["iv"] == pytest.approx(0.3)


def test_one_sided_quote_uses_last_and_says_so():
    raw, spots = _raw([(T0, 343.0, 0.25)])
    raw.loc[0, "bid"] = 0.0
    h = cv.history(raw, side="CALL", strike=K, expiry=EXP, spot_of=spots.get)
    assert h.iloc[0]["price_from"] == "last"


def test_archive_spot_reads_the_scan_and_tolerates_bad_files(tmp_path):
    (tmp_path / "AAPL_1.json").write_text('{"spot": 343.46}')
    (tmp_path / "AAPL_2.json").write_text('{"spot": 1}{"spot": 2}')       # corrupt
    assert cv.archive_spot("AAPL_1", str(tmp_path)) == 343.46
    assert cv.archive_spot("AAPL_2", str(tmp_path)) is None
    assert cv.archive_spot("AAPL_3", str(tmp_path)) is None


def test_page_default_strike_and_menu():
    from ui import shell
    from ui.pages import contract as page
    assert page.default_strike([340.0, 342.5, 345.0], 343.46) == 342.5
    assert page.default_strike([], 343.0) is None
    assert {e.id: e.title for e in shell.entries()}["contract"] == "Contract"


def test_page_draws_one_chart_per_series():
    from ui.pages import contract as page
    h = _hist([(T0, 343.0, 0.25), (T0 + timedelta(minutes=15), 344.0, 0.26)])
    figs = page.charts(h)
    assert [f.layout.title.text for f in figs] == [
        "Option price", "Stock price", "Delta", "Gamma", "Implied volatility"]
    assert all(len(f.data) == 1 for f in figs)
    assert list(figs[4].data[0].y) == pytest.approx([25.0, 26.0], abs=0.01)   # IV in %
