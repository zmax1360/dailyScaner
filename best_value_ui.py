"""
best_value_ui.py — Pure helpers for Best Value table selection → add-position.

No Streamlit imports. app.py renders; tests assert payload mapping.
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import Any

import pandas as pd

log = logging.getLogger("best_value_ui")

CONTRACT_KEY_COL = "_bv_key"
STAR_COL = "★"


def contract_key(side: Any, strike: Any, expiry: Any) -> str:
    """Stable identity: SIDE|strike:.4f|expiry."""
    return f"{str(side).upper()}|{float(strike):.4f}|{str(expiry)}"


def contract_key_from_row(row: pd.Series) -> str:
    side = row["side"] if "side" in row.index else row.get("Side")
    strike = row["strike"] if "strike" in row.index else row.get("Strike")
    expiry = row["expiry"] if "expiry" in row.index else row.get("Expiry")
    return contract_key(side, strike, expiry)


def best_value_star(status: Any) -> str:
    return "★" if "BEST VALUE" in str(status or "") else ""


def attach_contract_keys(top5: pd.DataFrame) -> pd.Series:
    """Per-row contract keys aligned with top5.reset_index(drop=True)."""
    top5_r = top5.reset_index(drop=True)
    return top5_r.apply(contract_key_from_row, axis=1)


def _payload_from_raw(ticker: str, raw: pd.Series) -> dict[str, Any]:
    return {
        "Ticker": str(ticker).upper(),
        "Side": str(raw["side"]).upper(),
        "Strike": float(raw["strike"]),
        "Expiry": str(raw["expiry"]),
        "default_price": float(raw["last"]),
    }


def pending_add_pos_payload(
    ticker: str,
    top5: pd.DataFrame,
    selected_rows: list[int] | tuple[int, ...] | None,
    *,
    display: pd.DataFrame | None = None,
    key_col: str = CONTRACT_KEY_COL,
) -> dict[str, Any] | None:
    """
    Resolve selection to a pending add-position payload by contract key.

    Prefer reading ``key_col`` from ``display`` at the selected index (survives
    client-side column sorts). Look the key up in the underlying ``top5`` frame.
    If that key is duplicated in top5, fall back to positional iloc (and warn)
    rather than silently picking the first match.
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
    keys = [contract_key_from_row(top5_r.iloc[i]) for i in range(len(top5_r))]
    counts = Counter(keys)

    if display is not None:
        disp_r = display.reset_index(drop=True)
        if idx < 0 or idx >= len(disp_r):
            return None
        if key_col not in disp_r.columns:
            return None
        sel_key = str(disp_r.iloc[idx][key_col])
    else:
        # Legacy / unit tests: treat top5 order as the display order.
        if idx < 0 or idx >= len(top5_r):
            return None
        sel_key = keys[idx]

    if counts.get(sel_key, 0) == 0:
        return None

    if counts[sel_key] > 1:
        log.warning(
            "duplicate Best Value contract key %r — falling back to positional "
            "index %s (sort-safe identity unavailable for this key)",
            sel_key,
            idx,
        )
        if idx < 0 or idx >= len(top5_r):
            return None
        return _payload_from_raw(ticker, top5_r.iloc[idx])

    # Unique key → identity lookup (order-independent)
    for i, k in enumerate(keys):
        if k == sel_key:
            return _payload_from_raw(ticker, top5_r.iloc[i])
    return None
