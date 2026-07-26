#!/usr/bin/env python3
"""
best_value.py — Single Best Value scoring engine for dashboard + Telegram.

Both app.py and telegram_bot.py MUST import from here. Do not reimplement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd
import pytz


def _contract_price(c: dict) -> float:
    """Prefer mid(bid, ask); fall back to lastPrice."""
    bid = float(c.get("bid") or 0)
    ask = float(c.get("ask") or 0)
    last = float(c.get("lastPrice") or c.get("last") or 0)
    if bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    return last if last > 0 else 0.0


def attach_dvol(df: pd.DataFrame, vol_prev: dict | None) -> pd.DataFrame:
    """
    Attach ΔVol vs previous archive top-30 snapshot.

    Contracts missing from the previous top-30 get dVol = NaN (unknown),
    NOT volume - 0. Treating absence as prev=0 made new entrants look like
    full-volume surges and systematically win BEST VALUE (phantom ΔVol).
    """
    df = df.copy()
    if not vol_prev:
        return df

    prev_lookup: dict[tuple, int] = {}
    for side, key in [("CALL", "top_calls"), ("PUT", "top_puts")]:
        for c in (vol_prev.get(key) or []):
            k = (side, float(c.get("strike") or 0), c.get("expiry", ""))
            prev_lookup[k] = int(c.get("volume") or 0)

    def _delta(r: pd.Series) -> float:
        k = (r["side"], float(r["strike"]), r["expiry"])
        if k not in prev_lookup:
            return float("nan")
        return float(r["volume"]) - float(prev_lookup[k])

    df["dVol"] = df.apply(_delta, axis=1)
    return df


def calculate_best_value(
    df: pd.DataFrame,
    spot_price: float,
    min_volume: int = 500,
    daily_bias: str | None = None,
    market_state: str | None = None,
    news_bias: str | None = None,
    vwap_state: str | None = None,
    now_et: datetime | None = None,
    profited_shares_pct: float | None = None,
    *,
    upper_1sd: float | None = None,
    lower_1sd: float | None = None,
    optimal_strategy: str | None = None,
    has_catalyst: bool = False,
    spot_below_support: bool = False,
    odte_info: dict | None = None,
    pov_info: dict | None = None,
) -> pd.DataFrame:
    """
    Pure function — no Streamlit, no IO.

    Appends Value_Score, Status, Optimal_Strategy, Strategy_Tag.
    Expired contracts are always dropped; after 16:15 ET, same-day 0DTE
    is also dropped.

    Strategy Engine (1SD + 5 Directions) alters ranking:
      • CALL strike > upper_1sd  → ×0.2  (lottery OTM)
      • PUT  strike < lower_1sd  → ×0.2
      • (+2) slightly OTM CALLS (spot < K ≤ upper_1sd) → ×1.5
      • (+1) OTM CALLS → ×0.5 ; ATM/ITM CALLS → ×1.3
      • (0)  all long CALL/PUT → ×0.3

    0DTE Gamma Reflexivity (odte_info):
      • CALL SQUEEZE → 0DTE ATM CALLS ×1.2
      • PUT CASCADE  → 0DTE ATM PUTS  ×1.2

    POV Institutional Urgency (pov_info):
      • Over-participation ≥3× above VWAP → all CALLS ×1.25
    """
    from cost_distribution import BLUE_SKY_TAG, is_blue_sky_breakout
    from strategy_engine import (
        calculate_expected_move,
        recommend_strategy,
    )
    from zero_dte_gex import BOOST_MULT, apply_0dte_boost_mask
    from pov_leakage import URGENCY_CALL_MULT, URGENCY_TAG

    df = df.copy()
    df["Value_Score"] = float("nan")
    df["Status"] = ""
    df["Optimal_Strategy"] = ""
    df["Strategy_Tag"] = ""

    mask = (df["volume"] >= min_volume) & (df["last"] > 0.01)
    work = df[mask].copy()
    if work.empty:
        return df

    if now_et is None:
        now_et = datetime.now(pytz.timezone("US/Eastern"))
    elif now_et.tzinfo is None:
        now_et = pytz.timezone("US/Eastern").localize(now_et)

    today_et = now_et.date()
    after_close = now_et.hour > 16 or (now_et.hour == 16 and now_et.minute >= 15)

    exp_col = "expiry" if "expiry" in work.columns else (
        "Expiry" if "Expiry" in work.columns else None
    )
    if exp_col is not None:
        exp_dates = pd.to_datetime(work[exp_col], errors="coerce").dt.date
        keep = exp_dates > today_et
        if not after_close:
            keep = keep | (exp_dates == today_et)
        work = work[keep]
    else:
        if "dte" in work.columns:
            dte_num = work["dte"].astype(float)
            work = work[dte_num > 0] if after_close else work[dte_num >= 0]
        elif "DTE" in work.columns:
            dte_norm = (
                work["DTE"].astype(str).str.strip().str.lower()
                .str.replace("d", "", regex=False)
            )
            dte_num = pd.to_numeric(dte_norm, errors="coerce")
            work = work[dte_num > 0] if after_close else work[dte_num >= 0]

    if work.empty:
        return df

    if "delta" in work.columns:
        delta_col = work["delta"].fillna(0.5)
    else:
        delta_col = 0.5
    lev = (delta_col * spot_price) / work["last"].replace(0, float("nan"))
    work["_lev"] = lev.fillna(0.0)

    voi_raw = work["volume"] / work["openInterest"].clip(lower=1)
    if "dVol" in work.columns:
        # Known ΔVol → |ΔVol|; new top-30 entrants (NaN) → neutral ×1
        # so they are not rewarded with phantom full-volume surges.
        d_vol = work["dVol"].abs().fillna(1.0)
    else:
        d_vol = work["volume"]
    work["_flow"] = (voi_raw * d_vol).fillna(0.0).clip(lower=0)

    def _minmax(s: pd.Series) -> pd.Series:
        mn, mx = s.min(), s.max()
        if mx <= mn:
            return pd.Series(0.5, index=s.index)
        return (s - mn) / (mx - mn)

    work["_nlev"] = _minmax(work["_lev"])
    work["_nflow"] = _minmax(work["_flow"])
    work["Value_Score"] = work["_nlev"] * 0.4 + work["_nflow"] * 0.6

    side_col = None
    if "side" in work.columns:
        side_col = work["side"].astype(str).str.upper()
    elif "Side" in work.columns:
        side_col = work["Side"].astype(str).str.upper()

    strike_col = None
    if "strike" in work.columns:
        strike_col = pd.to_numeric(work["strike"], errors="coerce")
    elif "Strike" in work.columns:
        strike_col = pd.to_numeric(work["Strike"], errors="coerce")

    if daily_bias and side_col is not None:
        if daily_bias == "HEAVY BEARISH":
            work.loc[side_col == "CALL", "Value_Score"] *= 0.5
        elif daily_bias == "HEAVY BULLISH":
            work.loc[side_col == "PUT", "Value_Score"] *= 0.5

    if market_state and side_col is not None:
        if market_state == "BEARISH DRAG":
            work.loc[side_col == "CALL", "Value_Score"] *= 0.3
        elif market_state == "BULLISH TAILWIND":
            work.loc[side_col == "PUT", "Value_Score"] *= 0.3

    if news_bias and side_col is not None:
        if news_bias == "BEARISH":
            work.loc[side_col == "CALL", "Value_Score"] *= 0.8
            work.loc[side_col == "PUT", "Value_Score"] *= 1.2
        elif news_bias == "BULLISH":
            work.loc[side_col == "CALL", "Value_Score"] *= 1.2
            work.loc[side_col == "PUT", "Value_Score"] *= 0.8

    # VWAP reclaim sniper — align with Daily Bias, push matching side to top
    if vwap_state and daily_bias and side_col is not None:
        if vwap_state == "RECLAIMED UP" and daily_bias == "HEAVY BULLISH":
            work.loc[side_col == "CALL", "Value_Score"] *= 1.5
        elif vwap_state == "RECLAIMED DOWN" and daily_bias == "HEAVY BEARISH":
            work.loc[side_col == "PUT", "Value_Score"] *= 1.5

    # ── Resolve Optimal Strategy + 1SD bounds ─────────────────────────────────
    ctx_iv = None
    if "iv" in work.columns:
        ivs = pd.to_numeric(work["iv"], errors="coerce").dropna()
        if not ivs.empty:
            ctx_iv = float(ivs.median())

    if not optimal_strategy:
        optimal_strategy = recommend_strategy(
            daily_bias,
            ctx_iv,
            profited_shares_pct,
            bool(has_catalyst),
            spot_below_support=bool(spot_below_support),
        )

    u1 = upper_1sd
    l1 = lower_1sd
    if (u1 is None or l1 is None) and ctx_iv is not None and spot_price > 0:
        dte_med = None
        if "dte" in work.columns:
            dtes = pd.to_numeric(work["dte"], errors="coerce").dropna()
            if not dtes.empty:
                dte_med = float(dtes.median())
        if dte_med is not None:
            em = calculate_expected_move(spot_price, ctx_iv, dte_med)
            if u1 is None:
                u1 = em.get("Upper_1SD")
            if l1 is None:
                l1 = em.get("Lower_1SD")

    try:
        u1_f = float(u1) if u1 is not None else None
    except (TypeError, ValueError):
        u1_f = None
    try:
        l1_f = float(l1) if l1 is not None else None
    except (TypeError, ValueError):
        l1_f = None

    work["Optimal_Strategy"] = optimal_strategy or ""
    work["Strategy_Tag"] = ""

    # ── Strategy Engine multipliers (alter ranking) ───────────────────────────
    tags: dict[Any, list[str]] = {idx: [] for idx in work.index}

    if side_col is not None and strike_col is not None and spot_price > 0:
        strat = str(optimal_strategy or "")

        # 1) Dynamic strike filter vs 1SD range
        if u1_f is not None:
            call_lottery = (side_col == "CALL") & (strike_col > u1_f)
            work.loc[call_lottery, "Value_Score"] *= 0.2
            for idx in work.index[call_lottery]:
                tags[idx].append("⚠️ Strike Outside 1SD")
        if l1_f is not None:
            put_lottery = (side_col == "PUT") & (strike_col < l1_f)
            work.loc[put_lottery, "Value_Score"] *= 0.2
            for idx in work.index[put_lottery]:
                tags[idx].append("⚠️ Strike Outside 1SD")

        # 2) Strategy-based multipliers
        if "(+2)" in strat:
            # Slightly OTM calls inside the 1SD upside band
            if u1_f is not None:
                boost_mask = (
                    (side_col == "CALL")
                    & (strike_col > spot_price)
                    & (strike_col <= u1_f)
                )
                work.loc[boost_mask, "Value_Score"] *= 1.5
                for idx in work.index[boost_mask]:
                    tags[idx].append("🎯 Boosted by +2 Outlook")
        elif "(+1)" in strat:
            otm_calls = (side_col == "CALL") & (strike_col > spot_price)
            itm_atm = (side_col == "CALL") & (strike_col <= spot_price)
            work.loc[otm_calls, "Value_Score"] *= 0.5
            work.loc[itm_atm, "Value_Score"] *= 1.3
            for idx in work.index[otm_calls]:
                tags[idx].append("⚠️ Penalized by +1 Outlook")
            for idx in work.index[itm_atm]:
                tags[idx].append("🎯 Boosted by +1 Outlook")
        elif "(0)" in strat:
            directional = (side_col == "CALL") | (side_col == "PUT")
            work.loc[directional, "Value_Score"] *= 0.3
            for idx in work.index[directional]:
                tags[idx].append("⚠️ (0) Outlook — Premium Penalty")

    # 3) 0DTE Gamma reflexivity boost (+20% ATM on squeeze/cascade side)
    boost_side = (odte_info or {}).get("boost_side")
    if (
        boost_side
        and side_col is not None
        and strike_col is not None
        and "dte" in work.columns
    ):
        odte_mask = apply_0dte_boost_mask(
            side_col, strike_col, work["dte"], spot_price, boost_side,
        )
        if odte_mask.any():
            work.loc[odte_mask, "Value_Score"] *= BOOST_MULT
            label = (
                "🚀 0DTE Gamma Squeeze Boost"
                if str(boost_side).upper() == "CALL"
                else "🩸 0DTE Gamma Cascade Boost"
            )
            for idx in work.index[odte_mask]:
                tags[idx].append(label)

    # 4) POV institutional urgency — Calls ×1.25 when magenta spike above VWAP
    if (pov_info or {}).get("urgency") and side_col is not None:
        call_mask = side_col == "CALL"
        if call_mask.any():
            work.loc[call_mask, "Value_Score"] *= URGENCY_CALL_MULT
            for idx in work.index[call_mask]:
                tags[idx].append(URGENCY_TAG)

    work["Strategy_Tag"] = [
        " · ".join(tags[idx]) if tags.get(idx) else ""
        for idx in work.index
    ]

    work["Value_Score"] = work["Value_Score"].round(4)
    work["Status"] = ""
    best_idx = work["Value_Score"].idxmax()
    status = "⭐ BEST VALUE"
    if is_blue_sky_breakout(profited_shares_pct, daily_bias):
        status = f"{status} · {BLUE_SKY_TAG}"
    work.at[best_idx, "Status"] = status

    df.loc[work.index, "Value_Score"] = work["Value_Score"]
    df.loc[work.index, "Status"] = work["Status"]
    df.loc[work.index, "Optimal_Strategy"] = work["Optimal_Strategy"]
    df.loc[work.index, "Strategy_Tag"] = work["Strategy_Tag"]
    return df


def build_best_value_df(
    vol_curr: dict,
    spot: float,
    vol_prev: dict | None,
    min_volume: int = 500,
    daily_bias: str | None = None,
    market_state: str | None = None,
    news_bias: str | None = None,
    vwap_state: str | None = None,
    now_et: datetime | None = None,
    profited_shares_pct: float | None = None,
    *,
    upper_1sd: float | None = None,
    lower_1sd: float | None = None,
    optimal_strategy: str | None = None,
    has_catalyst: bool = False,
    spot_below_support: bool = False,
    odte_info: dict | None = None,
    pov_info: dict | None = None,
) -> pd.DataFrame:
    """Build flat contracts DF from archive volume blocks, then score."""
    rows: list[dict[str, Any]] = []
    for side, key in [("CALL", "top_calls"), ("PUT", "top_puts")]:
        for c in (vol_curr.get(key) or []):
            vol_i = int(c.get("volume") or 0)
            oi_i = max(int(c.get("openInterest") or 0), 1)
            rows.append({
                "side": side,
                "strike": float(c.get("strike") or 0),
                "expiry": c.get("expiry", ""),
                "dte": int(c.get("dte") or 0),
                "last": _contract_price(c),
                "volume": vol_i,
                "openInterest": oi_i,
                "iv": float(c.get("impliedVolatility") or 0),
            })

    if not rows:
        return pd.DataFrame()

    df = attach_dvol(pd.DataFrame(rows), vol_prev)
    return calculate_best_value(
        df,
        spot_price=spot,
        min_volume=min_volume,
        daily_bias=daily_bias,
        market_state=market_state,
        news_bias=news_bias,
        vwap_state=vwap_state,
        now_et=now_et,
        profited_shares_pct=profited_shares_pct,
        upper_1sd=upper_1sd,
        lower_1sd=lower_1sd,
        optimal_strategy=optimal_strategy,
        has_catalyst=has_catalyst,
        spot_below_support=spot_below_support,
        odte_info=odte_info,
        pov_info=pov_info,
    )

def resolve_biases_for_ticker(
    ticker: str,
    session: dict | None,
    spot: float,
) -> tuple[str | None, str | None]:
    """
    Best-effort daily_bias + market_state for non-Streamlit callers
    (Telegram / scheduler). Never raises — returns (None, None) on failure.
    """
    daily_bias = None
    market_state = None
    session = session or {}

    def _daily(open_px, high_px, low_px, close_px):
        body = close_px - open_px
        rng = high_px - low_px
        ratio = 0.0 if abs(rng) < 1e-12 else body / rng
        if ratio <= -0.60:
            return "HEAVY BEARISH"
        if ratio >= 0.60:
            return "HEAVY BULLISH"
        return "NEUTRAL"

    try:
        import data_adapter

        ohlc = data_adapter.fetch_daily_ohlc(ticker)
        if ohlc:
            daily_bias = _daily(
                ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"]
            )
        else:
            open_px = session.get("open")
            high_px = session.get("day_high")
            low_px = session.get("day_low")
            if (
                open_px is not None
                and high_px is not None
                and low_px is not None
                and spot
            ):
                daily_bias = _daily(
                    float(open_px), float(high_px), float(low_px), float(spot)
                )

        macro = data_adapter.fetch_macro_snapshot()
        if macro:
            spy = macro["SPY"]
            qqq = macro["QQQ"]
            vix = macro["VIX"]
            spy_rng = spy["high"] - spy["low"]
            qqq_rng = qqq["high"] - qqq["low"]
            spy_r = (spy["close"] - spy["open"]) / spy_rng if spy_rng else 0.0
            qqq_r = (qqq["close"] - qqq["open"]) / qqq_rng if qqq_rng else 0.0
            vix_prev = vix.get("prev_close")
            if vix_prev and float(vix_prev) > 0:
                vix_chg = (
                    (float(vix["close"]) - float(vix_prev))
                    / float(vix_prev)
                    * 100.0
                )
            else:
                vix_chg = 0.0
            if spy_r <= -0.60 or qqq_r <= -0.60 or vix_chg > 5.0:
                market_state = "BEARISH DRAG"
            elif spy_r >= 0.60 and qqq_r >= 0.60 and vix_chg < -2.0:
                market_state = "BULLISH TAILWIND"
            else:
                market_state = "NEUTRAL"
    except Exception:
        pass

    return daily_bias, market_state
