"""gex — net gamma exposure (GEX) per strike and expiry from open interest.

    GEX = gamma x open_interest x 100 x spot^2 x 0.01      ($ of delta hedging per 1% move)

Calls count positive, puts negative. That sign is the standard dealer-positioning
ASSUMPTION (dealers long calls, short puts); it is not observed.

Pure calculation: no network, no Streamlit, no writes. Display/analysis only — not a
scoring input. Missing inputs (open interest, IV, spot) are excluded and counted,
never defaulted to 0. A strike/expiry with no usable contract is NaN, not 0.

Input is the frame from ``volume_history.latest_scan`` (contracts that have traded today,
so a strike with open interest but no volume yet is absent until it trades).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from config import SCORING
from zero_dte_gex import bs_gamma

ET = ZoneInfo("America/New_York")
SESSION_CLOSE = dtime(16, 0)
MIN_T_MINUTES = 15.0          # gamma is unbounded at expiry; same floor as zero_dte_gex
CONTRACT_MULTIPLIER = 100
MOVE_PCT = 0.01               # exposure is quoted per 1% move in spot

TABLE_COLS = ["side", "strike", "expiry", "dte", "open_interest", "iv", "gamma", "gex"]


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def years_to_expiry(expiry: str, as_of: datetime) -> float | None:
    """Calendar time from ``as_of`` to 16:00 ET on the expiry date, in years.

    Includes the hours left today (unlike a whole-day DTE). Floored at
    MIN_T_MINUTES. None when the expiry is unparseable or already past.
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    now = as_of.astimezone(ET)
    try:
        exp_d = date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return None
    if exp_d < now.date():
        return None
    close = datetime.combine(exp_d, SESSION_CLOSE, tzinfo=ET)
    minutes = max((close - now).total_seconds() / 60.0, MIN_T_MINUTES)
    return minutes / (365.0 * 24.0 * 60.0)


def gex_table(latest: pd.DataFrame, *, spot: float | None, as_of: datetime) -> pd.DataFrame:
    """One row per contract with its gamma and signed dollar GEX.

    ``gex`` is NaN when open interest, IV or gamma is unusable. Expired rows are dropped.
    """
    s = _f(spot)
    if latest is None or latest.empty or s is None or s <= 0:
        return pd.DataFrame(columns=TABLE_COLS)
    r = float(SCORING.get("risk_free_rate", 0.045))
    min_iv = float(SCORING.get("min_iv_usable", 0.01))
    today = as_of.astimezone(ET).date()

    rows = []
    for rec in latest.to_dict(orient="records"):
        side = str(rec.get("side") or "").upper()
        strike = _f(rec.get("strike"))
        expiry = str(rec.get("expiry") or "")[:10]
        t = years_to_expiry(expiry, as_of)
        if side not in ("CALL", "PUT") or strike is None or strike <= 0 or t is None:
            continue
        oi = _f(rec.get("open_interest"))
        iv = _f(rec.get("iv"))
        gamma = None
        if iv is not None and iv >= min_iv:
            gamma = bs_gamma(s, strike, iv, t_years=t, r=r)
        gex = float("nan")
        if gamma is not None and oi is not None and oi >= 0:
            sign = 1.0 if side == "CALL" else -1.0
            gex = sign * gamma * oi * CONTRACT_MULTIPLIER * s * s * MOVE_PCT
        rows.append({
            "side": side, "strike": strike, "expiry": expiry,
            "dte": (date.fromisoformat(expiry) - today).days,
            "open_interest": float("nan") if oi is None else oi,
            "iv": float("nan") if iv is None else iv,
            "gamma": float("nan") if gamma is None else gamma,
            "gex": gex,
        })
    return pd.DataFrame(rows, columns=TABLE_COLS)


def coverage(table: pd.DataFrame) -> dict[str, int]:
    """How many contracts actually contributed — the map is only as good as this."""
    n = int(len(table))
    used = int(table["gex"].notna().sum()) if n else 0
    return {"contracts": n, "used": used, "excluded": n - used}


def gex_matrix(
    table: pd.DataFrame,
    *,
    spot: float,
    window_pct: float = 0.06,
    max_expiries: int = 4,
) -> pd.DataFrame:
    """Net GEX: strikes (descending) x expiries (nearest first).

    Strikes within ``window_pct`` of spot, the nearest ``max_expiries`` expiries.
    A cell is NaN when no usable contract exists there (never 0).
    """
    if table is None or table.empty:
        return pd.DataFrame()
    t = table[table["gex"].notna()]
    lo, hi = spot * (1.0 - window_pct), spot * (1.0 + window_pct)
    t = t[(t["strike"] >= lo) & (t["strike"] <= hi)]
    expiries = sorted(t["expiry"].unique())[: max(int(max_expiries), 1)]
    t = t[t["expiry"].isin(expiries)]
    if t.empty:
        return pd.DataFrame()
    m = t.pivot_table(index="strike", columns="expiry", values="gex",
                      aggfunc=lambda x: x.sum(min_count=1))
    return m.reindex(columns=expiries).sort_index(ascending=False)


def walls(matrix: pd.DataFrame) -> dict[str, dict[str, float | None]]:
    """Per expiry: largest positive strike (call wall), most negative (put wall), net."""
    out: dict[str, dict[str, float | None]] = {}
    for exp in matrix.columns:
        col = matrix[exp].dropna()
        if col.empty:
            continue
        pos, neg = col[col > 0], col[col < 0]
        out[str(exp)] = {
            "call_wall": float(pos.idxmax()) if not pos.empty else None,
            "put_wall": float(neg.idxmin()) if not neg.empty else None,
            "net": float(col.sum()),
        }
    return out


def nearest_strike(matrix: pd.DataFrame, spot: float) -> float | None:
    if matrix is None or matrix.empty:
        return None
    return float(min(matrix.index, key=lambda k: abs(float(k) - float(spot))))


def fmt_money(v: Any) -> str:
    """25.40M / -399K / 8,356 — blank for missing."""
    x = _f(v)
    if x is None:
        return ""
    a = abs(x)
    if a >= 1e9:
        return f"{x / 1e9:.2f}B"
    if a >= 1e6:
        return f"{x / 1e6:.2f}M"
    if a >= 1e4:
        return f"{x / 1e3:.0f}K"
    return f"{x:,.0f}"
