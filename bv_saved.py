"""bv_saved — the Best Value ranking the scan saved, in the shape the app's pages use.

The scan scores Best Value once (`dailyScaner._score_best_value`) and saves the rows in
the archive (`best_value.rows`, top 30 per DTE pool) together with the inputs it used
(`best_value.inputs`). Pages read that; nothing here scores. No Streamlit.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

NUMERIC = ("strike", "dte", "last", "volume", "openInterest", "dVol", "Value_Score",
           "_nflow", "_nlev", "delta", "iv", "bid", "ask", "extrinsic", "_base_score")

# How the saved inputs moved the scores (best_value.py, config.SCORING multipliers).
_NOTES = {
    ("daily_bias", "HEAVY BEARISH"): "Daily **HEAVY BEARISH** → calls counter-trend −50%",
    ("daily_bias", "HEAVY BULLISH"): "Daily **HEAVY BULLISH** → puts counter-trend −50%",
    ("market_state", "BEARISH DRAG"): "Macro **BEARISH DRAG** → calls counter-trend −70%",
    ("market_state", "BULLISH TAILWIND"): "Macro **BULLISH TAILWIND** → puts counter-trend −70%",
    ("news_bias", "BEARISH"): "News **BEARISH** → CALL ×0.8 · PUT ×1.2",
    ("news_bias", "BULLISH"): "News **BULLISH** → CALL ×1.2 · PUT ×0.8",
}


def saved_block(payload: dict | None) -> dict:
    return (payload or {}).get("best_value") or {}


def saved_rows_df(payload: dict | None) -> pd.DataFrame:
    """The saved rows as a DataFrame, best first within each pool. Missing numbers are
    NaN (never 0). Empty when the scan saved no ranking."""
    rows = saved_block(payload).get("rows") or []
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    for col in NUMERIC:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    for col in ("Status", "Strategy_Tag", "Optimal_Strategy", "side", "expiry"):
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str)
    if "multipliers" not in df.columns:
        df["multipliers"] = [{} for _ in range(len(df))]
    df = df[df["Value_Score"].notna()] if "Value_Score" in df.columns else df.iloc[0:0]
    return df.reset_index(drop=True)


def saved_inputs(payload: dict | None) -> dict[str, Any] | None:
    """{"daily_bias", "market_state", "news_bias"} the scan scored with, or None for a
    scan saved before the inputs were recorded (2026-10-16)."""
    inputs = saved_block(payload).get("inputs")
    return dict(inputs) if isinstance(inputs, dict) else None


def input_notes(inputs: dict[str, Any] | None) -> list[str]:
    """One line per input that moved the scores. None → one line saying it is unknown."""
    if inputs is None:
        return ["Inputs not recorded for this scan (saved before 2026-10-16)"]
    return [text for (key, value), text in _NOTES.items() if inputs.get(key) == value]


def multiplier_text(mults: Any) -> str:
    """'news_with ×1.2 · macro_against ×0.3' for one row, '' when none."""
    if not isinstance(mults, dict) or not mults:
        return ""
    return " · ".join(f"{k} ×{float(v):g}" for k, v in sorted(mults.items()))
