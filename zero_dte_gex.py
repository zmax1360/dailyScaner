#!/usr/bin/env python3
"""
zero_dte_gex.py — 0DTE Gamma Exposure & Reflexivity Detector.

Filters archive top-calls/puts for DTE==0, estimates net gamma exposure
from Black-Scholes gamma × volume, and classifies squeeze / cascade / neutral.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from config import SCORING

ET = ZoneInfo("America/New_York")

STATE_SQUEEZE = "🚀 0DTE CALL GAMMA SQUEEZE (High Reflexivity)"
STATE_CASCADE = "🩸 0DTE PUT GAMMA CASCADE (Forced Selling)"
STATE_NEUTRAL = "⚖️ NEUTRAL 0DTE FLOW (Balanced Hedging)"

ATM_PCT = 0.015  # |K-S|/S ≤ 1.5% → ATM for boost
BOOST_MULT = float(SCORING["mult_0dte_boost"])


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(
    spot: float,
    strike: float,
    iv: float,
    *,
    t_years: float,
    r: float = 0.05,
) -> float | None:
    """Black-Scholes gamma. Returns None on invalid inputs."""
    if spot <= 0 or strike <= 0 or iv <= 0.001 or t_years <= 1e-8:
        return None
    try:
        sqT = math.sqrt(t_years)
        d1 = (math.log(spot / strike) + (r + 0.5 * iv * iv) * t_years) / (iv * sqT)
        return _norm_pdf(d1) / (spot * iv * sqT)
    except Exception:
        return None


def _0dte_time_years(now_et: datetime) -> float:
    """
    Years to the 16:00 ET cash close for same-day options.
    Floors at ~15 minutes so gamma stays finite near the bell.
    """
    close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    secs = (close - now_et).total_seconds()
    hours = max(secs / 3600.0, 0.25)
    return hours / (365.0 * 24.0)


def _is_atm(strike: float, spot: float, pct: float = ATM_PCT) -> bool:
    if spot <= 0:
        return False
    return abs(float(strike) - float(spot)) / float(spot) <= pct


def _empty(ticker: str = "") -> dict[str, Any]:
    return {
        "ticker": (ticker or "").upper(),
        "has_0dte": False,
        "0DTE_Call_Volume": 0,
        "0DTE_Put_Volume": 0,
        "0DTE_Call_Ratio": None,
        "Net_0DTE_Gamma": None,
        "0DTE_State": STATE_NEUTRAL,
        "afternoon_phase": False,
        "boost_side": None,  # "CALL" | "PUT" | None
        "top_strikes": [],
        "call_gamma_wavg": None,
        "put_gamma_wavg": None,
    }


def calculate_0dte_gamma_flow(
    vol_curr: dict | None,
    spot: float,
    *,
    now_et: datetime | None = None,
    ticker: str = "",
) -> dict[str, Any]:
    """
    Compute 0DTE call/put volume, call ratio, net GEX proxy, and reflexivity state.

    Net_0DTE_Gamma ≈ Σ(call_vol × γ) − Σ(put_vol × γ)
    """
    out = _empty(ticker)
    if now_et is None:
        now_et = datetime.now(ET)
    elif now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    else:
        now_et = now_et.astimezone(ET)

    out["afternoon_phase"] = (
        now_et.hour > 13 or (now_et.hour == 13 and now_et.minute >= 0)
    )

    try:
        spot_f = float(spot)
    except (TypeError, ValueError):
        return out
    if spot_f <= 0:
        return out

    t_yrs = _0dte_time_years(now_et)
    call_vol_tot = 0
    put_vol_tot = 0
    call_gex = 0.0
    put_gex = 0.0
    call_g_w = 0.0
    put_g_w = 0.0
    strike_agg: dict[float, dict[str, float]] = {}

    for side, key in [("CALL", "top_calls"), ("PUT", "top_puts")]:
        for c in (vol_curr or {}).get(key) or []:
            try:
                dte = int(c.get("dte") if c.get("dte") is not None else -1)
            except (TypeError, ValueError):
                continue
            if dte != 0:
                # Also accept expiry == today ET when dte missing/wrong
                exp = str(c.get("expiry") or "")
                today_s = now_et.strftime("%Y-%m-%d")
                if exp != today_s:
                    continue
            try:
                strike = float(c.get("strike") or 0)
                vol = float(c.get("volume") or 0)
                iv = float(c.get("impliedVolatility") or c.get("iv") or 0)
            except (TypeError, ValueError):
                continue
            if strike <= 0 or vol <= 0:
                continue
            if iv > 2.0:
                iv = iv / 100.0

            g = bs_gamma(spot_f, strike, iv, t_years=t_yrs)
            if g is None:
                g = 0.0

            bucket = strike_agg.setdefault(
                strike, {"call_vol": 0.0, "put_vol": 0.0, "total": 0.0}
            )
            if side == "CALL":
                call_vol_tot += vol
                call_gex += vol * g
                call_g_w += vol * g
                bucket["call_vol"] += vol
            else:
                put_vol_tot += vol
                put_gex += vol * g
                put_g_w += vol * g
                bucket["put_vol"] += vol
            bucket["total"] += vol

    out["0DTE_Call_Volume"] = int(round(call_vol_tot))
    out["0DTE_Put_Volume"] = int(round(put_vol_tot))
    total = call_vol_tot + put_vol_tot
    if total <= 0:
        return out

    out["has_0dte"] = True
    ratio = call_vol_tot / total
    net_gex = call_gex - put_gex
    out["0DTE_Call_Ratio"] = round(ratio, 4)
    out["Net_0DTE_Gamma"] = round(net_gex, 8)
    out["call_gamma_wavg"] = (
        round(call_g_w / call_vol_tot, 8) if call_vol_tot > 0 else None
    )
    out["put_gamma_wavg"] = (
        round(put_g_w / put_vol_tot, 8) if put_vol_tot > 0 else None
    )

    if ratio >= 0.65 and net_gex > 0:
        out["0DTE_State"] = STATE_SQUEEZE
        out["boost_side"] = "CALL"
    elif ratio <= 0.35 and net_gex < 0:
        out["0DTE_State"] = STATE_CASCADE
        out["boost_side"] = "PUT"
    else:
        out["0DTE_State"] = STATE_NEUTRAL
        out["boost_side"] = None

    top = sorted(
        (
            {
                "strike": k,
                "call_vol": int(round(v["call_vol"])),
                "put_vol": int(round(v["put_vol"])),
                "total_vol": int(round(v["total"])),
                "atm": _is_atm(k, spot_f),
            }
            for k, v in strike_agg.items()
        ),
        key=lambda r: r["total_vol"],
        reverse=True,
    )[:5]
    out["top_strikes"] = top
    return out


def apply_0dte_boost_mask(
    side: pd.Series,
    strike: pd.Series,
    dte: pd.Series,
    spot: float,
    boost_side: str | None,
) -> pd.Series:
    """Boolean mask: 0DTE ATM contracts on the boosted side."""
    if not boost_side or spot <= 0:
        return pd.Series(False, index=side.index)
    side_u = side.astype(str).str.upper()
    dte_n = pd.to_numeric(dte, errors="coerce")
    strike_n = pd.to_numeric(strike, errors="coerce")
    atm = (strike_n - float(spot)).abs() / float(spot) <= ATM_PCT
    return (dte_n == 0) & atm & (side_u == boost_side.upper())


def call_put_progress_bar_html(call_ratio: float | None, width_px: int = 160) -> str:
    """Green/red dominance bar from 0DTE_Call_Ratio (0–1)."""
    if call_ratio is None:
        return '<span style="color:#666">—</span>'
    call_pct = max(0.0, min(100.0, float(call_ratio) * 100.0))
    put_pct = 100.0 - call_pct
    return (
        f'<div style="display:flex;align-items:center;gap:8px">'
        f'<div style="flex:0 0 {width_px}px;height:12px;border-radius:6px;'
        f'overflow:hidden;background:#333;display:flex">'
        f'<div style="width:{call_pct:.1f}%;background:#00c853"></div>'
        f'<div style="width:{put_pct:.1f}%;background:#d50000"></div>'
        f'</div>'
        f'<span style="font-size:0.8rem;color:#9e9e9e">'
        f'Call {call_pct:.0f}% · Put {put_pct:.0f}%</span></div>'
    )
