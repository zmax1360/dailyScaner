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

from config import SCORING
from scoring_pool import (
    DEFAULT_MIN_POOL_SIZE,
    POOL_0DTE,
    POOL_1DTE,
    scoring_pool,
)


def _contract_price(c: dict) -> float:
    """Prefer mid(bid, ask) only when both are real quotes; else lastPrice.

    Never treat NaN bid/ask as a zero-width spread.
    """
    try:
        bid = float(c["bid"]) if c.get("bid") is not None else float("nan")
    except (TypeError, ValueError):
        bid = float("nan")
    try:
        ask = float(c["ask"]) if c.get("ask") is not None else float("nan")
    except (TypeError, ValueError):
        ask = float("nan")
    if bid == bid and ask == ask and bid > 0 and ask > 0:
        return (bid + ask) / 2.0
    last = float(c.get("lastPrice") or c.get("last") or 0)
    return last if last == last and last > 0 else 0.0


def attach_dvol(
    df: pd.DataFrame,
    vol_prev: dict | None,
    *,
    eod_vol_lookup: dict | None = None,
    now_et: datetime | None = None,
    cfg: dict | None = None,
    volume_is_session_scoped: bool = False,
    current_source: str = "yahoo",
    prev_archive_source: str | None = None,
    eod_archive_source: str | None = None,
) -> pd.DataFrame:
    """
    Attach ΔVol vs previous archive top-30 snapshot.

    Contracts missing from the previous top-30 get dVol = NaN (unknown),
    NOT volume - 0. Treating absence as prev=0 made new entrants look like
    full-volume surges and systematically win BEST VALUE (phantom ΔVol).

    Detectors (both set dVol=NaN + dvol_suspect; never write 0):
      1. Decrease vs prior scan — contract already rolled (Task A)
      2. EOD-match — volume still >= ratio * prior-day EOD (not yet rolled)

    When ``volume_is_session_scoped`` is True (e.g. Massive), both detectors
    are dormant — session volume is already clean.

    Cross-source comparisons are skipped (Yahoo vs Massive volume semantics
    differ); missing archive ``source`` is treated as yahoo.

    stale_volume=True only for detector (2). Counts logged via attrs on the frame.
    """
    from chain_quality import (
        archive_source_name,
        is_volume_stale_vs_eod,
        rollover_detectors_active,
        stale_check_active,
    )
    from config import SCORING

    cfg = cfg or SCORING
    df = df.copy()
    if "dvol_suspect" not in df.columns:
        df["dvol_suspect"] = False
    if "stale_volume" not in df.columns:
        df["stale_volume"] = False

    if not rollover_detectors_active(volume_is_session_scoped):
        if "dVol" not in df.columns:
            df["dVol"] = float("nan")
        df.attrs["n_decrease_suspect"] = 0
        df.attrs["n_eod_stale"] = 0
        df.attrs["rollover_detectors"] = "dormant_session_scoped"
        return df

    curr_src = archive_source_name({"source": current_source})
    prev_src = archive_source_name({"source": prev_archive_source})
    eod_src = archive_source_name({"source": eod_archive_source})

    use_prev = bool(vol_prev) and prev_src == curr_src
    use_eod_lookup = bool(eod_vol_lookup) and eod_src == curr_src

    if vol_prev and not use_prev:
        df.attrs["prev_source_compare"] = (
            f"skipped_mismatch:{prev_src}->{curr_src}"
        )
    if eod_vol_lookup and not use_eod_lookup:
        df.attrs["eod_source_compare"] = (
            f"skipped_mismatch:{eod_src}->{curr_src}"
        )

    if not use_prev and not use_eod_lookup:
        if "dVol" not in df.columns:
            df["dVol"] = float("nan")
        df.attrs["n_decrease_suspect"] = 0
        df.attrs["n_eod_stale"] = 0
        if vol_prev or eod_vol_lookup:
            df.attrs["rollover_detectors"] = "skipped_source_mismatch"
        return df

    prev_lookup: dict[tuple, int] = {}
    if use_prev and vol_prev:
        for side, key in [("CALL", "top_calls"), ("PUT", "top_puts")]:
            for c in (vol_prev.get(key) or []):
                k = (side, float(c.get("strike") or 0), c.get("expiry", ""))
                prev_lookup[k] = int(c.get("volume") or 0)

    eod_lookup = eod_vol_lookup if use_eod_lookup else {}
    do_eod = bool(eod_lookup) and (
        now_et is None or stale_check_active(now_et, cfg=cfg)
    )

    n_decrease = 0
    n_eod_stale = 0
    dvols: list[float] = []
    suspects: list[bool] = []
    stales: list[bool] = []

    for _, r in df.iterrows():
        k = (r["side"], float(r["strike"]), r["expiry"])
        curr_v = float(r["volume"])
        suspect = False
        stale = False
        dvol = float("nan")

        if k in prev_lookup:
            prev_v = float(prev_lookup[k])
            if curr_v < prev_v:
                # Yahoo rolled prior-session cumulative volume off this contract
                n_decrease += 1
                suspect = True
                dvol = float("nan")
            else:
                dvol = curr_v - prev_v
        # else: new entrant — dVol stays NaN, not suspect

        if (
            not suspect
            and do_eod
            and k in eod_lookup
            and is_volume_stale_vs_eod(curr_v, eod_lookup[k], cfg=cfg)
        ):
            n_eod_stale += 1
            suspect = True
            stale = True
            dvol = float("nan")

        dvols.append(dvol)
        suspects.append(suspect)
        stales.append(stale)

    df["dVol"] = dvols
    df["dvol_suspect"] = suspects
    df["stale_volume"] = stales
    df.attrs["n_decrease_suspect"] = n_decrease
    df.attrs["n_eod_stale"] = n_eod_stale
    return df


def calculate_best_value(
    df: pd.DataFrame,
    spot_price: float,
    min_volume: int | None = None,
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

    Appends Value_Score, Status, Optimal_Strategy, Strategy_Tag,
    delta, _lev, _flow, _nlev, _nflow, _base_score, _multipliers, extrinsic
    (per-row scoring inputs for attribution pass-through).
    Expired contracts are always dropped; after 16:15 ET, same-day 0DTE
    is also dropped.

    Multiplier values come from config.SCORING — do not retune here.
    """
    from cost_distribution import BLUE_SKY_TAG, is_blue_sky_breakout
    from strategy_engine import (
        calculate_expected_move,
        is_straddle_strategy,
        is_unknown_strategy,
        recommend_strategy,
        strategy_outlook,
    )
    from zero_dte_gex import apply_0dte_boost_mask
    from pov_leakage import URGENCY_TAG

    import logging
    _log = logging.getLogger("best_value")

    cfg = SCORING
    w_lev = float(cfg["w_lev"])
    w_flow = float(cfg["w_flow"])
    mv = int(cfg["min_volume"] if min_volume is None else min_volume)
    min_last = float(cfg["min_last"])
    m_heavy = float(cfg["mult_heavy_bias_against"])
    m_macro = float(cfg["mult_macro_against"])
    m_news_against = float(cfg["mult_news_against"])
    m_news_with = float(cfg["mult_news_with"])
    m_vwap = float(cfg["mult_vwap_sniper"])
    m_1sd = float(cfg["mult_outside_1sd"])
    m_p2 = float(cfg["mult_plus2_boost"])
    m_p1_otm = float(cfg["mult_plus1_otm"])
    m_p1_itm = float(cfg["mult_plus1_itm"])
    m_m2 = float(cfg["mult_minus2_boost"])
    m_m1_otm = float(cfg["mult_minus1_otm"])
    m_m1_itm = float(cfg["mult_minus1_itm"])
    m_straddle = float(cfg.get("mult_straddle_atm", 1.3))
    m_zero = float(cfg["mult_zero_outlook"])
    m_0dte = float(cfg["mult_0dte_boost"])
    m_pov = float(cfg["mult_pov_urgency"])

    df = df.copy()
    df["Value_Score"] = float("nan")
    df["Status"] = ""
    df["Optimal_Strategy"] = ""
    df["Strategy_Tag"] = ""
    df["_nlev"] = float("nan")
    df["_nflow"] = float("nan")
    df["_lev"] = float("nan")
    df["_flow"] = float("nan")
    df["_base_score"] = float("nan")
    df["_multipliers"] = None
    df["delta"] = float("nan")
    df["extrinsic"] = float("nan")
    df["pool"] = None
    df["_rank"] = pd.NA

    mask = (df["volume"] >= mv) & (df["last"] > min_last)
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

    from chain_quality import iv_degraded_for_1sd
    from greeks import bs_delta, effective_dte_days

    # Emit / refresh delta via Black-Scholes (never default to 0.5)
    r_free = float(cfg.get("risk_free_rate", 0.045))

    deltas: list[float] = []
    for _, r in work.iterrows():
        side = r.get("side") or r.get("Side") or ""
        strike = r.get("strike") if "strike" in r.index else r.get("Strike")
        iv = r.get("iv") if "iv" in r.index else r.get("impliedVolatility")
        dte = r.get("dte") if "dte" in r.index else r.get("DTE")
        exp = r.get("expiry") if "expiry" in r.index else r.get("Expiry")
        try:
            d = bs_delta(
                str(side),
                float(spot_price),
                float(strike or 0),
                effective_dte_days(dte, expiry=exp, now_et=now_et),
                float(iv if iv is not None else 0),
                r=r_free,
            )
        except (TypeError, ValueError):
            d = None
        deltas.append(float("nan") if d is None else float(d))
    work["delta"] = deltas

    # Leverage: magnitude of exposure per premium dollar. Direction is carried
    # by side + directional multipliers — signed put delta must not floor the
    # put universe at _minmax min (engine-v1.1 / abs-delta).
    # Exclude null-delta rows; renormalise over survivors. No default (F-01).
    work["_lev"] = float("nan")
    has_delta = work["delta"].notna()
    if has_delta.any():
        lev = (
            work.loc[has_delta, "delta"].abs() * spot_price
        ) / work.loc[has_delta, "last"].replace(0, float("nan"))
        work.loc[has_delta, "_lev"] = lev

    voi_raw = work["volume"].astype(float) / work["openInterest"].clip(lower=1)
    # Stale Yahoo volume is not a measurement — exclude from volume-derived flow
    # (NaN, never 0). Decrease-suspect keeps volume; only dVol is unknown.
    if "stale_volume" in work.columns:
        voi_raw = voi_raw.where(~work["stale_volume"].astype(bool), float("nan"))
    if "dVol" in work.columns:
        # Direction of flow is signal — do NOT abs(). New entrants (NaN) → ×1
        # (F-05 fillna(1.0) left unchanged). Stale rows keep NaN dVol (no fill).
        d_vol = work["dVol"].copy()
        if "stale_volume" in work.columns:
            fill = d_vol.isna() & ~work["stale_volume"].astype(bool)
            d_vol = d_vol.where(~fill, 1.0)
        else:
            d_vol = d_vol.fillna(1.0)
    else:
        d_vol = work["volume"]
    work["_flow"] = (voi_raw * d_vol).clip(lower=0)
    # NaN flow (stale) stays NaN — do not fillna(0); excluded from nflow minmax below

    def _minmax(s: pd.Series) -> pd.Series:
        mn, mx = s.min(), s.max()
        if mx <= mn:
            return pd.Series(0.5, index=s.index)
        return (s - mn) / (mx - mn)

    # Pool assignment (engine-v1.2) — NULL dte never silently pooled
    dte_col = work["dte"] if "dte" in work.columns else (
        work["DTE"] if "DTE" in work.columns else None
    )
    pools: list[str | None] = []
    if dte_col is None:
        pools = [None] * len(work)
        _log.warning("dte column missing — all rows excluded from pool ranking")
    else:
        n_excl = 0
        for v in dte_col.tolist():
            p = scoring_pool(v)
            if p is None:
                n_excl += 1
            pools.append(p)
        if n_excl:
            _log.info(
                "null/invalid dte — %d rows excluded from ranking (not pooled)",
                n_excl,
            )
    work["pool"] = pools

    work["_nlev"] = float("nan")
    work["_nflow"] = float("nan")
    work["Value_Score"] = float("nan")
    flow_ok = work["_flow"].notna()
    eligible = has_delta & flow_ok & work["pool"].notna()

    min_pool = int(cfg.get("min_pool_size", DEFAULT_MIN_POOL_SIZE))
    ranked_pools: list[str] = []
    for pool_name in (POOL_0DTE, POOL_1DTE):
        in_pool = eligible & (work["pool"] == pool_name)
        n_pool = int(in_pool.sum())
        if n_pool < min_pool:
            if n_pool > 0:
                _log.info(
                    "pool %s has %d survivors (< %d) — not ranked (not merged)",
                    pool_name, n_pool, min_pool,
                )
            continue
        # Independent min-max within this pool only
        work.loc[in_pool, "_nlev"] = _minmax(
            work.loc[in_pool, "_lev"].astype(float)
        )
        work.loc[in_pool, "_nflow"] = _minmax(
            work.loc[in_pool, "_flow"].astype(float)
        )
        work.loc[in_pool, "Value_Score"] = (
            work.loc[in_pool, "_nlev"] * w_lev
            + work.loc[in_pool, "_nflow"] * w_flow
        )
        ranked_pools.append(pool_name)

    # Pre-multiplier blend — persisted as base_score (do not recompute in log_run)
    work["_base_score"] = work["Value_Score"].copy()
    work.attrs["ranked_pools"] = ranked_pools

    # Extrinsic value (instrumentation): mid - intrinsic
    work["extrinsic"] = float("nan")
    for idx, r in work.iterrows():
        side = str(r.get("side") or r.get("Side") or "").upper()
        strike = r.get("strike") if "strike" in r.index else r.get("Strike")
        try:
            strike_f = float(strike)
        except (TypeError, ValueError):
            continue
        bid = r.get("bid")
        ask = r.get("ask")
        last = r.get("last", r.get("lastPrice"))
        mid = None
        try:
            b = float(bid) if bid is not None and pd.notna(bid) else 0.0
            a = float(ask) if ask is not None and pd.notna(ask) else 0.0
            if b > 0 and a > 0:
                mid = (b + a) / 2.0
            elif last is not None and pd.notna(last) and float(last) > 0:
                mid = float(last)
        except (TypeError, ValueError):
            mid = None
        if mid is None:
            continue
        if side == "CALL":
            intrinsic = max(float(spot_price) - strike_f, 0.0)
        elif side == "PUT":
            intrinsic = max(strike_f - float(spot_price), 0.0)
        else:
            continue
        work.at[idx, "extrinsic"] = mid - intrinsic

    # Per-row multiplier breakdown (product must reproduce Value_Score)
    mults: dict[Any, dict[str, float]] = {
        idx: {"_base": 1.0} for idx in work.index
    }

    def _apply(mask_s: pd.Series, key: str, value: float) -> None:
        if mask_s is None or not bool(mask_s.any()):
            return
        work.loc[mask_s, "Value_Score"] *= value
        for idx in work.index[mask_s]:
            mults[idx][key] = float(value)

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
            _apply(side_col == "CALL", "heavy_bias_against", m_heavy)
        elif daily_bias == "HEAVY BULLISH":
            _apply(side_col == "PUT", "heavy_bias_against", m_heavy)

    if market_state and side_col is not None:
        if market_state == "BEARISH DRAG":
            _apply(side_col == "CALL", "macro_against", m_macro)
        elif market_state == "BULLISH TAILWIND":
            _apply(side_col == "PUT", "macro_against", m_macro)

    if news_bias and side_col is not None:
        if news_bias == "BEARISH":
            _apply(side_col == "CALL", "news_against", m_news_against)
            _apply(side_col == "PUT", "news_with", m_news_with)
        elif news_bias == "BULLISH":
            _apply(side_col == "CALL", "news_with", m_news_with)
            _apply(side_col == "PUT", "news_against", m_news_against)

    # VWAP reclaim sniper — align with Daily Bias, push matching side to top
    if vwap_state and daily_bias and side_col is not None:
        if vwap_state == "RECLAIMED UP" and daily_bias == "HEAVY BULLISH":
            _apply(side_col == "CALL", "vwap_sniper", m_vwap)
        elif vwap_state == "RECLAIMED DOWN" and daily_bias == "HEAVY BEARISH":
            _apply(side_col == "PUT", "vwap_sniper", m_vwap)

    # ── Resolve Optimal Strategy + 1SD bounds ─────────────────────────────────
    ctx_iv = None
    iv_degraded = True
    if "iv" in work.columns:
        ivs = pd.to_numeric(work["iv"], errors="coerce")
        iv_degraded = iv_degraded_for_1sd(ivs.tolist())
        if not iv_degraded:
            usable = ivs[ivs >= float(cfg.get("min_iv_usable", 0.01))]
            if not usable.empty:
                ctx_iv = float(usable.median())

    if not optimal_strategy:
        if daily_bias is None or not str(daily_bias).strip():
            _log.warning(
                "daily_bias unavailable — strategy multipliers skipped "
                "(not treating as NEUTRAL / iron condor)"
            )
        optimal_strategy = recommend_strategy(
            daily_bias,
            ctx_iv,
            profited_shares_pct,
            bool(has_catalyst),
            spot_below_support=bool(spot_below_support),
        )

    u1 = upper_1sd
    l1 = lower_1sd
    # Task B: when IV is degraded chain-wide, do not compute 1SD bands at all
    if iv_degraded:
        u1, l1 = None, None
    elif (u1 is None or l1 is None) and ctx_iv is not None and spot_price > 0:
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
    bias_unknown = is_unknown_strategy(optimal_strategy)
    work["_bias_unknown"] = bool(bias_unknown)

    # ── Strategy Engine multipliers (alter ranking) ───────────────────────────
    tags: dict[Any, list[str]] = {idx: [] for idx in work.index}

    if side_col is not None and strike_col is not None and spot_price > 0:
        strat = str(optimal_strategy or "")
        outlook = strategy_outlook(strat)

        # 1) Dynamic strike filter vs 1SD range
        if u1_f is not None:
            call_lottery = (side_col == "CALL") & (strike_col > u1_f)
            _apply(call_lottery, "outside_1sd", m_1sd)
            for idx in work.index[call_lottery]:
                tags[idx].append("⚠️ Strike Outside 1SD")
        if l1_f is not None:
            put_lottery = (side_col == "PUT") & (strike_col < l1_f)
            _apply(put_lottery, "outside_1sd", m_1sd)
            for idx in work.index[put_lottery]:
                tags[idx].append("⚠️ Strike Outside 1SD")

        # 2) Strategy-based multipliers — branch on integer outlook (F-04)
        if bias_unknown:
            for idx in work.index:
                tags[idx].append("⚠️ bias unavailable")
        elif outlook == 2:
            # Slightly OTM calls inside the 1SD upside band
            if u1_f is not None:
                boost_mask = (
                    (side_col == "CALL")
                    & (strike_col > spot_price)
                    & (strike_col <= u1_f)
                )
                _apply(boost_mask, "plus2_boost", m_p2)
                for idx in work.index[boost_mask]:
                    tags[idx].append("🎯 Boosted by +2 Outlook")
        elif outlook == 1:
            otm_calls = (side_col == "CALL") & (strike_col > spot_price)
            itm_atm = (side_col == "CALL") & (strike_col <= spot_price)
            _apply(otm_calls, "plus1_otm", m_p1_otm)
            _apply(itm_atm, "plus1_itm", m_p1_itm)
            for idx in work.index[otm_calls]:
                tags[idx].append("⚠️ Penalized by +1 Outlook")
            for idx in work.index[itm_atm]:
                tags[idx].append("🎯 Boosted by +1 Outlook")
        elif outlook == 0:
            directional = (side_col == "CALL") | (side_col == "PUT")
            _apply(directional, "zero_outlook", m_zero)
            for idx in work.index[directional]:
                tags[idx].append("⚠️ (0) Outlook — Premium Penalty")
        elif outlook == -2:
            # Mirror of +2: slightly OTM puts inside the 1SD downside band
            if l1_f is not None:
                boost_mask = (
                    (side_col == "PUT")
                    & (strike_col < spot_price)
                    & (strike_col >= l1_f)
                )
                _apply(boost_mask, "minus2_boost", m_m2)
                for idx in work.index[boost_mask]:
                    tags[idx].append("🎯 Boosted by -2 Outlook")
        elif outlook == -1:
            otm_puts = (side_col == "PUT") & (strike_col < spot_price)
            itm_atm_puts = (side_col == "PUT") & (strike_col >= spot_price)
            _apply(otm_puts, "minus1_otm", m_m1_otm)
            _apply(itm_atm_puts, "minus1_itm", m_m1_itm)
            for idx in work.index[otm_puts]:
                tags[idx].append("⚠️ Penalized by -1 Outlook")
            for idx in work.index[itm_atm_puts]:
                tags[idx].append("🎯 Boosted by -1 Outlook")
        elif is_straddle_strategy(strat):
            # Vol expansion: boost near-ATM on BOTH sides (explicit, not fall-through)
            if u1_f is not None and l1_f is not None:
                atm_mask = (strike_col >= l1_f) & (strike_col <= u1_f)
            else:
                atm_mask = (strike_col - spot_price).abs() / max(spot_price, 1e-9) <= 0.03
            _apply(atm_mask, "straddle_atm", m_straddle)
            for idx in work.index[atm_mask]:
                tags[idx].append("🎯 Boosted by Straddle (near-ATM)")
        # else: unrecognised label with outlook None — no strategy multiplier

    # 3) 0DTE Gamma reflexivity boost (ATM on squeeze/cascade side)
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
            _apply(odte_mask, "odte_boost", m_0dte)
            label = (
                "🚀 0DTE Gamma Squeeze Boost"
                if str(boost_side).upper() == "CALL"
                else "🩸 0DTE Gamma Cascade Boost"
            )
            for idx in work.index[odte_mask]:
                tags[idx].append(label)

    # 4) POV institutional urgency — Calls when magenta spike above VWAP
    if (pov_info or {}).get("urgency") and side_col is not None:
        call_mask = side_col == "CALL"
        if call_mask.any():
            _apply(call_mask, "pov_urgency", m_pov)
            for idx in work.index[call_mask]:
                tags[idx].append(URGENCY_TAG)

    work["Strategy_Tag"] = [
        " · ".join(tags[idx]) if tags.get(idx) else ""
        for idx in work.index
    ]
    work["_multipliers"] = [mults[idx] for idx in work.index]

    work["Value_Score"] = work["Value_Score"].round(4)
    work["Status"] = ""
    work["_rank"] = pd.NA
    # Rank within pool + one BEST VALUE star per ranked pool
    for pool_name in (POOL_0DTE, POOL_1DTE):
        pool_scored = work.loc[
            work["pool"].eq(pool_name) & work["Value_Score"].notna(),
            "Value_Score",
        ].sort_values(ascending=False)
        if pool_scored.empty:
            continue
        for rank_i, idx in enumerate(pool_scored.index, start=1):
            work.at[idx, "_rank"] = rank_i
        best_idx = pool_scored.index[0]
        status = f"⭐ BEST VALUE · {pool_name}"
        if is_blue_sky_breakout(profited_shares_pct, daily_bias):
            status = f"{status} · {BLUE_SKY_TAG}"
        work.at[best_idx, "Status"] = status

    df.loc[work.index, "Value_Score"] = work["Value_Score"]
    df.loc[work.index, "Status"] = work["Status"]
    df.loc[work.index, "Optimal_Strategy"] = work["Optimal_Strategy"]
    df.loc[work.index, "Strategy_Tag"] = work["Strategy_Tag"]
    df.loc[work.index, "_nlev"] = work["_nlev"]
    df.loc[work.index, "_nflow"] = work["_nflow"]
    df.loc[work.index, "_multipliers"] = work["_multipliers"]
    df.loc[work.index, "delta"] = work["delta"]
    # Pass-through scoring inputs for attribution (do not recompute in log_run)
    df.loc[work.index, "_lev"] = work["_lev"]
    df.loc[work.index, "_flow"] = work["_flow"]
    df.loc[work.index, "_base_score"] = work["_base_score"]
    df.loc[work.index, "extrinsic"] = work["extrinsic"]
    df.loc[work.index, "pool"] = work["pool"]
    df.loc[work.index, "_rank"] = work["_rank"]
    if "_bias_unknown" in work.columns:
        df.loc[work.index, "_bias_unknown"] = work["_bias_unknown"]
    if "dvol_suspect" in work.columns:
        df.loc[work.index, "dvol_suspect"] = work["dvol_suspect"]
    if "stale_volume" in work.columns:
        df.loc[work.index, "stale_volume"] = work["stale_volume"]
    return df


def build_best_value_df(
    vol_curr: dict,
    spot: float,
    vol_prev: dict | None,
    min_volume: int | None = None,
    daily_bias: str | None = None,
    market_state: str | None = None,
    news_bias: str | None = None,
    vwap_state: str | None = None,
    now_et: datetime | None = None,
    profited_shares_pct: float | None = None,
    *,
    eod_vol_lookup: dict | None = None,
    volume_is_session_scoped: bool = False,
    current_source: str = "yahoo",
    prev_archive_source: str | None = None,
    eod_archive_source: str | None = None,
    upper_1sd: float | None = None,
    lower_1sd: float | None = None,
    optimal_strategy: str | None = None,
    has_catalyst: bool = False,
    spot_below_support: bool = False,
    odte_info: dict | None = None,
    pov_info: dict | None = None,
) -> pd.DataFrame:
    """Build flat contracts DF from archive volume blocks, then score."""
    from greeks import bs_delta, effective_dte_days

    rows: list[dict[str, Any]] = []
    r_free = float(SCORING.get("risk_free_rate", 0.045))
    for side, key in [("CALL", "top_calls"), ("PUT", "top_puts")]:
        for c in (vol_curr.get(key) or []):
            vol_i = int(c.get("volume") or 0)
            oi_i = max(int(c.get("openInterest") or 0), 1)
            dte_i = int(c.get("dte") or 0)
            iv_f = float(c.get("impliedVolatility") or 0)
            strike_f = float(c.get("strike") or 0)
            exp = c.get("expiry", "")
            t_days = effective_dte_days(dte_i, expiry=exp, now_et=now_et)
            d = bs_delta(side, float(spot), strike_f, t_days, iv_f, r=r_free)
            def _quote_f(key: str) -> float:
                v = c.get(key)
                if v is None:
                    return float("nan")
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return float("nan")

            rows.append({
                "side": side,
                "strike": strike_f,
                "expiry": exp,
                "dte": dte_i,
                "last": _contract_price(c),
                "bid": _quote_f("bid"),
                "ask": _quote_f("ask"),
                "volume": vol_i,
                "openInterest": oi_i,
                "iv": iv_f,
                "delta": float("nan") if d is None else float(d),
            })

    if not rows:
        return pd.DataFrame()

    df = attach_dvol(
        pd.DataFrame(rows),
        vol_prev,
        eod_vol_lookup=eod_vol_lookup,
        now_et=now_et,
        volume_is_session_scoped=volume_is_session_scoped,
        current_source=current_source,
        prev_archive_source=prev_archive_source,
        eod_archive_source=eod_archive_source,
    )
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
    except Exception as exc:
        log = __import__("logging").getLogger("best_value")
        log.warning(
            "resolve_biases_for_ticker(%s) failed: %s: %s",
            ticker,
            type(exc).__name__,
            exc,
        )

    return daily_bias, market_state
