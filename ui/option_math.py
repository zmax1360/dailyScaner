"""Black-Scholes delta/gamma/theta used for display on the Archive and Volume pages."""

from __future__ import annotations



def _norm_cdf(x: float) -> float:
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    import math
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_greeks(
    S: float, K: float, iv: float, dte_days: int,
    r: float = 0.05, is_call: bool = True,
) -> tuple[float | None, float | None, float | None]:
    """
    Black-Scholes delta, gamma, and theta (archive display / prefill only).
    Returns (delta, gamma, theta_per_calendar_day).
    Theta is in dollars per calendar day (negative = time decay cost).
    Returns (None, None, None) when inputs are invalid (e.g. 0DTE, zero IV).
    """
    import math
    if dte_days <= 0 or iv <= 0.001 or S <= 0 or K <= 0:
        return None, None, None
    try:
        T    = dte_days / 365.0
        sqT  = math.sqrt(T)
        d1   = (math.log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * sqT)
        d2   = d1 - iv * sqT
        nd1  = _norm_pdf(d1)
        disc = math.exp(-r * T)

        delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
        gamma = nd1 / (S * iv * sqT)

        theta_annual = -(S * nd1 * iv) / (2 * sqT)
        if is_call:
            theta_annual -= r * K * disc * _norm_cdf(d2)
        else:
            theta_annual += r * K * disc * _norm_cdf(-d2)

        return delta, gamma, theta_annual / 365.0
    except Exception:
        return None, None, None
