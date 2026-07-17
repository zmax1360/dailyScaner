"""
snapshot_store.py — file-based persistence for the dashboard.

Manages two JSON files:
  flow_snapshot.json  — last full chain snapshot for delta computation
  gate_history.json   — last 20 spread-gate evaluations

No market analysis lives here: only serialisation, deserialisation,
and purely arithmetic delta computation.
"""

import json
import os
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd

SNAPSHOT_FILE   = "flow_snapshot.json"
GATE_HISTORY_FILE = "gate_history.json"
ET = ZoneInfo("America/New_York")

_CHAIN_COLS = [
    "side", "strike", "expiry", "dte",
    "bid", "ask", "mid", "last",
    "volume", "openInterest", "impliedVolatility",
]


# ── Snapshot I/O ──────────────────────────────────────────────────────────────

def load_snapshot() -> tuple[pd.DataFrame | None, str | None]:
    """
    Read flow_snapshot.json.

    Returns
    -------
    (DataFrame, iso_timestamp_str)  on success
    (None, None)                    if the file does not exist or is corrupt
    """
    if not os.path.exists(SNAPSHOT_FILE):
        return None, None
    try:
        with open(SNAPSHOT_FILE) as f:
            data = json.load(f)
        df = pd.DataFrame(data["rows"])
        return df, data["timestamp"]
    except Exception:
        return None, None


def save_snapshot(df: pd.DataFrame) -> None:
    """
    Overwrite flow_snapshot.json with *df* and the current wall-clock time.
    Only the canonical chain columns are persisted.
    """
    keep = [c for c in _CHAIN_COLS if c in df.columns]
    payload = {
        "timestamp": datetime.now().isoformat(),
        "rows":      df[keep].to_dict(orient="records"),
    }
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump(payload, f)


# ── Delta computation ─────────────────────────────────────────────────────────

def compute_deltas(
    current: pd.DataFrame,
    prev: pd.DataFrame,
    prev_ts: str,
) -> pd.DataFrame:
    """
    Enrich *current* with delta columns relative to the previous snapshot.

    Added columns
    -------------
    premium         float  — volume × mid × 100
    delta_volume    int | None
    delta_premium   float | None   (None when cross-day stale)
    is_new          bool   — contract absent from the previous snapshot
    is_stale_day    bool   — snapshot is from a prior calendar day
    is_block        bool   — delta_premium >= 1_000_000 (False when stale)

    Cross-day stale rule
    --------------------
    yfinance volume is *cumulative within a trading day*.  If the previous
    snapshot was saved on an earlier calendar day, delta_volume and
    delta_premium are meaningless (the counter reset overnight) and are set
    to None.  Rows are still shown; callers display them with a stale note.
    """
    current = current.copy()

    # Determine staleness
    try:
        prev_date = datetime.fromisoformat(prev_ts).date()
    except Exception:
        prev_date = date.min
    stale_day = prev_date < date.today()

    # Build lookup maps from the previous snapshot
    prev = prev.copy()
    prev["_key"] = _make_key(prev)
    prev_vol_map   = dict(zip(prev["_key"], prev["volume"].astype(float)))
    prev_prem_map  = dict(zip(
        prev["_key"],
        (prev["volume"].astype(float) * prev["mid"].astype(float) * 100),
    ))
    prev_keys = set(prev["_key"])

    current["_key"]    = _make_key(current)
    current["is_new"]  = (~current["_key"].isin(prev_keys)).map(bool)
    current["premium"] = current["volume"].astype(float) * current["mid"].astype(float) * 100

    if stale_day:
        current["delta_volume"]  = None
        current["delta_premium"] = None
        current["is_stale_day"]  = True
        current["is_block"]      = False
    else:
        current["delta_volume"] = current.apply(
            lambda r: int(r["volume"]) - int(prev_vol_map.get(r["_key"], 0)),
            axis=1,
        )
        current["delta_premium"] = current.apply(
            lambda r: r["premium"] - prev_prem_map.get(r["_key"], 0.0),
            axis=1,
        )
        current["is_stale_day"] = False
        current["is_block"] = (current["delta_premium"] >= 1_000_000).map(bool)

    current.drop(columns=["_key"], inplace=True)
    return current


def _make_key(df: pd.DataFrame) -> pd.Series:
    return df["side"].astype(str) + "_" + df["strike"].astype(str) + "_" + df["expiry"].astype(str)


# ── Gate history ──────────────────────────────────────────────────────────────

def load_gate_history() -> list[dict]:
    """Return the last ≤20 gate evaluations, newest first.  [] if missing."""
    if not os.path.exists(GATE_HISTORY_FILE):
        return []
    try:
        with open(GATE_HISTORY_FILE) as f:
            return json.load(f)
    except Exception:
        return []


def save_gate_history(entries: list[dict]) -> None:
    """Persist *entries*, keeping only the last 20."""
    with open(GATE_HISTORY_FILE, "w") as f:
        json.dump(entries[:20], f, indent=2)
