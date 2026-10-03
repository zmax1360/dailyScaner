"""
Delta saturation (handover D4) — quote-repaired IV, engine-v1.5.

Vendor IV on some contracts is junk-low: it prices the contract below its live
bid. bs_delta at that IV pins delta at ±1.000 (ITM) or ~0 (OTM). In
"quote_repair" mode IV is re-solved from the bid/ask mid for those contracts
only. Absent the config key the engine behaves exactly as <= engine-v1.4.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
import pytz

from best_value import calculate_best_value
from config import SCORING
from greeks import (
    bs_delta,
    bs_price,
    contract_delta,
    effective_dte_days,
    implied_vol,
    quote_repaired_iv,
)

ET = pytz.timezone("US/Eastern")
SPOT = 333.0
R = 0.045
T_0DTE_10AM = 0.25  # days: 6h to the close
TRUE_IV = 0.30
JUNK_IV = 0.08
SATURATED = 0.9995  # rounds to 1.000 at three decimals


def _quote(side, strike, t_days, iv=TRUE_IV, half_spread=0.03):
    """Bid/ask centred on the BS price at ``iv`` — a quote the market 'means'."""
    p = bs_price(side, SPOT, strike, t_days, iv, r=R)
    return p - half_spread, p + half_spread


@pytest.fixture
def repair_mode(monkeypatch):
    monkeypatch.setitem(SCORING, "delta_iv_source", "quote_repair")


@pytest.fixture
def vendor_mode(monkeypatch):
    monkeypatch.setitem(SCORING, "delta_iv_source", "vendor")


# ── bs_price / implied_vol ──────────────────────────────────────────────────

@pytest.mark.parametrize("strike", [320.0, 333.0, 345.0])
@pytest.mark.parametrize("t_days", [0.25, 1.0, 7.0])
def test_bs_price_satisfies_put_call_parity(strike, t_days):
    import math

    c = bs_price("CALL", SPOT, strike, t_days, TRUE_IV, r=R)
    p = bs_price("PUT", SPOT, strike, t_days, TRUE_IV, r=R)
    disc_k = strike * math.exp(-R * t_days / 365.0)
    assert c - p == pytest.approx(SPOT - disc_k, abs=1e-9)


@pytest.mark.parametrize("side", ["CALL", "PUT"])
@pytest.mark.parametrize("strike", [331.0, 333.0, 335.0])
@pytest.mark.parametrize("t_days", [0.25, 1.0, 7.0])
@pytest.mark.parametrize("iv", [0.15, 0.30, 0.80])
def test_implied_vol_round_trips(side, strike, t_days, iv):
    price = bs_price(side, SPOT, strike, t_days, iv, r=R)
    solved = implied_vol(side, SPOT, strike, t_days, price, r=R)
    assert solved == pytest.approx(iv, abs=1e-4)


def test_implied_vol_is_none_without_time_value():
    # ITM call quoted at intrinsic: no IV reproduces it.
    assert implied_vol("CALL", SPOT, 320.0, T_0DTE_10AM, 13.0, r=R) is None
    # Below intrinsic (stale / broken quote).
    assert implied_vol("CALL", SPOT, 320.0, T_0DTE_10AM, 12.5, r=R) is None


def test_implied_vol_is_none_above_no_arbitrage_ceiling():
    # A call can never be worth more than the stock.
    assert implied_vol("CALL", SPOT, 333.0, 7.0, SPOT + 1.0, r=R) is None


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), None])
def test_implied_vol_is_none_for_unusable_price(bad):
    assert implied_vol("CALL", SPOT, 333.0, 7.0, bad, r=R) is None


# ── the defect, and the repair ──────────────────────────────────────────────

def test_vendor_mode_reproduces_itm_call_saturation(vendor_mode):
    """Characterises D4: 1% ITM, junk-low vendor IV → delta rounds to 1.000."""
    strike = SPOT / 1.01
    bid, ask = _quote("CALL", strike, T_0DTE_10AM)
    d = contract_delta(
        "CALL", SPOT, strike, T_0DTE_10AM, JUNK_IV, bid=bid, ask=ask, r=R
    )
    assert d is not None and d >= SATURATED
    assert round(d, 3) == 1.0


def test_repair_unsaturates_itm_call(repair_mode):
    strike = SPOT / 1.01
    bid, ask = _quote("CALL", strike, T_0DTE_10AM)
    d = contract_delta(
        "CALL", SPOT, strike, T_0DTE_10AM, JUNK_IV, bid=bid, ask=ask, r=R
    )
    want = bs_delta("CALL", SPOT, strike, T_0DTE_10AM, TRUE_IV, r=R)
    assert d == pytest.approx(want, abs=1e-3)
    assert 0.8 < d < 0.95


def test_repair_unsaturates_itm_put(repair_mode):
    strike = SPOT * 1.01
    bid, ask = _quote("PUT", strike, T_0DTE_10AM)
    junk = bs_delta("PUT", SPOT, strike, T_0DTE_10AM, JUNK_IV, r=R)
    assert junk <= -SATURATED
    d = contract_delta(
        "PUT", SPOT, strike, T_0DTE_10AM, JUNK_IV, bid=bid, ask=ask, r=R
    )
    want = bs_delta("PUT", SPOT, strike, T_0DTE_10AM, TRUE_IV, r=R)
    assert d == pytest.approx(want, abs=1e-3)
    assert -0.95 < d < -0.8


def test_repair_lifts_otm_delta_off_zero(repair_mode):
    """Same defect, other tail: junk-low IV gives an OTM contract ~zero delta."""
    strike = SPOT * 1.01
    bid, ask = _quote("CALL", strike, T_0DTE_10AM, half_spread=0.01)
    junk = bs_delta("CALL", SPOT, strike, T_0DTE_10AM, JUNK_IV, r=R)
    assert junk < 0.001
    d = contract_delta(
        "CALL", SPOT, strike, T_0DTE_10AM, JUNK_IV, bid=bid, ask=ask, r=R
    )
    want = bs_delta("CALL", SPOT, strike, T_0DTE_10AM, TRUE_IV, r=R)
    assert d == pytest.approx(want, abs=1e-3)
    assert d > 0.05


def test_repair_fixes_1dte_plus_within_3pct(repair_mode):
    """The 1DTE+ cell from the attribution query: 2% ITM, IV < 0.15."""
    strike = SPOT / 1.02
    bid, ask = _quote("CALL", strike, 1.0)
    junk = bs_delta("CALL", SPOT, strike, 1.0, JUNK_IV, r=R)
    assert junk >= SATURATED
    d = contract_delta("CALL", SPOT, strike, 1.0, JUNK_IV, bid=bid, ask=ask, r=R)
    assert d == pytest.approx(
        bs_delta("CALL", SPOT, strike, 1.0, TRUE_IV, r=R), abs=1e-3
    )
    assert d < 0.95


# ── what the repair must NOT touch ──────────────────────────────────────────

def test_legitimate_saturation_is_preserved(repair_mode):
    """Deep ITM 0DTE really is delta 1.000 — a consistent quote changes nothing."""
    strike = SPOT / 1.05
    bid, ask = _quote("CALL", strike, T_0DTE_10AM)
    d = contract_delta(
        "CALL", SPOT, strike, T_0DTE_10AM, TRUE_IV, bid=bid, ask=ask, r=R
    )
    assert d == bs_delta("CALL", SPOT, strike, T_0DTE_10AM, TRUE_IV, r=R)
    assert d >= SATURATED


def test_vendor_iv_kept_when_its_price_is_inside_the_quote(repair_mode):
    bid, ask = _quote("CALL", 335.0, 7.0)
    assert quote_repaired_iv("CALL", SPOT, 335.0, 7.0, TRUE_IV, bid, ask, r=R) == TRUE_IV


def test_vendor_iv_kept_when_it_prices_above_the_ask(repair_mode):
    """Too-HIGH vendor IV is a different defect; this change does not touch it."""
    bid, ask = _quote("CALL", 335.0, 7.0, iv=0.20)
    assert quote_repaired_iv("CALL", SPOT, 335.0, 7.0, 0.60, bid, ask, r=R) == 0.60


@pytest.mark.parametrize(
    "bid, ask",
    [
        (None, None),
        (float("nan"), float("nan")),
        (0.0, 1.0),      # no bid
        (1.2, 1.0),      # crossed
        (1.0, None),
    ],
)
def test_vendor_iv_kept_without_a_usable_two_sided_quote(repair_mode, bid, ask):
    strike = SPOT / 1.005
    got = quote_repaired_iv("CALL", SPOT, strike, T_0DTE_10AM, JUNK_IV, bid, ask, r=R)
    assert got == JUNK_IV


@pytest.mark.parametrize("iv", [None, float("nan"), 0.0, 0.001])
def test_unusable_vendor_iv_still_yields_no_delta(repair_mode, iv):
    """No universe expansion: a missing/sub-floor IV stays excluded, as before."""
    bid, ask = _quote("CALL", 335.0, 7.0)
    assert contract_delta("CALL", SPOT, 335.0, 7.0, iv, bid=bid, ask=ask, r=R) is None


def test_repair_needed_but_unsolvable_is_none_not_a_default(repair_mode):
    # Bid above the stock price: vendor IV under-prices it, and no IV can match.
    d = contract_delta(
        "CALL", SPOT, 333.0, 7.0, TRUE_IV, bid=SPOT + 1.0, ask=SPOT + 2.0, r=R
    )
    assert d is None


@pytest.mark.parametrize("side", ["CALL", "PUT"])
@pytest.mark.parametrize("strike", [320.0, 331.0, 333.0, 335.0, 345.0])
@pytest.mark.parametrize("iv", [JUNK_IV, TRUE_IV])
def test_vendor_mode_is_identical_to_bs_delta(vendor_mode, side, strike, iv):
    """<= engine-v1.4 behaviour is exactly preserved, whatever the quote says."""
    bid, ask = _quote(side, strike, 1.0)
    assert contract_delta(side, SPOT, strike, 1.0, iv, bid=bid, ask=ask, r=R) == bs_delta(
        side, SPOT, strike, 1.0, iv, r=R
    )


def test_absent_config_key_means_vendor_mode(monkeypatch):
    monkeypatch.delitem(SCORING, "delta_iv_source", raising=False)
    strike = SPOT / 1.005
    bid, ask = _quote("CALL", strike, T_0DTE_10AM)
    d = contract_delta(
        "CALL", SPOT, strike, T_0DTE_10AM, JUNK_IV, bid=bid, ask=ask, r=R
    )
    assert d == bs_delta("CALL", SPOT, strike, T_0DTE_10AM, JUNK_IV, r=R)


# ── scoring path ────────────────────────────────────────────────────────────

def _chain():
    now = ET.localize(datetime(2026, 10, 2, 10, 0))  # 6h to the close
    t = effective_dte_days(0, expiry="2026-10-02", now_et=now)
    rows = []
    for strike, iv in [(SPOT / 1.01, JUNK_IV), (335.0, TRUE_IV)]:
        p = bs_price("CALL", SPOT, strike, t, TRUE_IV, r=float(SCORING["risk_free_rate"]))
        rows.append({
            "side": "CALL", "strike": strike, "expiry": "2026-10-02", "dte": 0,
            "last": round(p, 2), "bid": p - 0.03, "ask": p + 0.03,
            "volume": 5000, "openInterest": 5000, "iv": iv,
        })
    return pd.DataFrame(rows), now, rows[0]["strike"]


def test_scoring_path_stores_saturated_delta_in_vendor_mode(vendor_mode):
    df, now, itm = _chain()
    out = calculate_best_value(df, spot_price=SPOT, now_et=now, min_volume=500)
    d = float(out.loc[out["strike"] == itm, "delta"].iloc[0])
    assert d >= SATURATED


def test_scoring_path_stores_repaired_delta_in_repair_mode(repair_mode):
    df, now, itm = _chain()
    out = calculate_best_value(df, spot_price=SPOT, now_et=now, min_volume=500)
    d = float(out.loc[out["strike"] == itm, "delta"].iloc[0])
    assert 0.8 < d < 0.95
    # The contract whose vendor IV already matched its quote is untouched.
    other = float(out.loc[out["strike"] == 335.0, "delta"].iloc[0])
    t = effective_dte_days(0, expiry="2026-10-02", now_et=now)
    assert other == pytest.approx(
        bs_delta("CALL", SPOT, 335.0, t, TRUE_IV, r=float(SCORING["risk_free_rate"]))
    )
