"""
best_value_ui.py — Pure helpers for Best Value table selection → add-position.

No Streamlit imports. app.py renders; tests assert payload mapping.
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def pending_add_pos_payload(
    ticker: str,
    top5: pd.DataFrame,
    selected_rows: list[int] | tuple[int, ...] | None,
) -> dict[str, Any] | None:
    """
    Map a dataframe selection index onto the underlying Best Value frame.

    Selection indices are positions in top5.reset_index(drop=True), which must
    stay aligned with the display frame. Returns None when selection is empty
    or out of range.
    """
    if not selected_rows:
        return None
    try:
        idx = int(selected_rows[0])
    except (TypeError, ValueError, IndexError):
        return None
    if top5 is None or getattr(top5, "empty", True):
        return None
    top5_r = top5.reset_index(drop=True)
    if idx < 0 or idx >= len(top5_r):
        return None
    raw = top5_r.iloc[idx]
    return {
        "Ticker": str(ticker).upper(),
        "Side": str(raw["side"]).upper(),
        "Strike": float(raw["strike"]),
        "Expiry": str(raw["expiry"]),
        "default_price": float(raw["last"]),
    }


def style_best_value_rows(
    disp: pd.DataFrame,
    top5: pd.DataFrame,
) -> "pd.io.formats.style.Styler":
    """Highlight BEST VALUE using Status on the underlying frame (not display)."""
    top5_r = top5.reset_index(drop=True)
    disp_r = disp.reset_index(drop=True)

    def _apply(_df: pd.DataFrame) -> pd.DataFrame:
        styles = pd.DataFrame("", index=_df.index, columns=_df.columns)
        n = min(len(top5_r), len(_df))
        for i in range(n):
            if "BEST VALUE" in str(top5_r.iloc[i].get("Status") or ""):
                styles.iloc[i, :] = "background-color:#1e4620;color:#ffffff"
        return styles

    return disp_r.style.apply(_apply, axis=None)
