#!/usr/bin/env python3
"""
strategy_engine.py — 5 Directions framework + 1-SD Expected Move.

Expected Move (68% / 1σ):
  EM = Spot * IV * sqrt(DTE / 365)
  Upper_1SD = Spot + EM
  Lower_1SD = Spot - EM
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any

import pandas as pd

log = logging.getLogger("strategy_engine")

# Strategy label constants (stable for styling / Telegram)
STRAT_LONG_CALL = "🚀 LONG CALL (+2) - Explosive Upside"
STRAT_BULL_CALL_SPREAD = "📈 BULL CALL SPREAD (+1) - High Probability Up"
STRAT_IRON_CONDOR = "🦅 IRON CONDOR (0) - Range Bound Premium"
STRAT_STRADDLE = "💥 STRADDLE/STRANGLE - Volatility Expansion"
STRAT_BEAR_PUT_SPREAD = "📉 BEAR PUT SPREAD (-1) - High Probability Down"
STRAT_LONG_PUT = "🩸 LONG PUT (-2) - Explosive Downside"
STRAT_UNKNOWN = "—"

# Explicit outlook tokens — never re-derive from emoji/prose in hot paths.
# None = STRADDLE (vol expansion) or UNKNOWN bias (no strategy multiplier).
_OUTLOOK_BY_LABEL: dict[str, int | None] = {
    STRAT_LONG_CALL: 2,
    STRAT_BULL_CALL_SPREAD: 1,
    STRAT_IRON_CONDOR: 0,
    STRAT_STRADDLE: None,
    STRAT_BEAR_PUT_SPREAD: -1,
    STRAT_LONG_PUT: -2,
    STRAT_UNKNOWN: None,
}

_ALL_STRATS: tuple[str, ...] = (
    STRAT_LONG_CALL,
    STRAT_BULL_CALL_SPREAD,
    STRAT_IRON_CONDOR,
    STRAT_STRADDLE,
    STRAT_BEAR_PUT_SPREAD,
    STRAT_LONG_PUT,
    STRAT_UNKNOWN,
)


def strategy_outlook(strat: str | None) -> int | None:
    """
    Integer outlook for the strategy-multiplier block.

    Returns +2, +1, 0, -1, -2 for directional/neutral labels.
    Returns None for STRADDLE (vol expansion), STRAT_UNKNOWN, or unrecognised.
    Callers must treat STRADDLE vs UNKNOWN via is_straddle_strategy().
    """
    if strat is None:
        return None
    s = str(strat).strip()
    if not s or s == STRAT_UNKNOWN:
        return None
    if s in _OUTLOOK_BY_LABEL:
        return _OUTLOOK_BY_LABEL[s]
    # Abbreviated / test labels like "(+1) BULL" — parse the token only
    m = re.search(r"\(([+-]?\d+)\)", s)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    return None


def is_straddle_strategy(strat: str | None) -> bool:
    """True for the volatility-expansion outlook (explicit branch, not fall-through)."""
    if strat is None:
        return False
    s = str(strat).strip()
    return s == STRAT_STRADDLE or s.startswith("💥")


def is_unknown_strategy(strat: str | None) -> bool:
    """True when bias was unavailable — no strategy multiplier should apply."""
    if strat is None:
        return True
    s = str(strat).strip()
    return s == STRAT_UNKNOWN or s == "" or s == "—"


def recommend_strategy(
    daily_bias: str | None,
    iv: float | None,
    profited_shares_pct: float | None,
    has_catalyst: bool,
    *,
    spot_below_support: bool = False,
) -> str:
    """
    5 Directions strategy matrix. Returns a display label (unchanged strings).

    Outlook priority:
      +2  HEAVY BULLISH + Blue Sky (profited ≥ 95%)
      +1  HEAVY BULLISH (otherwise)
      -2  HEAVY BEARISH + spot below VWAP/cost support
      -1  HEAVY BEARISH (otherwise)
      V   NEUTRAL + catalyst — STRADDLE
      0   NEUTRAL, no catalyst — IRON CONDOR
      ?   daily_bias is None / blank — STRAT_UNKNOWN (not NEUTRAL)
    """
    # Distinguish unknown bias from genuine NEUTRAL (F-04)
    if daily_bias is None or not str(daily_bias).strip():
        return STRAT_UNKNOWN

    bias = str(daily_bias).strip().upper()
    try:
        prof = float(profited_shares_pct) if profited_shares_pct is not None else None
    except (TypeError, ValueError):
        prof = None

    # Optional IV context (normalized) — reserved for future filters / logging
    _ = iv

    if bias == "HEAVY BULLISH":
        if prof is not None and prof >= 95.0:
            return STRAT_LONG_CALL
        return STRAT_BULL_CALL_SPREAD

    if bias == "HEAVY BEARISH":
        if spot_below_support:
            return STRAT_LONG_PUT
        return STRAT_BEAR_PUT_SPREAD

    if bias == "NEUTRAL":
        if has_catalyst:
            return STRAT_STRADDLE
        return STRAT_IRON_CONDOR

    # Unrecognised bias string — treat as unknown, not iron condor
    log.warning("recommend_strategy: unrecognised daily_bias=%r → STRAT_UNKNOWN", daily_bias)
    return STRAT_UNKNOWN


def calculate_expected_move(
    spot_price: float,
    implied_volatility: float,
    dte: float | int,
) -> dict[str, float | None]:
    """
    1-standard-deviation expected move for a given spot / IV / DTE.

    IV may be decimal (0.25) or percent (25); values > 2.0 are treated as %.
    DTE ≤ 0 → EM = 0 (same-day / expired).
    """
    try:
        spot = float(spot_price)
        iv = float(implied_volatility)
        dte_f = float(dte)
    except (TypeError, ValueError):
        return {
            "Expected_Move": None,
            "Upper_1SD": None,
            "Lower_1SD": None,
            "IV": None,
            "DTE": None,
        }

    if spot <= 0 or not math.isfinite(spot):
        return {
            "Expected_Move": None,
            "Upper_1SD": None,
            "Lower_1SD": None,
            "IV": None,
            "DTE": dte_f,
        }

    # Normalize IV to decimal
    if iv > 2.0:
        iv = iv / 100.0
    if iv < 0:
        iv = 0.0

    if dte_f <= 0 or iv <= 0:
        em = 0.0
    else:
        em = spot * iv * math.sqrt(dte_f / 365.0)

    return {
        "Expected_Move": round(em, 4),
        "Upper_1SD": round(spot + em, 4),
        "Lower_1SD": round(spot - em, 4),
        "IV": round(iv, 6),
        "DTE": dte_f,
    }


def resolve_has_catalyst(
    news_bias: str | None = None,
    catalyst_score: float | None = None,
    *,
    score_threshold: float = 0.35,
) -> bool:
    """True when news is directional or catalyst score is elevated."""
    bias = (news_bias or "NEUTRAL").strip().upper()
    if bias in ("BULLISH", "BEARISH"):
        return True
    try:
        if catalyst_score is not None and abs(float(catalyst_score)) >= score_threshold:
            return True
    except (TypeError, ValueError):
        pass
    return False


def resolve_spot_below_support(
    spot: float,
    *,
    vwap: float | None = None,
    vwap_state: str | None = None,
    cost_info: dict | None = None,
) -> bool:
    """
    Spot has broken major VWAP / cost support:
      • price below live VWAP, or VWAP_State is RECLAIMED DOWN / TRENDING BELOW
      • OR price below 70% cost-range floor / POC
    """
    try:
        px = float(spot)
    except (TypeError, ValueError):
        return False
    if px <= 0:
        return False

    state = (vwap_state or "").strip().upper()
    if state in ("RECLAIMED DOWN", "TRENDING BELOW"):
        return True
    if vwap is not None:
        try:
            if px < float(vwap):
                return True
        except (TypeError, ValueError):
            pass

    info = cost_info or {}
    r70 = info.get("Cost_Range_70") or (None, None)
    try:
        lo = r70[0]
        if lo is not None and px < float(lo):
            return True
    except (TypeError, ValueError, IndexError):
        pass
    poc = info.get("Average_Cost_POC")
    try:
        if poc is not None and px < float(poc):
            return True
    except (TypeError, ValueError):
        pass
    return False


def estimate_front_iv_dte(
    vol_curr: dict | None,
    spot: float,
) -> tuple[float | None, float | None]:
    """
    Volume-weighted average IV and median DTE from archive top calls/puts
    nearest the money (for ticker-level 1SD range).
    """
    rows: list[tuple[float, float, float]] = []  # (iv, dte, weight)
    for key in ("top_calls", "top_puts"):
        for c in (vol_curr or {}).get(key) or []:
            try:
                iv = float(c.get("impliedVolatility") or c.get("iv") or 0)
                dte = float(c.get("dte") or 0)
                strike = float(c.get("strike") or 0)
                vol = float(c.get("volume") or 0)
            except (TypeError, ValueError):
                continue
            if iv <= 0 or dte < 0 or strike <= 0:
                continue
            if iv > 2.0:
                iv = iv / 100.0
            # Prefer near-ATM: weight ∝ volume / (1 + |strike-spot|/spot)
            moneyness = abs(strike - spot) / spot if spot > 0 else 1.0
            w = max(vol, 1.0) / (1.0 + moneyness * 10.0)
            rows.append((iv, dte, w))

    if not rows:
        return None, None

    tw = sum(r[2] for r in rows)
    iv_w = sum(r[0] * r[2] for r in rows) / tw if tw else None
    dtes = sorted(r[1] for r in rows)
    dte_med = dtes[len(dtes) // 2]
    return (round(float(iv_w), 6) if iv_w is not None else None), float(dte_med)


def ticker_expected_range(
    spot: float,
    vol_curr: dict | None = None,
    *,
    iv: float | None = None,
    dte: float | None = None,
) -> dict[str, Any]:
    """Ticker-level 1SD expected range for KPI / banner display."""
    if iv is None or dte is None:
        est_iv, est_dte = estimate_front_iv_dte(vol_curr, spot)
        iv = iv if iv is not None else est_iv
        dte = dte if dte is not None else est_dte
    if iv is None or dte is None:
        return {
            "Expected_Move": None,
            "Upper_1SD": None,
            "Lower_1SD": None,
            "IV": None,
            "DTE": None,
            "label": "1SD Expected Range: —",
        }
    em = calculate_expected_move(spot, iv, dte)
    lo, hi = em.get("Lower_1SD"), em.get("Upper_1SD")
    if lo is None or hi is None:
        label = "1SD Expected Range: —"
    else:
        label = f"1SD Expected Range: ${lo:.2f} – ${hi:.2f}"
    return {**em, "label": label}


def attach_optimal_strategy(
    df: pd.DataFrame,
    *,
    daily_bias: str | None,
    profited_shares_pct: float | None,
    has_catalyst: bool,
    spot_below_support: bool = False,
    spot: float | None = None,
) -> pd.DataFrame:
    """
    Append `Optimal Strategy` (and per-row EM bounds when iv/dte present).
    Strategy uses the 5 Directions matrix; EM uses each row's IV + DTE.
    """
    out = df.copy()
    if out.empty:
        out["Optimal Strategy"] = pd.Series(dtype=str)
        return out

    # Ticker-level recommendation (same outlook for all rows)
    # Use median IV of scored rows as context for recommend_strategy(iv=...)
    iv_col = "iv" if "iv" in out.columns else ("IV" if "IV" in out.columns else None)
    dte_col = "dte" if "dte" in out.columns else ("DTE" if "DTE" in out.columns else None)

    ctx_iv = None
    if iv_col is not None:
        ivs = pd.to_numeric(out[iv_col], errors="coerce").dropna()
        if not ivs.empty:
            ctx_iv = float(ivs.median())

    strat = recommend_strategy(
        daily_bias,
        ctx_iv,
        profited_shares_pct,
        has_catalyst,
        spot_below_support=spot_below_support,
    )
    out["Optimal Strategy"] = strat

    if spot is not None and iv_col and dte_col:
        ems, ups, los = [], [], []
        for _, row in out.iterrows():
            dte_raw = row[dte_col]
            # Display DTE may be "5d" — strip
            if isinstance(dte_raw, str):
                dte_raw = dte_raw.strip().lower().replace("d", "")
            em = calculate_expected_move(float(spot), row[iv_col], dte_raw)
            ems.append(em["Expected_Move"])
            ups.append(em["Upper_1SD"])
            los.append(em["Lower_1SD"])
        out["Expected_Move"] = ems
        out["Upper_1SD"] = ups
        out["Lower_1SD"] = los

    return out


def strategy_cell_style(val: str) -> str:
    """Pandas Styler map for the Optimal Strategy column."""
    s = str(val or "")
    if "LONG CALL" in s or "BULL CALL" in s:
        return "color:#00e676;font-weight:bold"
    if "LONG PUT" in s or "BEAR PUT" in s:
        return "color:#ff1744;font-weight:bold"
    if "STRADDLE" in s or "STRANGLE" in s:
        return "color:#ffab00;font-weight:bold"
    if "IRON CONDOR" in s:
        return "color:#9e9e9e;font-weight:bold"
    return "color:#9e9e9e"
