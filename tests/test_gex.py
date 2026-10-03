"""Gamma exposure: calculation (gex.py) and the Gamma page (gex_page.py)."""

from __future__ import annotations

import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import gex
import volume_history as vh
from config import SCORING
from zero_dte_gex import bs_gamma

ET = ZoneInfo("America/New_York")
SPOT = 333.0
AS_OF = datetime(2026, 10, 2, 10, 0, tzinfo=ET)       # Friday, 6h to the close
TODAY, MONDAY = "2026-10-02", "2026-10-05"


def _c(side, strike, expiry, oi, iv=0.30, volume=1000):
    return {"side": side, "strike": strike, "expiry": expiry, "volume": volume,
            "open_interest": oi, "bid": 1.0, "ask": 1.1, "last": 1.05, "iv": iv,
            "ts_et": AS_OF.isoformat(), "scan_id": "AAPL_20261002_100000"}


def _latest(rows):
    return pd.DataFrame(rows)


# ── time ────────────────────────────────────────────────────────────────────

def test_years_to_expiry_counts_the_hours_left_today():
    same_day = gex.years_to_expiry(TODAY, AS_OF)
    assert same_day == pytest.approx(6 / (365 * 24))
    monday = gex.years_to_expiry(MONDAY, AS_OF)
    assert monday == pytest.approx((3 * 24 + 6) / (365 * 24))    # not a flat 3 days


def test_years_to_expiry_floors_near_the_close_and_rejects_the_past():
    late = datetime(2026, 10, 2, 15, 59, tzinfo=ET)
    assert gex.years_to_expiry(TODAY, late) == pytest.approx(15 / (365 * 24 * 60))
    assert gex.years_to_expiry("2026-10-01", AS_OF) is None
    assert gex.years_to_expiry("not-a-date", AS_OF) is None


def test_years_to_expiry_refuses_naive_datetimes():
    with pytest.raises(ValueError):
        gex.years_to_expiry(TODAY, AS_OF.replace(tzinfo=None))


# ── per-contract GEX ────────────────────────────────────────────────────────

def test_gex_matches_the_formula_and_calls_are_positive():
    t = gex.gex_table(_latest([_c("CALL", 335.0, MONDAY, 2000)]), spot=SPOT, as_of=AS_OF)
    g = bs_gamma(SPOT, 335.0, 0.30, t_years=gex.years_to_expiry(MONDAY, AS_OF),
                 r=float(SCORING["risk_free_rate"]))
    assert t.loc[0, "gamma"] == pytest.approx(g)
    assert t.loc[0, "gex"] == pytest.approx(g * 2000 * 100 * SPOT * SPOT * 0.01)
    assert t.loc[0, "gex"] > 0
    assert t.loc[0, "dte"] == 3


def test_puts_are_negative_and_mirror_calls():
    t = gex.gex_table(
        _latest([_c("CALL", 335.0, MONDAY, 2000), _c("PUT", 335.0, MONDAY, 2000)]),
        spot=SPOT, as_of=AS_OF,
    )
    call, put = t.loc[t.side == "CALL", "gex"].iloc[0], t.loc[t.side == "PUT", "gex"].iloc[0]
    assert put < 0 and put == pytest.approx(-call)      # same strike, same IV -> same gamma


def test_gex_scales_linearly_with_open_interest():
    a = gex.gex_table(_latest([_c("CALL", 335.0, MONDAY, 5000)]), spot=SPOT, as_of=AS_OF)
    b = gex.gex_table(_latest([_c("CALL", 335.0, MONDAY, 1000)]), spot=SPOT, as_of=AS_OF)
    assert a.loc[0, "gex"] == pytest.approx(5 * b.loc[0, "gex"])


@pytest.mark.parametrize("oi, iv", [(None, 0.30), (float("nan"), 0.30), (2000, None),
                                    (2000, float("nan")), (2000, 0.0), (2000, 0.001)])
def test_missing_inputs_are_nan_never_zero(oi, iv):
    t = gex.gex_table(_latest([_c("CALL", 335.0, MONDAY, oi, iv=iv)]), spot=SPOT, as_of=AS_OF)
    assert len(t) == 1 and math.isnan(t.loc[0, "gex"])
    assert gex.coverage(t) == {"contracts": 1, "used": 0, "excluded": 1}


def test_zero_open_interest_is_a_real_zero():
    t = gex.gex_table(_latest([_c("CALL", 335.0, MONDAY, 0)]), spot=SPOT, as_of=AS_OF)
    assert t.loc[0, "gex"] == 0.0
    assert gex.coverage(t)["used"] == 1


def test_expired_contracts_are_dropped_and_no_spot_gives_an_empty_table():
    t = gex.gex_table(_latest([_c("CALL", 335.0, "2026-10-01", 2000)]), spot=SPOT, as_of=AS_OF)
    assert t.empty
    for bad in (None, 0.0, float("nan")):
        assert gex.gex_table(_latest([_c("CALL", 335.0, MONDAY, 2000)]),
                             spot=bad, as_of=AS_OF).empty


# ── matrix and walls ────────────────────────────────────────────────────────

def _chain():
    return _latest([
        _c("CALL", 335.0, TODAY, 9000), _c("PUT", 335.0, TODAY, 1000),
        _c("CALL", 332.5, TODAY, 3000), _c("PUT", 332.5, TODAY, 500),
        _c("CALL", 327.5, TODAY, 200), _c("PUT", 327.5, TODAY, 6000),
        _c("CALL", 340.0, MONDAY, 4000),
        _c("PUT", 325.0, MONDAY, 5000),
        _c("CALL", 400.0, MONDAY, 99999),                 # outside the strike window
        _c("CALL", 336.0, MONDAY, None),                  # unusable
    ])


def test_matrix_nets_calls_and_puts_per_strike_and_expiry():
    table = gex.gex_table(_chain(), spot=SPOT, as_of=AS_OF)
    m = gex.gex_matrix(table, spot=SPOT, window_pct=0.06, max_expiries=4)
    assert list(m.columns) == [TODAY, MONDAY]                      # nearest first
    assert list(m.index) == sorted(m.index, reverse=True)          # strikes descending
    assert 400.0 not in m.index and 336.0 not in m.index
    want = table[(table.strike == 335.0) & (table.expiry == TODAY)]["gex"].sum()
    assert m.loc[335.0, TODAY] == pytest.approx(want)
    assert m.loc[327.5, TODAY] < 0 < m.loc[335.0, TODAY]


def test_matrix_cell_without_a_usable_contract_is_nan_not_zero():
    m = gex.gex_matrix(gex.gex_table(_chain(), spot=SPOT, as_of=AS_OF), spot=SPOT)
    assert math.isnan(m.loc[340.0, TODAY])
    assert math.isnan(m.loc[335.0, MONDAY])


def test_matrix_respects_window_and_expiry_limit():
    table = gex.gex_table(_chain(), spot=SPOT, as_of=AS_OF)
    one = gex.gex_matrix(table, spot=SPOT, max_expiries=1)
    assert list(one.columns) == [TODAY]
    wide = gex.gex_matrix(table, spot=SPOT, window_pct=0.25)
    assert 400.0 in wide.index
    assert gex.gex_matrix(pd.DataFrame(), spot=SPOT).empty


def test_walls_and_net_per_expiry():
    m = gex.gex_matrix(gex.gex_table(_chain(), spot=SPOT, as_of=AS_OF), spot=SPOT)
    w = gex.walls(m)
    assert w[TODAY]["call_wall"] == 335.0
    assert w[TODAY]["put_wall"] == 327.5
    assert w[TODAY]["net"] == pytest.approx(m[TODAY].sum())
    assert w[MONDAY] == {"call_wall": 340.0, "put_wall": 325.0,
                         "net": pytest.approx(m[MONDAY].sum())}


def test_walls_are_none_when_one_side_is_absent():
    m = gex.gex_matrix(
        gex.gex_table(_latest([_c("CALL", 335.0, MONDAY, 2000)]), spot=SPOT, as_of=AS_OF),
        spot=SPOT,
    )
    assert gex.walls(m)[MONDAY]["put_wall"] is None


def test_nearest_strike_and_money_format():
    m = gex.gex_matrix(gex.gex_table(_chain(), spot=SPOT, as_of=AS_OF), spot=SPOT)
    assert gex.nearest_strike(m, SPOT) == 332.5
    assert gex.nearest_strike(pd.DataFrame(), SPOT) is None
    assert gex.fmt_money(25_400_000) == "25.40M"
    assert gex.fmt_money(-399_216) == "-399K"
    assert gex.fmt_money(8356) == "8,356"
    assert gex.fmt_money(1.5e9) == "1.50B"
    assert gex.fmt_money(float("nan")) == "" and gex.fmt_money(None) == ""


def test_gex_is_not_a_scoring_input():
    import inspect

    import best_value
    import strategy_engine

    for mod in (best_value, strategy_engine):
        src = inspect.getsource(mod)
        assert "import gex" not in src and "from gex" not in src


# ── page ────────────────────────────────────────────────────────────────────

_SCRIPT = """
import streamlit as st, volume_history as vh, gex_page
from zoneinfo import ZoneInfo
vh.DB_PATH = {db!r}
gex_page.render_gex_page('AAPL', tz=ZoneInfo('America/New_York'), spot={spot!r})
"""


def _leg(rows):
    cols = ["strike", "lastPrice", "volume", "openInterest", "impliedVolatility", "bid", "ask",
            "expiry"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows])


@pytest.fixture
def seeded(tmp_path):
    db = str(tmp_path / "vh.db")
    calls = _leg([(335.0, 1.2, 5000, 9000, 0.30, 1.15, 1.25, TODAY),
                  (332.5, 2.4, 4000, 3000, 0.30, 2.35, 2.45, TODAY),
                  (340.0, 1.9, 2000, 4000, 0.28, 1.85, 1.95, MONDAY)])
    puts = _leg([(327.5, 0.4, 3000, 6000, 0.33, 0.38, 0.42, TODAY),
                 (325.0, 1.1, 1500, 5000, 0.31, 1.05, 1.15, MONDAY)])
    vh.record_scan(calls, puts, ticker="AAPL", scan_id="AAPL_20261002_100000", ts=AS_OF,
                   db_path=db)
    return db


def _run(db, spot=SPOT):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_SCRIPT.format(db=db, spot=spot)).run()
    assert not at.exception, at.exception
    return at


def test_page_empty_state_without_a_snapshot(tmp_path):
    at = _run(str(tmp_path / "none.db"))
    assert any("No chain snapshot" in i.value for i in at.info)


def test_page_refuses_without_spot(seeded):
    at = _run(seeded, spot=None)
    assert any("No spot price" in i.value for i in at.info)
    assert len(at.dataframe) == 0


def test_page_renders_metrics_and_one_matrix(seeded):
    at = _run(seeded)
    labels = {m.label: m.value for m in at.metric}
    assert labels["Spot"] == "$333.00"
    assert labels["Call wall"] == "$335"
    assert labels["Put wall"] == "$327.5"
    assert len(at.dataframe) == 1
    shown = at.dataframe[0].value
    assert list(shown.columns) == [TODAY, MONDAY]
    assert sum("◀ spot" in str(i) for i in shown.index) == 1
    assert any("5 of 5 contracts used" in c.value for c in at.caption)


def test_page_expiry_control_narrows_the_matrix(seeded):
    at = _run(seeded)
    at.radio(key="gex_n_exp").set_value(1).run()
    assert not at.exception
    assert list(at.dataframe[0].value.columns) == [TODAY]


def test_style_matrix_marks_the_wall_and_leaves_missing_cells_unstyled(seeded):
    import gex_page

    table = gex.gex_table(vh.latest_scan("AAPL", db_path=seeded), spot=SPOT, as_of=AS_OF)
    m = gex.gex_matrix(table, spot=SPOT)
    html = gex_page.style_matrix(m, gex.walls(m), spot_strike=332.5).to_html()
    assert "rgb(250, 204, 21)" in html                       # wall highlight
    assert "◀ spot" in html
    assert gex_page._cell_css(float("nan"), 1.0) == ""
