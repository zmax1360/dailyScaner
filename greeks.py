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
