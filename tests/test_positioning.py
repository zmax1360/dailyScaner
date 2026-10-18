"""Volume vs open interest by strike (positioning.py) and its chart."""

from __future__ import annotations

import math

import pandas as pd

import positioning as pos

EXP = "2026-10-23"


def _latest():
    return pd.DataFrame([
        {"side": "CALL", "strike": 345.0, "expiry": EXP, "volume": 6000.0, "open_interest": 4000.0},
        {"side": "PUT", "strike": 345.0, "expiry": EXP, "volume": 100.0, "open_interest": 900.0},
        {"side": "CALL", "strike": 350.0, "expiry": EXP, "volume": 800.0, "open_interest": None},
        {"side": "PUT", "strike": 340.0, "expiry": EXP, "volume": 300.0, "open_interest": 2000.0},
        {"side": "CALL", "strike": 345.0, "expiry": "2026-10-30", "volume": 9.0, "open_interest": 9.0},
    ])


def test_by_strike_one_expiry_missing_stays_nan():
    t = pos.by_strike(_latest(), EXP)
    assert list(t.index) == [340.0, 345.0, 350.0]
    assert t.loc[345.0, "call_volume"] == 6000 and t.loc[345.0, "put_oi"] == 900
    assert math.isnan(t.loc[350.0, "call_oi"]) and math.isnan(t.loc[340.0, "call_volume"])
    assert pos.by_strike(pd.DataFrame(), EXP).empty
    assert pos.by_strike(_latest(), "2027-01-15").empty


def test_by_strike_keeps_nearest_strikes():
    t = pos.by_strike(_latest(), EXP, spot=346.0, n_strikes=2)
    assert list(t.index) == [345.0, 350.0]


def test_new_positioning_flags_volume_at_or_above_oi():
    f = pos.new_positioning(pos.by_strike(_latest(), EXP), min_volume=500)
    got = {(r.side, r.strike) for r in f.itertuples()}
    assert got == {("CALL", 345.0), ("CALL", 350.0)}          # 350: OI unknown, volume big
    assert f.set_index(["side", "strike"]).loc[("CALL", 345.0), "vol_oi"] == 1.5


def test_chart_has_volume_and_oi_for_both_sides():
    from ui.components import volume_by_strike as vbs
    fig = vbs.figure(pos.by_strike(_latest(), EXP), spot=344.0)
    assert len(fig.data) == 4
    assert {t.name for t in fig.data} == {"Call open interest", "Call volume today",
                                          "Put open interest", "Put volume today"}
