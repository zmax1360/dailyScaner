"""0DTE delta unit fix — effective_dte_days must return DAYS, not year-fraction."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
import pytz

from best_value import calculate_best_value
from greeks import bs_delta, effective_dte_days

ET = pytz.timezone("US/Eastern")


def test_0dte_delta_is_not_a_step_function():
    """At 6h to close, ATM-ish 341 CALL must have a mid delta — not ~0 or ~1."""
    now = ET.localize(datetime(2026, 7, 24, 10, 0))  # 6 hours to 16:00
    t = effective_dte_days(0, expiry="2026-07-24", now_et=now)
    assert t == pytest.approx(0.25, abs=1e-3)
    d = bs_delta("CALL", 340.0, 341.0, t, 0.30)
    assert d is not None
    assert 0.25 <= d <= 0.55
    assert abs(d - 0.0) > 1e-6
    assert abs(d - 1.0) > 1e-6


def test_0dte_and_1dte_delta_are_continuous():
    now = ET.localize(datetime(2026, 7, 24, 10, 0))
    t0 = effective_dte_days(0, expiry="2026-07-24", now_et=now)
    d0 = bs_delta("CALL", 340.0, 340.0, t0, 0.30)
    d1 = bs_delta("CALL", 340.0, 340.0, 1.0, 0.30)
    assert d0 is not None and d1 is not None
    assert 0.2 < d0 < 0.8
    assert 0.2 < d1 < 0.8
    # No order-of-magnitude discontinuity
    assert abs(d0 - d1) < 0.4


def test_t_days_returns_days_in_both_branches():
    now = ET.localize(datetime(2026, 7, 24, 10, 0))
    assert effective_dte_days(28, expiry="2026-08-21", now_et=now) == 28.0
    assert effective_dte_days(0, expiry="2026-07-24", now_et=now) == pytest.approx(
        0.25, abs=1e-3
    )


def test_delta_monotonic_across_strikes_0dte():
    now = ET.localize(datetime(2026, 7, 24, 10, 0))
    spot = 340.0
    t = effective_dte_days(0, expiry="2026-07-24", now_et=now)
    strikes = [spot * x for x in (0.97, 0.985, 1.0, 1.015, 1.03)]
    deltas = [bs_delta("CALL", spot, k, t, 0.30) for k in strikes]
    assert all(d is not None for d in deltas)
    # Strictly decreasing
    for a, b in zip(deltas, deltas[1:]):
        assert a > b
    # At least 4 distinct values (step function only has 2)
    distinct = {round(d, 6) for d in deltas}
    assert len(distinct) >= 4


def test_bs_delta_verify_curve_at_6h():
    """Sanity vs CURSOR_0DTE_DELTA_FIX expected approximate curve."""
    for k, lo, hi in [
        (330.0, 0.99, 1.0),
        (338.0, 0.70, 0.85),
        (341.0, 0.30, 0.45),
        (344.0, 0.04, 0.12),
    ]:
        d = bs_delta("CALL", 340.0, k, 0.25, 0.30)
        assert d is not None and lo <= d <= hi, (k, d)


def test_0dte_scoring_path_uses_days_not_year_fraction():
    """calculate_best_value must produce a mid delta for near-ATM 0DTE."""
    now = ET.localize(datetime(2026, 7, 24, 10, 0))
    rows = [
        {
            "side": "CALL", "strike": 341.0, "expiry": "2026-07-24", "dte": 0,
            "last": 1.50, "volume": 5000, "openInterest": 5000, "iv": 0.30,
        },
        {
            "side": "CALL", "strike": 350.0, "expiry": "2026-07-24", "dte": 0,
            "last": 0.40, "volume": 5000, "openInterest": 5000, "iv": 0.30,
        },
    ]
    out = calculate_best_value(
        pd.DataFrame(rows), spot_price=340.0, now_et=now, min_volume=500,
    )
    d = float(out.loc[out["strike"] == 341.0, "delta"].iloc[0])
    assert 0.25 <= d <= 0.55
