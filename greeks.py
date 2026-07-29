"""
greeks.py — Black-Scholes helpers (no scipy).

yfinance does not return greeks; delta must be computed (CURSOR_DELTA_TASKS C).
"""

from __future__ import annotations

import math
from typing import Any

from config import SCORING


def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _as_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def bs_delta(
    side: str,
    spot: float,
    strike: float,
    dte_days: float,
    iv: float,
    r: float | None = None,
) -> float | None:
    """
    Black-Scholes delta.

        d1 = (ln(S/K) + (r + iv^2/2) * T) / (iv * sqrt(T)),  T = dte/365
        call: N(d1)      put: N(d1) - 1

    Returns None (never a default) when inputs are unusable:
    iv < min_iv_usable, dte <= 0, spot/strike <= 0, or any NaN.
    """
    s = _as_float(spot)
    k = _as_float(strike)
    t_days = _as_float(dte_days)
    sigma = _as_float(iv)
    if s is None or k is None or t_days is None or sigma is None:
        return None

    min_iv = float(SCORING.get("min_iv_usable", 0.01))
    if sigma < min_iv or t_days <= 0 or s <= 0 or k <= 0:
        return None

    if r is None:
        r = float(SCORING.get("risk_free_rate", 0.045))
    else:
        rr = _as_float(r)
        if rr is None:
            return None
        r = rr

    T = t_days / 365.0
    if T <= 0 or sigma <= 0:
        return None

    try:
        d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * T) / (
            sigma * math.sqrt(T)
        )
    except (ValueError, ZeroDivisionError, OverflowError):
        return None

    nd1 = _norm_cdf(d1)
    side_u = str(side or "").strip().upper()
    if side_u in ("CALL", "C"):
        return float(nd1)
    if side_u in ("PUT", "P"):
        return float(nd1 - 1.0)
    return None
