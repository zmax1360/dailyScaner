"""
config.py — Single source of truth for scoring thresholds and multipliers.

Every literal that shapes Value_Score lives here. Changing any value changes
config_hash(SCORING) so attribution runs stay segmentable.
"""

from __future__ import annotations


# Fingerprint of the scoring engine — hashed into every attribution run.
# Pre-delta / pre-quality-gate logs are engine v1; runs after this dict
# changes are engine v2 and must not be pooled in analysis.
SCORING: dict[str, float | int] = {
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
}
