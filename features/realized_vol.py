"""Close-to-close realised volatility vs IV (instrumentation only — not scored)."""

from __future__ import annotations

import math
from statistics import stdev


def realized_vol(closes: list[float], window: int = 20) -> float | None:
    """
    Annualised close-to-close realised volatility over ``window`` returns.

    Returns
        r_i = ln(C_i / C_{i-1})
        sigma = stdev(r) * sqrt(252)

    Annualise with ``sqrt(252)`` so units match implied volatility (quoted
    annualised). A day-count mismatch would make every IV-vs-RV comparison
    meaningless.

    Returns None if fewer than ``window + 1`` closes, or if any close <= 0.
    Never substitutes a default.
    """
    if closes is None or window < 1:
        return None
    if len(closes) < window + 1:
        return None
    series = closes[-(window + 1) :]
    if any(c is None or not math.isfinite(float(c)) or float(c) <= 0 for c in series):
        return None
    rets: list[float] = []
    for i in range(1, len(series)):
        rets.append(math.log(float(series[i]) / float(series[i - 1])))
    if len(rets) < 2:
        return None
    return float(stdev(rets) * math.sqrt(252))


def iv_premium(iv: float | None, rv: float | None) -> float | None:
    """
    Return iv / rv. None if either is None, or rv <= 0.01
    (ratio explodes on a near-zero denominator).
    """
    if iv is None or rv is None:
        return None
    try:
        iv_f = float(iv)
        rv_f = float(rv)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(iv_f) or not math.isfinite(rv_f):
        return None
    if rv_f <= 0.01:
        return None
    return iv_f / rv_f
