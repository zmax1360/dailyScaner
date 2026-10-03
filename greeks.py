"""
greeks.py — Black-Scholes helpers (no scipy).

yfinance does not return greeks; delta must be computed (CURSOR_DELTA_TASKS C).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

from config import SCORING

ET = ZoneInfo("America/New_York")


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


def effective_dte_days(
    dte: Any,
    *,
    expiry: Any = None,
    now_et: datetime | None = None,
    session_close: dtime | None = None,
) -> float:
    """
    Time to expiry in **calendar DAYS** for bs_delta (which divides by 365).

    - dte > 0: return that value as days.
    - Live 0DTE (expiry == today ET): return the fraction of a day remaining
      until 16:00 ET (e.g. 6 hours → 0.25 days), floored at 60 seconds.
      Never return a year-fraction here.
    - Otherwise: 0.0 (bs_delta will return None).

    Single shared helper — do not reimplement this conversion at call sites.
    """
    close_t = session_close or dtime(16, 0)
    try:
        d = float(dte) if dte is not None else float("nan")
    except (TypeError, ValueError):
        d = float("nan")
    if d == d and d > 0:
        return float(d)

    now = now_et or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    now = now.astimezone(ET)

    exp_d: date | None = None
    if expiry is not None and str(expiry).strip():
        try:
            if isinstance(expiry, datetime):
                exp_d = expiry.astimezone(ET).date() if expiry.tzinfo else expiry.date()
            elif isinstance(expiry, date):
                exp_d = expiry
            else:
                exp_d = date.fromisoformat(str(expiry)[:10])
        except Exception:
            exp_d = None

    # Live same-day expiry: fraction of a day to 16:00 ET. Floor at 60s so the
    # 16:00–16:15 window (still scored by best_value) stays usable for delta.
    if exp_d == now.date():
        close_et = datetime.combine(now.date(), close_t, tzinfo=ET)
        secs = max((close_et - now).total_seconds(), 60.0)
        return secs / 86400.0  # DAYS, not years
    return 0.0


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

    ``dte_days`` must be in calendar DAYS (use effective_dte_days for 0DTE).
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


# ── Quote-repaired IV (engine-v1.5) ───────────────────────────────────────
#
# Vendor IV on in-the-money contracts is often junk-low (it is solved from a
# stale last trade near intrinsic). bs_delta at that IV rounds to ±1.000 for
# contracts whose live quote still carries time value. The quote is the
# measurement, so when the vendor IV prices the contract below its live bid we
# solve IV from the mid instead. If that has no solution we return None
# (NaN and exclude) — never a default.

_IV_SOLVE_LO = 1e-4
_IV_SOLVE_HI = 10.0
_IV_SOLVE_ITERS = 100


def bs_price(
    side: str,
    spot: float,
    strike: float,
    dte_days: float,
    iv: float,
    r: float | None = None,
) -> float | None:
    """Black-Scholes price. Same units and None-on-unusable contract as bs_delta,
    except that any positive ``iv`` is accepted (the IV solver needs the range)."""
    s = _as_float(spot)
    k = _as_float(strike)
    t_days = _as_float(dte_days)
    sigma = _as_float(iv)
    if s is None or k is None or t_days is None or sigma is None:
        return None
    if sigma <= 0 or t_days <= 0 or s <= 0 or k <= 0:
        return None
    if r is None:
        r = float(SCORING.get("risk_free_rate", 0.045))
    else:
        rr = _as_float(r)
        if rr is None:
            return None
        r = rr

    T = t_days / 365.0
    vol_t = sigma * math.sqrt(T)
    try:
        d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * T) / vol_t
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    d2 = d1 - vol_t
    disc_k = k * math.exp(-r * T)
    side_u = str(side or "").strip().upper()
    if side_u in ("CALL", "C"):
        return float(s * _norm_cdf(d1) - disc_k * _norm_cdf(d2))
    if side_u in ("PUT", "P"):
        return float(disc_k * _norm_cdf(-d2) - s * _norm_cdf(-d1))
    return None


def implied_vol(
    side: str,
    spot: float,
    strike: float,
    dte_days: float,
    price: float,
    r: float | None = None,
) -> float | None:
    """
    IV that reproduces ``price`` (bisection; price is monotonic in IV).

    Returns None when no IV in [_IV_SOLVE_LO, _IV_SOLVE_HI] reproduces it —
    i.e. the price has no time value (at or below discounted intrinsic) or is
    above the no-arbitrage ceiling. Never a default.
    """
    p = _as_float(price)
    if p is None or p <= 0:
        return None
    lo, hi = _IV_SOLVE_LO, _IV_SOLVE_HI
    p_lo = bs_price(side, spot, strike, dte_days, lo, r=r)
    p_hi = bs_price(side, spot, strike, dte_days, hi, r=r)
    if p_lo is None or p_hi is None:
        return None
    if not (p_lo < p < p_hi):
        return None
    for _ in range(_IV_SOLVE_ITERS):
        mid = 0.5 * (lo + hi)
        p_mid = bs_price(side, spot, strike, dte_days, mid, r=r)
        if p_mid is None:
            return None
        if p_mid < p:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-7:
            break
    return float(0.5 * (lo + hi))


def quote_repaired_iv(
    side: str,
    spot: float,
    strike: float,
    dte_days: float,
    iv: Any,
    bid: Any,
    ask: Any,
    r: float | None = None,
) -> float | None:
    """
    Vendor IV, repaired from the live quote only when it is provably too low.

    "Too low" = the BS price at the vendor IV is below the live BID: the market
    is bidding for time value the vendor IV says does not exist. That is the
    mechanism that pins delta at ±1.000 (ITM) or ~0 (OTM). In that case IV is
    re-solved from the bid/ask mid.

    Everything else is left exactly as before (narrow on purpose):
    - vendor IV missing or below ``min_iv_usable`` → returned unchanged
      (bs_delta then yields None, as in <= engine-v1.4);
    - no usable two-sided quote (missing, bid <= 0, ask < bid) → vendor IV;
    - vendor price at or above the bid → vendor IV.
    Returns None when repair is needed but the mid has no solvable IV.
    """
    vendor = _as_float(iv)
    min_iv = float(SCORING.get("min_iv_usable", 0.01))
    if vendor is None or vendor < min_iv:
        return vendor
    b = _as_float(bid)
    a = _as_float(ask)
    if b is None or a is None or b <= 0 or a < b:
        return vendor
    p_vendor = bs_price(side, spot, strike, dte_days, vendor, r=r)
    if p_vendor is None or p_vendor >= b:
        return vendor

    solved = implied_vol(side, spot, strike, dte_days, 0.5 * (b + a), r=r)
    if solved is None or solved < min_iv:
        return None
    return solved


def contract_delta(
    side: str,
    spot: float,
    strike: float,
    dte_days: float,
    iv: Any,
    *,
    bid: Any = None,
    ask: Any = None,
    r: float | None = None,
) -> float | None:
    """
    Delta for one contract — the single entry point for scoring and display.

    ``SCORING["delta_iv_source"]`` (absent → "vendor", so behaviour and
    ``config_hash`` are unchanged until the key is added to config.SCORING):
      "vendor"       (<= engine-v1.4) → vendor IV as-is
      "quote_repair" (engine-v1.5)    → IV from quote_repaired_iv
    Returns None (never a default) when no usable IV exists.
    """
    mode = str(SCORING.get("delta_iv_source", "vendor"))
    if mode == "quote_repair":
        use_iv = quote_repaired_iv(side, spot, strike, dte_days, iv, bid, ask, r=r)
    else:
        use_iv = _as_float(iv)
    if use_iv is None:
        return None
    return bs_delta(side, spot, strike, dte_days, use_iv, r=r)
