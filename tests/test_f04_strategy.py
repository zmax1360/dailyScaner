"""F-04 — bearish strategy multipliers and unknown-bias trap."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
import pytz

from best_value import calculate_best_value
from config import SCORING
from strategy_engine import (
    STRAT_BEAR_PUT_SPREAD,
    STRAT_BULL_CALL_SPREAD,
    STRAT_IRON_CONDOR,
    STRAT_LONG_CALL,
    STRAT_LONG_PUT,
    STRAT_STRADDLE,
    STRAT_UNKNOWN,
    is_straddle_strategy,
    is_unknown_strategy,
    recommend_strategy,
    strategy_outlook,
)

ET = pytz.timezone("US/Eastern")
NOW = ET.localize(datetime(2026, 7, 24, 11, 0))
SPOT = 250.0


def _contract(strike, price, side="CALL", **kw):
    row = {
        "side": side, "strike": float(strike), "expiry": "2026-08-21", "dte": 28,
        "last": float(price), "volume": 5000, "openInterest": 5000, "iv": 0.30,
    }
    row.update(kw)
    return row


def _score(rows, **kw):
    kw.setdefault("now_et", NOW)
    kw.setdefault("spot_price", SPOT)
    out = calculate_best_value(pd.DataFrame(rows), **kw)
    return out.set_index("strike")["Value_Score"]


def _mults(rows, **kw):
    kw.setdefault("now_et", NOW)
    kw.setdefault("spot_price", SPOT)
    out = calculate_best_value(pd.DataFrame(rows), **kw)
    return out.set_index("strike")["_multipliers"]


@pytest.mark.parametrize("strat,expected", [
    (STRAT_LONG_CALL, 2),
    (STRAT_BULL_CALL_SPREAD, 1),
    (STRAT_IRON_CONDOR, 0),
    (STRAT_STRADDLE, None),
    (STRAT_BEAR_PUT_SPREAD, -1),
    (STRAT_LONG_PUT, -2),
    (STRAT_UNKNOWN, None),
])
def test_strategy_outlook_parsing(strat, expected):
    assert strategy_outlook(strat) == expected


@pytest.mark.parametrize("strat", [
    STRAT_LONG_CALL, STRAT_BULL_CALL_SPREAD, STRAT_IRON_CONDOR,
    STRAT_STRADDLE, STRAT_BEAR_PUT_SPREAD, STRAT_LONG_PUT, STRAT_UNKNOWN,
])
def test_all_six_strategies_have_a_branch(strat):
    """Every STRAT_* is handled — no silent fall-through."""
    rows = [
        _contract(245, 3.0, side="CALL"),
        _contract(255, 3.0, side="PUT"),
        _contract(250, 3.0, side="CALL"),
        _contract(250, 3.0, side="PUT"),
    ]
    out = calculate_best_value(
        pd.DataFrame(rows), spot_price=SPOT, now_et=NOW,
        optimal_strategy=strat, upper_1sd=260.0, lower_1sd=240.0,
    )
    assert "Value_Score" in out.columns
    # Branch behaviour:
    if is_unknown_strategy(strat):
        for m in out["_multipliers"].dropna():
            if isinstance(m, dict):
                assert "zero_outlook" not in m
                assert "plus1_itm" not in m
                assert "minus1_itm" not in m
                assert "straddle_atm" not in m
        assert out["_bias_unknown"].all()
    elif is_straddle_strategy(strat):
        # At least one near-ATM row should carry straddle_atm
        found = any(
            isinstance(m, dict) and "straddle_atm" in m
            for m in out["_multipliers"]
        )
        assert found
    else:
        outlook = strategy_outlook(strat)
        assert outlook in (2, 1, 0, -1, -2)
        keys = {
            2: "plus2_boost", 1: "plus1_itm", 0: "zero_outlook",
            -1: "minus1_itm", -2: "minus2_boost",
        }
        # zero outlook applies to all directional; others may depend on bands
        if outlook == 0:
            assert any(
                isinstance(m, dict) and "zero_outlook" in m
                for m in out["_multipliers"]
            )
        elif outlook in (1, -1):
            key = keys[outlook]
            assert any(isinstance(m, dict) and key in m for m in out["_multipliers"])


def test_unknown_bias_applies_no_multiplier():
    rows = [_contract(255, 3.0, side="CALL"), _contract(245, 3.0, side="PUT")]
    base = _score(rows, optimal_strategy="")  # will become UNKNOWN via recommend
    # Force path: daily_bias=None → STRAT_UNKNOWN, no strategy mult
    unknown = _score(rows, daily_bias=None)
    # Same as baseline with no strategy label forced to skip (empty → UNKNOWN)
    assert unknown[255.0] == pytest.approx(base[255.0], rel=1e-9)
    assert unknown[245.0] == pytest.approx(base[245.0], rel=1e-9)
    # Must NOT be the 0.3 iron-condor crush
    crushed = _score(rows, daily_bias="NEUTRAL")
    assert unknown[255.0] != pytest.approx(crushed[255.0], rel=1e-6)


def test_unknown_bias_differs_from_explicit_neutral():
    rows = [_contract(255, 3.0, side="CALL"), _contract(245, 3.0, side="PUT")]
    unk = _score(rows, daily_bias=None)
    neu = _score(rows, daily_bias="NEUTRAL")
    assert unk[255.0] != pytest.approx(neu[255.0], rel=1e-9)
    assert unk[245.0] != pytest.approx(neu[245.0], rel=1e-9)


def test_bearish_mirrors_bullish():
    """PUT under HEAVY BEARISH gets the same strategy mult a CALL gets under HEAVY BULLISH."""
    # Mirrored placement: CALL 245 ITM for +1, PUT 255 ITM for -1
    call_row = _contract(245, 3.0, side="CALL")
    put_row = _contract(255, 3.0, side="PUT")
    filler_c = _contract(260, 3.0, side="CALL")
    filler_p = _contract(240, 3.0, side="PUT")

    bull = calculate_best_value(
        pd.DataFrame([call_row, filler_c, put_row, filler_p]),
        spot_price=SPOT, now_et=NOW, daily_bias="HEAVY BULLISH",
    )
    bear = calculate_best_value(
        pd.DataFrame([call_row, filler_c, put_row, filler_p]),
        spot_price=SPOT, now_et=NOW, daily_bias="HEAVY BEARISH",
    )
    bull_m = bull.loc[bull["strike"] == 245.0, "_multipliers"].iloc[0]
    bear_m = bear.loc[bear["strike"] == 255.0, "_multipliers"].iloc[0]
    assert isinstance(bull_m, dict) and isinstance(bear_m, dict)
    assert bull_m.get("plus1_itm") == pytest.approx(SCORING["mult_plus1_itm"])
    assert bear_m.get("minus1_itm") == pytest.approx(SCORING["mult_minus1_itm"])
    assert bull_m["plus1_itm"] == bear_m["minus1_itm"]


def test_recommend_none_bias_is_unknown_not_neutral():
    assert recommend_strategy(None, None, None, False) == STRAT_UNKNOWN
    assert recommend_strategy("NEUTRAL", None, None, False) == STRAT_IRON_CONDOR
    assert is_unknown_strategy(STRAT_UNKNOWN)
    assert not is_unknown_strategy(STRAT_IRON_CONDOR)
