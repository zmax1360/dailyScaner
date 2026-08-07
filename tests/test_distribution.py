"""Unit tests for distribution.py — no Streamlit, no default IV substitution."""

from __future__ import annotations

import math

import pytest

from distribution import expiry_distribution, prob_beyond_strike
from greeks import bs_delta


def _mean_mode(prices, density):
    mode = prices[max(range(len(density)), key=lambda i: density[i])]
    # trapezoid expectation
    num = 0.0
    den = 0.0
    for i in range(1, len(prices)):
        w = 0.5 * (density[i] + density[i - 1]) * (prices[i] - prices[i - 1])
        mid = 0.5 * (prices[i] + prices[i - 1])
        num += mid * w
        den += w
    return num / den, mode


def _sd(prices, density):
    mean, _ = _mean_mode(prices, density)
    var = 0.0
    den = 0.0
    for i in range(1, len(prices)):
        w = 0.5 * (density[i] + density[i - 1]) * (prices[i] - prices[i - 1])
        mid = 0.5 * (prices[i] + prices[i - 1])
        var += w * (mid - mean) ** 2
        den += w
    return math.sqrt(var / den)


def test_lognormal_is_right_skewed():
    spot = 100.0
    out = expiry_distribution(spot, iv=0.30, dte_days=30)
    assert out is not None
    prices, density = out
    mean, mode = _mean_mode(prices, density)
    assert mode < spot
    # Right skew: mean lies above the mode (and near spot under r=0).
    assert mean > mode


def test_width_scales_with_sqrt_time():
    spot, iv = 100.0, 0.25
    out1 = expiry_distribution(spot, iv, dte_days=30)
    out4 = expiry_distribution(spot, iv, dte_days=120)
    assert out1 and out4
    sd1 = _sd(*out1)
    sd4 = _sd(*out4)
    ratio = sd4 / sd1
    # 4x days → ~2x sigma_T (sqrt), not ~4x
    assert 1.6 < ratio < 2.5


def test_width_scales_linearly_with_iv():
    spot, dte = 100.0, 45
    out1 = expiry_distribution(spot, iv=0.20, dte_days=dte)
    out2 = expiry_distribution(spot, iv=0.40, dte_days=dte)
    assert out1 and out2
    ratio = _sd(*out2) / _sd(*out1)
    assert 1.7 < ratio < 2.3


def test_prob_beyond_strike_atm_near_half():
    p = prob_beyond_strike(100.0, 100.0, 0.30, 30, "CALL")
    assert p is not None
    assert 0.4 <= p <= 0.6


def test_prob_call_and_put_sum_to_one():
    spot, k, iv, dte = 105.0, 100.0, 0.28, 21
    c = prob_beyond_strike(spot, k, iv, dte, "CALL")
    p = prob_beyond_strike(spot, k, iv, dte, "PUT")
    assert c is not None and p is not None
    assert c + p == pytest.approx(1.0, abs=1e-9)


def test_degenerate_iv_returns_none():
    assert expiry_distribution(100.0, iv=1e-5, dte_days=5) is None
    assert expiry_distribution(100.0, iv=None, dte_days=5) is None
    assert expiry_distribution(100.0, iv=0, dte_days=5) is None
    assert prob_beyond_strike(100.0, 100.0, 1e-5, 5, "CALL") is None
    assert prob_beyond_strike(100.0, 100.0, None, 5, "CALL") is None
    assert prob_beyond_strike(100.0, 100.0, 0, 5, "CALL") is None


def test_zero_dte_returns_none():
    assert expiry_distribution(100.0, iv=0.40, dte_days=0) is None
    assert prob_beyond_strike(100.0, 100.0, 0.40, 0, "CALL") is None


def test_no_default_substituted():
    """Missing / unusable inputs must yield None — never a fabricated curve."""
    assert expiry_distribution(100.0, iv=None, dte_days=10) is None
    assert expiry_distribution(0, iv=0.30, dte_days=10) is None
    assert expiry_distribution(-5, iv=0.30, dte_days=10) is None
    assert expiry_distribution(100.0, iv=0.30, dte_days=-1) is None
    out = expiry_distribution(100.0, iv=None, dte_days=10)
    assert out is None  # not ([], []) or a unit spike


def test_prob_matches_delta_roughly():
    # N(d2) vs call delta N(d1): they differ by the d1−d2 = sigma_T drift term
    # (and greeks.bs_delta also uses a non-zero r). Assert close, not equal.
    spot, strike, iv, dte = 100.0, 100.0, 0.25, 45
    p = prob_beyond_strike(spot, strike, iv, dte, "CALL")
    d = bs_delta("CALL", spot, strike, dte, iv)
    assert p is not None and d is not None
    assert abs(p - d) < 0.15
