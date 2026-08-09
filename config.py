"""
config.py — Single source of truth for scoring thresholds and multipliers.

Every literal that shapes Value_Score lives here. Changing any value changes
config_hash(SCORING) so attribution runs stay segmentable.
"""

from __future__ import annotations


# Fingerprint of the scoring engine — hashed into every attribution run.
# Segment analysis by config_hash / engine_tag — never pool across versions.
SCORING: dict[str, float | int | str] = {
    # engine-v1.2: separate 0DTE / 1DTE+ normalisation pools.
    # Prior: v1.1 = 1e191ea1832c2c9a (abs-delta); v1 = dc2906741dbb2b15.
    "engine_tag": "engine-v1.2",
    # Min survivors (delta+flow) to rank a DTE pool; below → no picks (not merged).
    "min_pool_size": 5,
    # Base blend
    "w_lev": 0.4,
    "w_flow": 0.6,
    # Universe gates (scoring path + magnet filter)
    "min_volume": 500,
    "min_oi_for_magnet": 500,
    "min_last": 0.01,
    # Daily bias (against the heavy side)
    "mult_heavy_bias_against": 0.5,
    # Macro drag / tailwind (against the opposing side)
    "mult_macro_against": 0.3,
    # News alignment
    "mult_news_against": 0.8,
    "mult_news_with": 1.2,
    # VWAP reclaim sniper
    "mult_vwap_sniper": 1.5,
    # Strategy engine / 1SD
    "mult_outside_1sd": 0.2,
    "mult_plus2_boost": 1.5,
    "mult_plus1_otm": 0.5,
    "mult_plus1_itm": 1.3,
    "mult_zero_outlook": 0.3,
    # Bearish mirrors of plus2 / plus1 (F-04) — do not retune bullish values
    "mult_minus2_boost": 1.5,
    "mult_minus1_otm": 0.5,
    "mult_minus1_itm": 1.3,
    # Vol-expansion (straddle): boost near-ATM on both sides
    "mult_straddle_atm": 1.3,
    # 0DTE gamma reflexivity
    "mult_0dte_boost": 1.20,
    # POV institutional urgency
    "mult_pov_urgency": 1.25,
    # Risk-free rate for Black-Scholes delta (do not fetch per-scan).
    # Delta is insensitive to r at the short maturities we score.
    "risk_free_rate": 0.045,
    # Per-contract quality gate (Task B) — covered by config_hash
    "min_iv_usable": 0.01,
    "quality_top_n": 30,
    "max_unusable_frac": 0.20,
    # Stale Yahoo volume vs prior EOD (CURSOR_STALE_VOLUME_FIX)
    "stale_volume_ratio": 0.95,
    "stale_check_cutoff_et": "11:00",
    "stale_majority_abort_frac": 0.50,
    # Market data adapter (CURSOR_SOURCES_STEPS) — does not change scoring math
    "market_data_source": "yahoo",
    # Massive options snapshot pagination (CURSOR_SOURCE_AWARE_ROLLOVER)
    "massive_max_pages": 20,
    # Massive near-ATM strike window (Starter-friendly; narrower than full chain)
    "massive_strike_window_pct": 0.06,
    "massive_max_strikes_per_expiry": 20,
    # mark_runner — wall-clock cap so launchd StartInterval gets a clean slot
    "mark_runner_max_runtime_sec": 600,
    "mark_runner_socket_timeout_sec": 30,
    # health_check: fail if MAX(marked_t1h_at) older than this during RTH
    "mark_t1h_health_max_age_min": 90,
}
