"""Gamma exposure: calculation (gex.py) and the Gamma page (gex_page.py)."""

from __future__ import annotations

import math
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import gex
import gex_page
import volume_history as vh
from config import SCORING
from zero_dte_gex import bs_gamma

ET = ZoneInfo("America/New_York")
SPOT = 333.0
AS_OF = datetime(2026, 10, 2, 10, 0, tzinfo=ET)       # Friday, 6h to the close
TODAY, MONDAY, NEXT_FRI, LATER = "2026-10-02", "2026-10-05", "2026-10-09", "2026-10-16"


def _c(side, strike, expiry, oi, iv=0.30, volume=1000):
    return {"side": side, "strike": strike, "expiry": expiry, "volume": volume,
            "open_interest": oi, "bid": 1.0, "ask": 1.1, "last": 1.05, "iv": iv,
            "ts_et": AS_OF.isoformat(), "scan_id": "AAPL_20261002_100000"}


def _latest(rows):
    return pd.DataFrame(rows)


def _table(rows, **kw):
    kw.setdefault("iv_source", gex.IV_VENDOR)      # formula tests pin the vendor IV
    return gex.gex_table(_latest(rows), spot=SPOT, as_of=kw.pop("as_of", AS_OF), **kw)


# ── time ────────────────────────────────────────────────────────────────────

def test_years_to_expiry_counts_the_hours_left_today():
    assert gex.years_to_expiry(TODAY, AS_OF) == pytest.approx(6 / (365 * 24))
    # Friday 10:00 -> Monday 16:00 is 3 days + 6 hours, not a flat 3 days.
    assert gex.years_to_expiry(MONDAY, AS_OF) == pytest.approx((3 * 24 + 6) / (365 * 24))


def test_years_to_expiry_floors_just_before_the_close():
    late = datetime(2026, 10, 2, 15, 59, tzinfo=ET)
    assert gex.years_to_expiry(TODAY, late) == pytest.approx(15 / (365 * 24 * 60))


@pytest.mark.parametrize("as_of", [
    datetime(2026, 10, 2, 16, 0, tzinfo=ET),      # at the close
    datetime(2026, 10, 2, 16, 5, tzinfo=ET),      # end-of-day scan
    datetime(2026, 10, 3, 14, 0, tzinfo=ET),      # next day
])
def test_years_to_expiry_is_none_once_expired(as_of):
    assert gex.years_to_expiry(TODAY, as_of) is None


def test_years_to_expiry_rejects_bad_dates_and_naive_datetimes():
    assert gex.years_to_expiry("not-a-date", AS_OF) is None
    with pytest.raises(ValueError):
        gex.years_to_expiry(TODAY, AS_OF.replace(tzinfo=None))


# ── per-contract GEX ────────────────────────────────────────────────────────

def _gamma(strike, expiry, iv=0.30):
    return bs_gamma(SPOT, strike, iv, t_years=gex.years_to_expiry(expiry, AS_OF),
                    r=float(SCORING["risk_free_rate"]))


def test_default_unit_is_dollars_per_one_dollar_move():
    t = _table([_c("CALL", 335.0, MONDAY, 2000)])
    g = _gamma(335.0, MONDAY)
    assert t.loc[0, "gamma"] == pytest.approx(g)
    assert t.loc[0, "gex"] == pytest.approx(g * 2000 * 100 * SPOT)
    assert t.loc[0, "gex"] > 0 and t.loc[0, "dte"] == 3


def test_percent_unit_is_spot_over_100_times_the_dollar_unit():
    usd = _table([_c("CALL", 335.0, MONDAY, 2000)]).loc[0, "gex"]
    pct = _table([_c("CALL", 335.0, MONDAY, 2000)], unit=gex.UNIT_PCT).loc[0, "gex"]
    assert pct == pytest.approx(_gamma(335.0, MONDAY) * 2000 * 100 * SPOT * SPOT * 0.01)
    assert pct / usd == pytest.approx(SPOT / 100)


def test_shares_unit_is_the_dollar_unit_divided_by_spot():
    usd = _table([_c("CALL", 335.0, MONDAY, 2000)]).loc[0, "gex"]
    shares = _table([_c("CALL", 335.0, MONDAY, 2000)], unit=gex.UNIT_SHARES).loc[0, "gex"]
    assert shares == pytest.approx(_gamma(335.0, MONDAY) * 2000 * 100)
    assert usd / shares == pytest.approx(SPOT)


def test_unknown_unit_is_rejected():
    with pytest.raises(ValueError):
        _table([_c("CALL", 335.0, MONDAY, 2000)], unit="furlongs")


def test_puts_are_negative_and_mirror_calls():
    t = _table([_c("CALL", 335.0, MONDAY, 2000), _c("PUT", 335.0, MONDAY, 2000)])
    call, put = t.loc[t.side == "CALL", "gex"].iloc[0], t.loc[t.side == "PUT", "gex"].iloc[0]
    assert put < 0 and put == pytest.approx(-call)      # same strike, same IV -> same gamma


def test_gex_scales_linearly_with_open_interest():
    a = _table([_c("CALL", 335.0, MONDAY, 5000)])
    b = _table([_c("CALL", 335.0, MONDAY, 1000)])
    assert a.loc[0, "gex"] == pytest.approx(5 * b.loc[0, "gex"])


@pytest.mark.parametrize("oi, iv", [(None, 0.30), (float("nan"), 0.30), (2000, None),
                                    (2000, float("nan")), (2000, 0.0), (2000, 0.001)])
def test_missing_inputs_are_nan_never_zero(oi, iv):
    t = _table([_c("CALL", 335.0, MONDAY, oi, iv=iv)])
    assert len(t) == 1 and math.isnan(t.loc[0, "gex"])
    assert gex.coverage(t) == {"contracts": 1, "used": 0, "excluded": 1, "iv_from_quote": 0}


def test_zero_open_interest_is_a_real_zero():
    t = _table([_c("CALL", 335.0, MONDAY, 0)])
    assert t.loc[0, "gex"] == 0.0 and gex.coverage(t)["used"] == 1


def test_no_spot_gives_an_empty_table():
    for bad in (None, 0.0, float("nan")):
        assert gex.gex_table(_latest([_c("CALL", 335.0, MONDAY, 2000)]),
                             spot=bad, as_of=AS_OF).empty


def test_expiry_dead_at_the_snapshot_is_dropped_not_shown_as_zeros():
    """Regression: Friday's end-of-day snapshot must not show Friday's expiry."""
    eod = datetime(2026, 10, 2, 16, 5, tzinfo=ET)
    t = _table([_c("CALL", 335.0, TODAY, 9000), _c("CALL", 335.0, MONDAY, 2000)], as_of=eod)
    assert set(t["expiry"]) == {MONDAY}


def test_expiry_before_today_is_dropped_when_a_snapshot_is_viewed_later():
    """Regression: a Friday-morning snapshot viewed on Saturday hides Friday's expiry."""
    rows = [_c("CALL", 335.0, TODAY, 9000), _c("CALL", 335.0, MONDAY, 2000)]
    assert set(_table(rows)["expiry"]) == {TODAY, MONDAY}
    assert set(_table(rows, today=date(2026, 10, 3))["expiry"]) == {MONDAY}


# ── IV source ───────────────────────────────────────────────────────────────

def _quoted(side, strike, expiry, oi, *, vendor_iv, true_iv):
    """Contract whose bid/ask is centred on the BS price at ``true_iv``."""
    from greeks import bs_price

    t_days = gex.years_to_expiry(expiry, AS_OF) * 365.0
    p = bs_price(side, SPOT, strike, t_days, true_iv, r=float(SCORING["risk_free_rate"]))
    row = _c(side, strike, expiry, oi, iv=vendor_iv)
    row.update(bid=p - 0.02, ask=p + 0.02)
    return row


def test_quote_iv_is_the_default():
    row = _quoted("CALL", 335.0, MONDAY, 2000, vendor_iv=0.15, true_iv=0.30)
    t = gex.gex_table(_latest([row]), spot=SPOT, as_of=AS_OF)
    assert bool(t.loc[0, "iv_from_quote"])


def test_vendor_mode_ignores_quotes():
    t = _table([_quoted("CALL", 335.0, MONDAY, 2000, vendor_iv=0.15, true_iv=0.30)])
    assert t.loc[0, "iv"] == 0.15 and not t.loc[0, "iv_from_quote"]


def test_quote_iv_recovers_the_market_iv_and_changes_gamma():
    row = _quoted("CALL", 335.0, MONDAY, 2000, vendor_iv=0.15, true_iv=0.30)
    vendor = _table([row])
    quote = _table([row], iv_source=gex.IV_QUOTE)
    assert quote.loc[0, "iv"] == pytest.approx(0.30, abs=2e-3)
    assert bool(quote.loc[0, "iv_from_quote"])
    assert quote.loc[0, "gamma"] == pytest.approx(_gamma(335.0, MONDAY, iv=0.30), rel=1e-2)
    assert quote.loc[0, "gex"] != pytest.approx(vendor.loc[0, "gex"])
    assert gex.coverage(quote)["iv_from_quote"] == 1


def test_unquoted_strike_keeps_vendor_iv_and_never_borrows_from_neighbours():
    """Regression: neighbour IV overstated far-wing gamma several-fold (352.5 / 350)."""
    unquoted = _c("CALL", 350.0, MONDAY, 1000, iv=0.22)
    unquoted.update(bid=0.0, ask=0.05)                       # no bid: not a usable quote
    rows = [
        _quoted("CALL", 347.5, MONDAY, 1000, vendor_iv=0.10, true_iv=0.34),
        unquoted,
        _quoted("CALL", 355.0, MONDAY, 1000, vendor_iv=0.10, true_iv=0.90),   # penny wing
    ]
    t = _table(rows, iv_source=gex.IV_QUOTE).set_index("strike")
    assert t.loc[350.0, "iv"] == 0.22 and not t.loc[350.0, "iv_from_quote"]
    assert t.loc[350.0, "gex"] == pytest.approx(
        _gamma(350.0, MONDAY, iv=0.22) * 1000 * 100 * SPOT)


@pytest.mark.parametrize("bid, ask", [(None, None), (0.0, 1.0), (1.2, 1.0),
                                      (SPOT + 1.0, SPOT + 2.0)])     # last: no IV fits
def test_quote_mode_falls_back_to_vendor_without_a_usable_quote(bid, ask):
    row = _c("CALL", 335.0, MONDAY, 2000, iv=0.25)
    row.update(bid=bid, ask=ask)
    t = _table([row], iv_source=gex.IV_QUOTE)
    assert t.loc[0, "iv"] == 0.25 and not t.loc[0, "iv_from_quote"]
    assert not math.isnan(t.loc[0, "gex"])


def test_unknown_iv_source_is_rejected():
    with pytest.raises(ValueError):
        _table([_c("CALL", 335.0, MONDAY, 2000)], iv_source="guess")


# ── expiries, matrix and walls ──────────────────────────────────────────────

def _chain():
    return [
        _c("CALL", 335.0, MONDAY, 9000), _c("PUT", 335.0, MONDAY, 1000),
        _c("CALL", 332.5, MONDAY, 3000), _c("PUT", 332.5, MONDAY, 500),
        _c("CALL", 327.5, MONDAY, 200), _c("PUT", 327.5, MONDAY, 6000),
        _c("CALL", 340.0, NEXT_FRI, 4000),
        _c("PUT", 325.0, NEXT_FRI, 5000),
        _c("CALL", 400.0, NEXT_FRI, 99999),               # far from spot
        _c("CALL", 336.0, NEXT_FRI, None),                # unusable
        _c("CALL", 335.0, LATER, 1000),
    ]


def test_available_expiries_and_current_week():
    table = _table(_chain())
    assert gex.available_expiries(table) == [MONDAY, NEXT_FRI, LATER]
    assert gex.current_week([MONDAY, NEXT_FRI, LATER]) == [MONDAY, NEXT_FRI]
    assert gex.current_week([]) == [] and gex.available_expiries(pd.DataFrame()) == []
    only_unusable = _table([_c("CALL", 336.0, MONDAY, None)])
    assert gex.available_expiries(only_unusable) == []


def test_matrix_nets_calls_and_puts_per_strike_and_expiry():
    table = _table(_chain())
    m = gex.gex_matrix(table, spot=SPOT, n_strikes=None)
    assert list(m.columns) == [MONDAY, NEXT_FRI, LATER]             # nearest first
    assert list(m.index) == sorted(m.index, reverse=True)           # strikes descending
    assert 336.0 not in m.index                                     # unusable contract
    want = table[(table.strike == 335.0) & (table.expiry == MONDAY)]["gex"].sum()
    assert m.loc[335.0, MONDAY] == pytest.approx(want)
    assert m.loc[327.5, MONDAY] < 0 < m.loc[335.0, MONDAY]


def test_matrix_cell_without_a_usable_contract_is_nan_not_zero():
    m = gex.gex_matrix(_table(_chain()), spot=SPOT, n_strikes=None)
    assert math.isnan(m.loc[340.0, MONDAY]) and math.isnan(m.loc[332.5, NEXT_FRI])


def test_matrix_keeps_the_n_strikes_closest_to_spot():
    table = _table(_chain())
    m = gex.gex_matrix(table, spot=SPOT, n_strikes=3)
    assert list(m.index) == [335.0, 332.5, 327.5]
    assert 400.0 in gex.gex_matrix(table, spot=SPOT, n_strikes=None).index
    assert 400.0 not in gex.gex_matrix(table, spot=SPOT, n_strikes=5).index


def test_matrix_expiry_filter():
    table = _table(_chain())
    assert list(gex.gex_matrix(table, spot=SPOT, expiries=[NEXT_FRI]).columns) == [NEXT_FRI]
    assert gex.gex_matrix(table, spot=SPOT, expiries=["2027-01-15"]).empty
    assert gex.gex_matrix(pd.DataFrame(), spot=SPOT).empty


def test_walls_and_net_per_expiry():
    m = gex.gex_matrix(_table(_chain()), spot=SPOT, n_strikes=None)
    w = gex.walls(m)
    assert w[MONDAY]["call_wall"] == 335.0 and w[MONDAY]["put_wall"] == 327.5
    assert w[MONDAY]["net"] == pytest.approx(m[MONDAY].sum())
    assert w[LATER]["put_wall"] is None                              # no puts there


def test_nearest_strike_and_money_format():
    m = gex.gex_matrix(_table(_chain()), spot=SPOT)
    assert gex.nearest_strike(m, SPOT) == 332.5
    assert gex.nearest_strike(pd.DataFrame(), SPOT) is None
    assert gex.fmt_money(13_940_000) == "13.94M"
    assert gex.fmt_money(-399_264) == "-399,264"
    assert gex.fmt_money(8354) == "8,354"
    assert gex.fmt_money(1.5e9) == "1.50B"
    assert gex.fmt_money(-0.2) == "0" and gex.fmt_money(0.0) == "0"   # never "-0"
    assert gex.fmt_money(float("nan")) == "" and gex.fmt_money(None) == ""


def test_gex_is_not_a_scoring_input():
    import inspect

    import best_value
    import strategy_engine

    for mod in (best_value, strategy_engine):
        src = inspect.getsource(mod)
        assert "import gex" not in src and "from gex" not in src


# ── page helpers ────────────────────────────────────────────────────────────

def test_pick_expiries_modes():
    av = [MONDAY, NEXT_FRI, LATER]
    assert gex_page.pick_expiries(gex_page.EXP_CURRENT, av, None) == [MONDAY]
    assert gex_page.pick_expiries(gex_page.EXP_WEEK, av, None) == [MONDAY, NEXT_FRI]
    assert gex_page.pick_expiries(gex_page.EXP_ALL, av, None) == av
    assert gex_page.pick_expiries(gex_page.EXP_PICK, av, [LATER]) == [LATER]
    assert gex_page.pick_expiries(gex_page.EXP_PICK, av, []) == [MONDAY]   # nothing picked yet


def test_style_matrix_marks_walls_spot_and_net_row():
    m = gex.gex_matrix(_table(_chain()), spot=SPOT, n_strikes=None)
    styler = gex_page.style_matrix(m, gex.walls(m), spot_strike=332.5)
    assert styler.data.index[-1] == gex_page.NET_LABEL
    assert styler.data.loc[gex_page.NET_LABEL, MONDAY] == pytest.approx(m[MONDAY].sum())
    html = styler.to_html()
    assert "rgb(250, 204, 21)" in html and "rgb(45, 212, 191)" in html    # call / put wall
    assert "◀ spot" in html
    assert gex_page._cell_css(float("nan"), 1.0) == ""


# ── page ────────────────────────────────────────────────────────────────────

_SCRIPT = """
import streamlit as st, volume_history as vh, gex_page
from datetime import date
from zoneinfo import ZoneInfo
vh.DB_PATH = {db!r}
gex_page.render_gex_page('AAPL', tz=ZoneInfo('America/New_York'), spot={spot!r},
                         today=date(2026, 10, 3))
"""


def _leg(rows):
    cols = ["strike", "lastPrice", "volume", "openInterest", "impliedVolatility", "bid", "ask",
            "expiry"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows])


@pytest.fixture
def seeded(tmp_path):
    """Friday's end-of-day snapshot, viewed on Saturday (the reported case)."""
    db = str(tmp_path / "vh.db")
    calls = _leg([(335.0, 0.01, 5000, 9000, 0.30, 0.01, 0.02, TODAY),      # expired
                  (335.0, 1.2, 5000, 9000, 0.30, 1.15, 1.25, MONDAY),
                  (332.5, 2.4, 4000, 3000, 0.30, 2.35, 2.45, MONDAY),
                  (340.0, 1.9, 2000, 4000, 0.28, 1.85, 1.95, NEXT_FRI),
                  (335.0, 4.0, 1500, 1000, 0.27, 3.95, 4.05, LATER)])
    puts = _leg([(327.5, 0.4, 3000, 6000, 0.33, 0.38, 0.42, MONDAY),
                 (325.0, 1.1, 1500, 5000, 0.31, 1.05, 1.15, NEXT_FRI)])
    vh.record_scan(calls, puts, ticker="AAPL", scan_id="AAPL_20261002_160500",
                   ts=datetime(2026, 10, 2, 16, 5, tzinfo=ET), db_path=db)
    return db


def _run(db, spot=SPOT):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_SCRIPT.format(db=db, spot=spot)).run()
    assert not at.exception, at.exception
    return at


def test_page_empty_state_without_a_snapshot(tmp_path):
    at = _run(str(tmp_path / "none.db"))
    assert any("No chain snapshot" in i.value for i in at.info)


class _Table(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    """Reads the gamma map's HTML table: column headings and {row label: [cells]}."""

    def __init__(self):
        super().__init__()
        self.columns, self.rows, self.styles = [], {}, ""
        self._cell = self._row = None
        self._in_style = False

    def handle_starttag(self, tag, attrs):
        cls = dict(attrs).get("class", "")
        if tag == "style":
            self._in_style = True
        elif tag == "tr":
            self._row = {"label": None, "cells": []}
        elif tag in ("th", "td"):
            kind = ("col" if "col_heading" in cls else "label" if "row_heading" in cls
                    else "cell" if tag == "td" else "skip")
            self._cell = [kind, ""]

    def handle_data(self, data):
        if self._in_style:
            self.styles += data
        elif self._cell is not None:
            self._cell[1] += data

    def handle_endtag(self, tag):
        if tag == "style":
            self._in_style = False
        elif tag in ("th", "td") and self._cell is not None:
            kind, text = self._cell
            if kind == "col":
                self.columns.append(text)
            elif kind == "label":
                self._row["label"] = text
            elif kind == "cell":
                self._row["cells"].append(text)
            self._cell = None
        elif tag == "tr" and self._row and self._row["label"] is not None:
            self.rows[self._row["label"]] = self._row["cells"]


def _map(at) -> _Table:
    """The gamma map drawn on the page (exactly one)."""
    tables = [m.value for m in at.markdown if "<table" in m.value]
    assert len(tables) == 1, len(tables)
    parsed = _Table()
    parsed.feed(tables[0])
    return parsed


def _view(db, **kw):
    """The page's numbers, without Streamlit, for the seeded snapshot viewed on Saturday."""
    latest = vh.latest_scan("AAPL", db_path=db)
    as_of = datetime.fromisoformat(str(latest["ts_et"].iloc[0]))
    return gex_page.build_view(latest, spot=SPOT, as_of=as_of, today=date(2026, 10, 3), **kw)


def test_page_refuses_without_spot(seeded):
    at = _run(seeded, spot=None)
    assert any("No spot price" in i.value for i in at.info)
    assert not any("<table" in m.value for m in at.markdown)


def test_page_default_view_is_current_week_without_the_dead_expiry(seeded):
    at = _run(seeded)
    shown = _map(at)
    assert shown.columns == ["Oct 5", "Oct 9"]                # no Oct 2, no later week
    labels = list(shown.rows)
    assert labels[-1] == gex_page.NET_LABEL
    assert sum("◀ spot" in label for label in labels) == 1
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Spot"] == "$333.00"
    assert metrics["Call wall · Oct 5"] == "$335"
    assert metrics["Put wall · Oct 5"] == "$327.5"
    assert "-0" not in {c for cells in shown.rows.values() for c in cells}


def test_map_values_are_centred_in_their_cells(seeded):
    """The request: numbers in the middle of the cell, not pushed left or right."""
    shown = _map(_run(seeded))
    css = shown.styles.replace(" ", "")
    assert "#T_gextd{text-align:center;" in css
    assert "#T_gexth{text-align:center;" in css
    assert len(_run(seeded).dataframe) == 0                   # no right-aligning grid widget


def test_map_html_is_safe_for_markdown_and_keeps_wall_colours(seeded):
    view = _view(seeded)
    styled = gex_page.style_matrix(view["matrix"], view["walls"], spot_strike=335.0)
    html = gex_page.matrix_html(styled, ["Oct 5", "Oct 9"])
    assert "\n" not in html                                   # no line can become a code block
    assert html.startswith("<div") and html.endswith("</table></div>")
    assert "rgb(250, 204, 21)" in html and "rgb(45, 212, 191)" in html     # call / put wall


def test_page_expiration_filter(seeded):
    at = _run(seeded)
    at.radio(key="gex_exp_mode").set_value(gex_page.EXP_CURRENT).run()
    assert _map(at).columns == ["Oct 5"]
    at.radio(key="gex_exp_mode").set_value(gex_page.EXP_ALL).run()
    assert _map(at).columns == ["Oct 5", "Oct 9", "Oct 16"]
    at.radio(key="gex_exp_mode").set_value(gex_page.EXP_PICK).run()
    at.multiselect(key="gex_exp_pick").set_value([LATER]).run()
    assert not at.exception
    assert _map(at).columns == ["Oct 16"]


def test_page_strike_count_filter(seeded):
    at = _run(seeded)
    at.radio(key="gex_exp_mode").set_value(gex_page.EXP_ALL).run()
    assert len(_map(at).rows) - 1 == 5                          # minus the NET row
    at.radio(key="gex_strikes").set_value("All").run()
    assert len(_map(at).rows) - 1 == 5
    assert len(_view(seeded, mode=gex_page.EXP_ALL, strikes="8")["matrix"]) == 5


def test_view_expiry_modes_and_dead_expiry(seeded):
    assert _view(seeded)["available"] == [MONDAY, NEXT_FRI, LATER]         # no 2026-10-02
    assert list(_view(seeded)["matrix"].columns) == [MONDAY, NEXT_FRI]
    assert list(_view(seeded, mode=gex_page.EXP_CURRENT)["matrix"].columns) == [MONDAY]
    assert list(_view(seeded, mode=gex_page.EXP_PICK, picked=[LATER])["matrix"].columns) == [LATER]


def test_view_units_rescale_the_same_map(seeded):
    usd = _view(seeded)["walls"][MONDAY]["net"]
    pct = _view(seeded, unit=gex.UNIT_PCT)["walls"][MONDAY]["net"]
    shares = _view(seeded, unit=gex.UNIT_SHARES)["walls"][MONDAY]["net"]
    assert pct / usd == pytest.approx(SPOT / 100)
    assert usd / shares == pytest.approx(SPOT)
    for unit in gex.UNITS:                                    # walls do not depend on the unit
        w = _view(seeded, unit=unit)["walls"][MONDAY]
        assert (w["call_wall"], w["put_wall"]) == (335.0, 327.5)


def test_page_unit_toggle_redraws_the_map(seeded):
    at = _run(seeded)
    before = _map(at).rows[gex_page.NET_LABEL]
    at.radio(key="gex_unit").set_value("Dollars per 1%").run()
    assert not at.exception
    assert _map(at).rows[gex_page.NET_LABEL] != before


def test_page_iv_source_toggle(seeded):
    at = _run(seeded)
    assert any("IV from quotes on" in c.value for c in at.caption)        # the default
    at.radio(key="gex_iv_source").set_value("Vendor").run()
    assert not at.exception
    assert not any("IV from quotes" in c.value for c in at.caption)


def test_page_caption_escapes_dollar_signs(seeded):
    """Regression: two bare $ in the caption rendered as LaTeX math."""
    at = _run(seeded)
    cap = next(c.value for c in at.caption if "Snapshot" in c.value)
    assert "dollars of dealer hedging per \\$1 move" in cap
    assert cap.count("$") == cap.count("\\$")


def test_page_shares_unit_relabels_the_net_row_and_caption(seeded):
    at = _run(seeded)
    at.radio(key="gex_unit").set_value("Shares per $1").run()
    assert not at.exception
    assert list(_map(at).rows)[-1] == "NET sh"
    cap = next(c.value for c in at.caption if "Snapshot" in c.value)
    assert "shares of dealer hedging per \\$1 move" in cap
