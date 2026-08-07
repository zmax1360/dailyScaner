"""
distribution.py — risk-neutral lognormal expiry distributions (display only).

Pure helpers: no Streamlit, no scoring side effects. Probabilities here are the
Black-Scholes risk-neutral measure (r=0 form used for N(d2)), NOT a real-world
forecast, and they say nothing about whether an option is fairly priced.
"""

from __future__ import annotations

import math

from greeks import _norm_cdf

_MIN_IV = 0.01


def _sigma_T(iv: float, dte_days: float) -> float | None:
    if iv is None or dte_days is None:
        return None
    try:
        iv_f = float(iv)
        dte_f = float(dte_days)
    except (TypeError, ValueError):
        return None
    if iv_f < _MIN_IV or dte_f <= 0:
        return None
    return iv_f * math.sqrt(dte_f / 365.0)


def expiry_distribution(
    spot,
    iv,
    dte_days,
    *,
    n: int = 200,
    span_sd: float = 3.5,
) -> tuple[list[float], list[float]] | None:
    """
    Lognormal price density at expiry under Black-Scholes (r=0).

    sigma_T = iv * sqrt(dte_days / 365)

    density(S) ∝ (1/S) * exp( -(ln(S/spot) + sigma_T^2/2)^2 / (2 * sigma_T^2) )

    Returns (prices, density), or None when iv/dte/spot are unusable.
    Never substitutes a default iv or floors dte — missing input means no curve.
    """
    try:
        s = float(spot)
    except (TypeError, ValueError):
        return None
    if s <= 0 or n < 2:
        return None

    sig = _sigma_T(iv, dte_days)
    if sig is None or sig <= 0:
        return None

    lo = s * math.exp(-span_sd * sig)
    hi = s * math.exp(+span_sd * sig)
    if not (lo > 0 and hi > lo):
        return None

    prices: list[float] = []
    raw: list[float] = []
    half_sig2 = 0.5 * sig * sig
    inv_2var = 1.0 / (2.0 * sig * sig)
    for i in range(n):
        # inclusive endpoints
        t = i / (n - 1)
        px = lo * (hi / lo) ** t
        try:
            z = math.log(px / s) + half_sig2
            dens = (1.0 / px) * math.exp(-(z * z) * inv_2var)
        except (ValueError, OverflowError, ZeroDivisionError):
            return None
        prices.append(px)
        raw.append(dens)

    # Normalise so the grid integrates ≈ 1 (trapezoid)
    area = 0.0
    for i in range(1, n):
        area += 0.5 * (raw[i] + raw[i - 1]) * (prices[i] - prices[i - 1])
    if area <= 0:
        return None
    density = [v / area for v in raw]
    return prices, density


def prob_beyond_strike(
    spot,
    strike,
    iv,
    dte_days,
    side,
) -> float | None:
    """
    Risk-neutral probability of finishing past the strike (Black-Scholes, r=0):

        d2 = (ln(spot/K) - sigma_T^2/2) / sigma_T
        CALL -> N(d2)      PUT -> N(-d2)

    This is NOT a real-world forecast and says nothing about fair pricing.
    Returns None on the same degenerate inputs as expiry_distribution.
    """
    try:
        s = float(spot)
        k = float(strike)
    except (TypeError, ValueError):
        return None
    if s <= 0 or k <= 0:
        return None

    sig = _sigma_T(iv, dte_days)
    if sig is None or sig <= 0:
        return None

    try:
        d2 = (math.log(s / k) - 0.5 * sig * sig) / sig
    except (ValueError, ZeroDivisionError, OverflowError):
        return None

    side_u = str(side or "").strip().upper()
    if side_u in ("CALL", "C"):
        return float(_norm_cdf(d2))
    if side_u in ("PUT", "P"):
        return float(_norm_cdf(-d2))
    return None
