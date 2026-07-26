"""
best_value_archive.py — persist Best Value top-contract hits across refreshes.

Appends each scanner refresh's top contracts to:
  - st.session_state['best_value_archive']
  - data/best_value_archive.csv
"""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

ET = ZoneInfo("America/New_York")

ARCHIVE_COLS = [
    "Run_Timestamp",
    "Ticker",
    "Side",
    "Strike",
    "Expiry",
    "Price",
    "Value_Score",
    "Velocity",
    "Signal",
]

CSV_PATH = os.path.join("data", "best_value_archive.csv")
_SESSION_KEY = "best_value_archive"
_LAST_KEY = "_bv_archive_last_log_key"


def _empty_df() -> pd.DataFrame:
    return pd.DataFrame(columns=ARCHIVE_COLS)


def _ensure_data_dir() -> None:
    os.makedirs(os.path.dirname(CSV_PATH) or "data", exist_ok=True)


def load_archive_from_disk() -> pd.DataFrame:
    """Read CSV if present; return empty frame otherwise."""
    if not os.path.isfile(CSV_PATH):
        return _empty_df()
    try:
        df = pd.read_csv(CSV_PATH)
    except Exception:
        return _empty_df()
    if df.empty:
        return _empty_df()
    for col in ARCHIVE_COLS:
        if col not in df.columns:
            df[col] = pd.NA
    return df[ARCHIVE_COLS].copy()


def ensure_archive_loaded() -> pd.DataFrame:
    """Hydrate session_state from disk once per session."""
    if _SESSION_KEY not in st.session_state:
        st.session_state[_SESSION_KEY] = load_archive_from_disk()
    df = st.session_state[_SESSION_KEY]
    if not isinstance(df, pd.DataFrame):
        st.session_state[_SESSION_KEY] = _empty_df()
    return st.session_state[_SESSION_KEY]


def _save_archive_to_disk(df: pd.DataFrame) -> None:
    _ensure_data_dir()
    df[ARCHIVE_COLS].to_csv(CSV_PATH, index=False)


def _normalize_run_timestamp(run_timestamp: str | None) -> str:
    if run_timestamp:
        s = str(run_timestamp).strip()
        if s:
            # Normalize ISO → "YYYY-MM-DD HH:MM:SS ET"
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ET)
                return dt.astimezone(ET).strftime("%Y-%m-%d %H:%M:%S ET")
            except Exception:
                if not s.endswith("ET"):
                    return f"{s} ET"
                return s
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")


def _today_et_str() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d")


def _row_date(run_ts: str) -> str:
    """Extract YYYY-MM-DD from a Run_Timestamp cell."""
    s = str(run_ts or "")
    return s[:10] if len(s) >= 10 else ""


def filter_today(df: pd.DataFrame) -> pd.DataFrame:
    """Rows whose Run_Timestamp falls on today's ET calendar date."""
    if df is None or df.empty:
        return _empty_df()
    today = _today_et_str()
    mask = df["Run_Timestamp"].map(_row_date) == today
    return df.loc[mask].copy()


def add_times_flagged(df: pd.DataFrame) -> pd.DataFrame:
    """
    Count how many times each unique contract appears in the given frame
    (typically today's archive). Adds Times_Flagged column.
    """
    if df is None or df.empty:
        out = _empty_df()
        out["Times_Flagged"] = pd.Series(dtype=int)
        return out

    out = df.copy()
    keys = ["Ticker", "Side", "Strike", "Expiry"]
    for k in keys:
        if k not in out.columns:
            out[k] = ""
    # Normalize key columns for stable grouping
    out["_tk"] = out["Ticker"].astype(str).str.upper().str.strip()
    out["_sd"] = out["Side"].astype(str).str.upper().str.strip()
    out["_sk"] = pd.to_numeric(out["Strike"], errors="coerce").round(2)
    out["_ex"] = out["Expiry"].astype(str).str.strip()

    counts = (
        out.groupby(["_tk", "_sd", "_sk", "_ex"], dropna=False)
        .size()
        .rename("Times_Flagged")
        .reset_index()
    )
    out = out.merge(counts, on=["_tk", "_sd", "_sk", "_ex"], how="left")
    out = out.drop(columns=["_tk", "_sd", "_sk", "_ex"])
    out["Times_Flagged"] = out["Times_Flagged"].fillna(0).astype(int)
    return out


def most_persistent_today(df_today: pd.DataFrame) -> tuple[str, int] | None:
    """
    Return (label, times_flagged) for the contract flagged most today.
    label e.g. "AAPL CALL $210.0 2026-07-25"
    """
    if df_today is None or df_today.empty:
        return None
    flagged = add_times_flagged(df_today)
    # One row per contract with its count
    uniq = (
        flagged.drop_duplicates(subset=["Ticker", "Side", "Strike", "Expiry"])
        .sort_values("Times_Flagged", ascending=False)
    )
    if uniq.empty:
        return None
    top = uniq.iloc[0]
    n = int(top["Times_Flagged"])
    if n <= 0:
        return None
    try:
        strike = float(top["Strike"])
        strike_s = f"${strike:.1f}"
    except Exception:
        strike_s = str(top["Strike"])
    label = (
        f"{str(top['Ticker']).upper()} {str(top['Side']).upper()} "
        f"{strike_s} {top['Expiry']}"
    )
    return label, n


def log_best_value_run(
    top_contracts_df: pd.DataFrame,
    *,
    ticker: str,
    run_timestamp: str | None = None,
) -> bool:
    """
    Append top Best Value contracts for this scanner refresh.

    Dedupes on (Ticker, Run_Timestamp) so Streamlit widget reruns do not
    inflate Times_Flagged. Returns True if rows were written.
    """
    ensure_archive_loaded()
    if top_contracts_df is None or getattr(top_contracts_df, "empty", True):
        return False

    ticker_u = (ticker or "").strip().upper() or "UNKNOWN"
    run_ts = _normalize_run_timestamp(run_timestamp)
    dedupe_key = f"{ticker_u}|{run_ts}"

    if st.session_state.get(_LAST_KEY) == dedupe_key:
        return False

    arch = st.session_state[_SESSION_KEY]
    if isinstance(arch, pd.DataFrame) and not arch.empty:
        already = (
            (arch["Run_Timestamp"].astype(str) == run_ts)
            & (arch["Ticker"].astype(str).str.upper() == ticker_u)
        )
        if already.any():
            st.session_state[_LAST_KEY] = dedupe_key
            return False

    # Accept either raw engine columns or display-renamed columns
    colmap = {
        "Side": ("Side", "side"),
        "Strike": ("Strike", "strike"),
        "Expiry": ("Expiry", "expiry"),
        "Price": ("Price", "last", "lastPrice"),
        "Value_Score": ("Value_Score",),
        "Velocity": ("Velocity", "Score_Velocity"),
        "Signal": ("Signal", "Action_Signal"),
    }

    def _pick(row, names):
        for n in names:
            if n in row.index and pd.notna(row[n]):
                return row[n]
        return None

    rows = []
    for _, row in top_contracts_df.iterrows():
        side = _pick(row, colmap["Side"])
        strike = _pick(row, colmap["Strike"])
        expiry = _pick(row, colmap["Expiry"])
        price = _pick(row, colmap["Price"])
        score = _pick(row, colmap["Value_Score"])
        vel = _pick(row, colmap["Velocity"])
        sig = _pick(row, colmap["Signal"])
        if side is None or strike is None or expiry is None:
            continue
        try:
            strike_f = float(strike)
        except Exception:
            continue
        try:
            price_f = float(price) if price is not None else float("nan")
        except Exception:
            price_f = float("nan")
        try:
            score_f = float(score) if score is not None else float("nan")
        except Exception:
            score_f = float("nan")
        try:
            vel_f = float(vel) if vel is not None else 0.0
        except Exception:
            vel_f = 0.0

        rows.append({
            "Run_Timestamp": run_ts,
            "Ticker": ticker_u,
            "Side": str(side).upper(),
            "Strike": strike_f,
            "Expiry": str(expiry),
            "Price": price_f,
            "Value_Score": score_f,
            "Velocity": vel_f,
            "Signal": "" if sig is None else str(sig),
        })

    if not rows:
        return False

    new_df = pd.DataFrame(rows, columns=ARCHIVE_COLS)
    combined = pd.concat([arch, new_df], ignore_index=True)
    st.session_state[_SESSION_KEY] = combined
    st.session_state[_LAST_KEY] = dedupe_key
    _save_archive_to_disk(combined)
    return True


def clear_todays_log() -> int:
    """Remove today's ET rows from session + CSV. Returns number removed."""
    ensure_archive_loaded()
    arch = st.session_state[_SESSION_KEY]
    if arch is None or arch.empty:
        return 0
    today = _today_et_str()
    keep_mask = arch["Run_Timestamp"].map(_row_date) != today
    removed = int((~keep_mask).sum())
    kept = arch.loc[keep_mask].copy()
    st.session_state[_SESSION_KEY] = kept if not kept.empty else _empty_df()
    st.session_state.pop(_LAST_KEY, None)
    _save_archive_to_disk(st.session_state[_SESSION_KEY])
    return removed


def archive_csv_bytes() -> bytes:
    """Full archive CSV for download."""
    ensure_archive_loaded()
    df = st.session_state[_SESSION_KEY]
    if df is None or df.empty:
        return _empty_df().to_csv(index=False).encode("utf-8")
    return df[ARCHIVE_COLS].to_csv(index=False).encode("utf-8")
