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

# Display-only visual gate (do not filter or reorder). Attribution thresholds.
_DELTA_RED = 0.15
_DELTA_AMBER = 0.25
# Post-ranking view filter. Same numbers as Pre-Trade DELTA_MIN / DELTA_MAX.
# Does not change scoring, ranking, or attribution writes.
DISPLAY_DELTA_MIN = 0.35
DISPLAY_DELTA_MAX = 0.50
# TODO(2026-08-28): Signal often renders "(0)" and Optimal Strategy is identical
# across rows even as Value_Score ranges (~0.08–0.29). Consistent with known
# flow_norm / leverage_norm / category-multiplier bugs. Do not fix during the
# measurement window.


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


def _finite_float(value: Any) -> float | None:
    """Parse a provider number; None if missing / non-finite. Never estimates."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def format_abs_delta(delta: Any) -> str:
    """Absolute provider delta, 3 decimals. Missing → '—'."""
    d = _finite_float(delta)
    if d is None:
        return "—"
    return f"{abs(d):.3f}"


def delta_cell_tone(delta: Any) -> str | None:
    """'red' | 'amber' | 'plain', or None when the provider omitted delta."""
    d = _finite_float(delta)
    if d is None:
        return None
    mag = abs(d)
    if mag < _DELTA_RED:
        return "red"
    if mag < _DELTA_AMBER:
        return "amber"
    return "plain"


def delta_in_pretrade_band(
    delta: Any,
    *,
    lo: float = DISPLAY_DELTA_MIN,
    hi: float = DISPLAY_DELTA_MAX,
) -> bool:
    """True when |delta| is inside the Pre-Trade gate. Null/NaN → False."""
    d = _finite_float(delta)
    if d is None:
        return False
    mag = abs(d)
    return lo <= mag <= hi


def ranked_delta_band_mask(
    top5: pd.DataFrame,
    vol_curr: dict | None,
    *,
    lo: float = DISPLAY_DELTA_MIN,
    hi: float = DISPLAY_DELTA_MAX,
) -> list[bool]:
    """
    Per-row keep flags for the ranked scanner table.

    Uses provider ``chain_delta`` (the displayed Delta), never scoring
    ``delta`` and never strike distance. Null delta → False.
    """
    if top5 is None or getattr(top5, "empty", True):
        return []
    attached = attach_chain_greeks(top5, vol_curr)
    return [
        delta_in_pretrade_band(d, lo=lo, hi=hi)
        for d in attached["chain_delta"]
    ]


def apply_display_keep(frame: pd.DataFrame, keep: list[bool]) -> pd.DataFrame:
    """Positional keep. Do not pass a bool list to ``iloc`` (True→1, False→0)."""
    if frame is None or getattr(frame, "empty", True):
        return frame.copy() if frame is not None else pd.DataFrame()
    if len(keep) != len(frame):
        return frame.copy()
    pos = [i for i, k in enumerate(keep) if k]
    return frame.iloc[pos].copy()


def filter_ranked_display(
    top5: pd.DataFrame,
    vol_curr: dict | None,
    *,
    show_all: bool = False,
    lo: float = DISPLAY_DELTA_MIN,
    hi: float = DISPLAY_DELTA_MAX,
) -> tuple[pd.DataFrame, int, int]:
    """
    View filter after ranking. ``top5`` is not modified.

    Returns ``(visible, n_hidden, n_ranked)``. ``show_all=True`` restores
    the full ranked list (hidden=0).
    """
    if top5 is None or getattr(top5, "empty", True):
        empty = top5.copy() if top5 is not None else pd.DataFrame()
        return empty, 0, 0
    n_ranked = len(top5)
    if show_all:
        return top5.copy(), 0, n_ranked
    keep = ranked_delta_band_mask(top5, vol_curr, lo=lo, hi=hi)
    visible = apply_display_keep(top5, keep)
    n_hidden = n_ranked - len(visible)
    return visible, n_hidden, n_ranked


def hidden_delta_band_caption(n_hidden: int, n_ranked: int) -> str:
    return f"{n_hidden} of {n_ranked} hidden: outside delta band"


def format_delta_cell(delta: Any) -> str:
    """
    Display Delta. Colour travels in the cell text so a Streamlit header
    sort cannot desync CSS (Styler row-index bug in 1.37.1).
    """
    formatted = format_abs_delta(delta)
    if formatted == "—":
        return "—"
    tone = delta_cell_tone(delta)
    if tone == "red":
        return f"🔴 {formatted}"
    if tone == "amber":
        return f"🟠 {formatted}"
    return formatted


def is_fully_extrinsic(extrinsic: Any, price: Any) -> bool:
    """True when extrinsic/price rounds to 100.0% — same rule as Pre-Trade."""
    p = _finite_float(price)
    e = _finite_float(extrinsic)
    if p is None or p <= 0 or e is None:
        return False
    return round((e / p) * 100, 1) == 100.0


def format_theta_prem(theta: Any, price: Any) -> str:
    """theta / contract price as a percent per day. Missing or price≤0 → '—'."""
    t = _finite_float(theta)
    p = _finite_float(price)
    if t is None or p is None or p <= 0:
        return "—"
    return f"{(t / p):.1%}"


def ranking_identity_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Value_Score, Signal, and contract row order — ranking identity only."""
    work = df.copy()
    if "Signal" not in work.columns and "Action_Signal" in work.columns:
        work = work.rename(columns={"Action_Signal": "Signal"})
    cols = [
        c for c in ("side", "strike", "expiry", "Value_Score", "Signal")
        if c in work.columns
    ]
    return work.loc[:, cols].reset_index(drop=True)


def ranking_identity_bytes(df: pd.DataFrame) -> bytes:
    """Byte identity of ranking columns (csv, no index)."""
    return ranking_identity_frame(df).to_csv(index=False).encode("utf-8")


def _greeks_lookup(vol_curr: dict | None) -> dict[str, tuple[Any, Any]]:
    """(side, strike, expiry) → (delta, theta) from the already-fetched chain."""
    out: dict[str, tuple[Any, Any]] = {}
    block = vol_curr or {}
    for side, key in (("CALL", "top_calls"), ("PUT", "top_puts")):
        for c in block.get(key) or []:
            if not isinstance(c, dict):
                continue
            try:
                k = contract_key(side, c.get("strike"), c.get("expiry"))
            except (TypeError, ValueError):
                continue
            out[k] = (c.get("delta"), c.get("theta"))
    return out


def attach_chain_greeks(
    top5: pd.DataFrame,
    vol_curr: dict | None,
) -> pd.DataFrame:
    """
    Join provider greeks from the same chain the scanner already fetched.

    Adds ``chain_delta`` / ``chain_theta`` only. Does not write scoring
    ``delta``, Value_Score, Signal, or change row order.
    """
    out = top5.copy()
    lookup = _greeks_lookup(vol_curr)
    deltas: list[Any] = []
    thetas: list[Any] = []
    for _, row in out.iterrows():
        d, t = lookup.get(contract_key_from_row(row), (None, None))
        deltas.append(d)
        thetas.append(t)
    out["chain_delta"] = deltas
    out["chain_theta"] = thetas
    return out


def greeks_display_columns(
    top5: pd.DataFrame,
    vol_curr: dict | None,
) -> pd.DataFrame:
    """
    Display-only Delta and Theta/Prem, aligned to ``top5``'s index.

    Sourced from chain records. Scoring ``delta`` is ignored. Missing
    provider greeks render as '—' (no estimate, no raise).
    """
    if top5 is None or getattr(top5, "empty", True):
        return pd.DataFrame(columns=["Delta", "Theta/Prem"])
    attached = attach_chain_greeks(top5, vol_curr)
    prices = attached["last"] if "last" in attached.columns else [None] * len(attached)
    return pd.DataFrame(
        {
            "Delta": [format_delta_cell(v) for v in attached["chain_delta"]],
            "Theta/Prem": [
                format_theta_prem(t, p)
                for t, p in zip(attached["chain_theta"], prices)
            ],
        },
        index=attached.index,
    )


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
