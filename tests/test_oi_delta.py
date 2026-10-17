"""OI change (oi_delta.py) and its page."""

from __future__ import annotations

import math

import pandas as pd
import pytest

import oi_delta as od


def _row(day, side, strike, expiry, oi, vol):
    return {"session_date": day, "side": side, "strike": strike, "expiry": expiry,
            "open_interest": oi, "volume": vol, "last": 1.0, "iv": 0.3}


D1, D2, D3 = "2026-10-14", "2026-10-15", "2026-10-16"
EXP = "2026-10-23"


def _daily():
    return pd.DataFrame([
        _row(D1, "CALL", 345.0, EXP, 900, 50),
        _row(D2, "CALL", 345.0, EXP, 1000, 600), _row(D3, "CALL", 345.0, EXP, 1500, 10),
        _row(D2, "PUT", 335.0, EXP, 2000, 300), _row(D3, "PUT", 335.0, EXP, 1800, 5),
        _row(D2, "PUT", 330.0, EXP, 0, 40), _row(D3, "PUT", 330.0, EXP, 30, 1),
        _row(D2, "CALL", 350.0, EXP, 5000, 100), _row(D3, "CALL", 350.0, EXP, 1000, 1),  # suspect
        _row(D2, "CALL", 360.0, EXP, None, 100), _row(D3, "CALL", 360.0, EXP, 400, 1),   # unknown
        _row(D3, "CALL", 370.0, EXP, 400, 1),                                           # one day
        _row(D2, "CALL", 345.0, "2026-10-15", 700, 900), _row(D3, "CALL", 345.0, "2026-10-15", 10, 0),
    ])


def test_change_is_into_the_latest_session_from_the_one_before():
    t, meta = od.change_table(_daily())
    assert meta == {"session": D3, "previous": D2}
    row = t[(t["side"] == "CALL") & (t["strike"] == 345.0) & (t["expiry"] == EXP)].iloc[0]
    assert (row["oi_before"], row["oi_after"], row["oi_change"]) == (1000, 1500, 500)
    assert row["oi_change_pct"] == pytest.approx(50.0)
    assert row["traded"] == 600 and row["opened_share"] == pytest.approx(500 / 600)
    assert row["dte"] == 7 and not row["suspect"]


def test_missing_oi_or_a_missing_day_is_left_out_never_zero():
    t, _ = od.change_table(_daily())
    strikes = set(zip(t["side"], t["strike"]))
    assert ("CALL", 360.0) not in strikes and ("CALL", 370.0) not in strikes


def test_expired_contracts_are_dropped_and_zero_base_has_no_percent():
    t, _ = od.change_table(_daily())
    assert "2026-10-15" not in set(t["expiry"])
    zero = t[(t["side"] == "PUT") & (t["strike"] == 330.0)].iloc[0]
    assert zero["oi_change"] == 30 and math.isnan(zero["oi_change_pct"])


def test_a_change_larger_than_that_days_volume_is_flagged_and_left_out_of_totals():
    t, _ = od.change_table(_daily())
    bad = t[(t["side"] == "CALL") & (t["strike"] == 350.0)].iloc[0]
    assert bad["suspect"] and bad["oi_change"] == -4000
    h = od.headline(t)
    assert h["suspect"] == 1
    assert h["call_change"] == 500                # the suspect -4000 is not summed
    assert h["put_change"] == -200 + 30
    assert h["top_call"]["strike"] == 345.0 and h["top_put"]["strike"] == 330.0


def test_rows_are_sorted_by_size_of_change_either_sign():
    t, _ = od.change_table(_daily())
    assert list(t["oi_change"].abs()) == sorted(t["oi_change"].abs(), reverse=True)


def test_an_earlier_session_can_be_chosen():
    t, meta = od.change_table(_daily(), session=D2)
    assert meta == {"session": D2, "previous": D1}
    assert list(t["oi_change"]) == [100]


def test_too_few_sessions_gives_an_empty_table():
    one = _daily()[_daily()["session_date"] == D3]
    t, meta = od.change_table(one)
    assert t.empty and meta["previous"] is None
    assert od.change_table(pd.DataFrame())[0].empty
    assert od.change_table(_daily(), session=D1)[0].empty


def test_filters_by_dte_band_and_size():
    t, _ = od.change_table(_daily())
    assert set(od.filtered(t, spot=345.0, max_dte=5)["dte"]) == set()
    near = od.filtered(t, spot=345.0, band_pct=2.0)
    assert set(near["strike"]) == {345.0, 350.0, 340.0} & set(near["strike"])
    assert (od.filtered(t, spot=345.0, min_change=200)["oi_change"].abs() >= 200).all()


def test_by_strike_sums_sides_and_keeps_unknown_as_nan():
    t, _ = od.change_table(_daily())
    p = od.by_strike(t)
    assert list(p.columns) == ["CALL", "PUT"] and list(p.index) == sorted(p.index, reverse=True)
    assert p.loc[345.0, "CALL"] == 500 and math.isnan(p.loc[345.0, "PUT"])


def test_menu_has_the_page():
    from ui import shell
    assert {e.id: e.title for e in shell.entries()}["oi_delta"] == "OI Change"



def test_page_chart_leaves_suspect_rows_out():
    from ui.pages import oi_delta as page
    t, _ = od.change_table(_daily())
    rows = page.chart_rows(t)
    assert not rows["suspect"].any() and len(rows) == len(t) - 1
    assert 350.0 not in set(rows["strike"])          # the impossible -4000 is not drawn
