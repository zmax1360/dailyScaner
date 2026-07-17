"""
data_adapter.py — thin yfinance adapter for the Streamlit dashboard.

This is the ONLY file in the dashboard layer that imports yfinance.
It performs no market analysis — it normalises raw chain data into a
flat per-contract DataFrame and returns it. All analytical logic stays
in dailyScaner.py.
"""

import math
from datetime import date, datetime

import pandas as pd
import yfinance as yf


def _safe_int(v, default: int = 0) -> int:
    """Convert v to int, treating NaN/Inf/None as default."""
    try:
        f = float(v)
        return default if (math.isnan(f) or math.isinf(f)) else int(f)
    except (TypeError, ValueError):
        return default


def _safe_float(v, default: float = 0.0) -> float:
    """Convert v to float, treating NaN/None as default."""
    try:
        f = float(v)
        return default if math.isnan(f) else f
    except (TypeError, ValueError):
        return default


def fetch_full_chain(ticker: str = "AAPL") -> pd.DataFrame:
    """
    Fetch the full options chain across ALL expiries for *ticker*.

    Returns a DataFrame with one row per contract and these columns:

        side            "call" or "put"
        strike          float
        expiry          "YYYY-MM-DD"
        dte             int  (calendar days to expiry from today)
        bid             float
        ask             float
        mid             float  (bid+ask)/2; falls back to lastPrice if both zero
        last            float  (lastPrice)
        volume          int
        openInterest    int
        impliedVolatility float

    The DataFrame is never filtered or sorted here — callers decide what
    they want to keep.  Returns an empty DataFrame (correct columns) on
    any fetch failure so the caller can display a graceful error.
    """
    today = date.today()
    rows: list[dict] = []

    try:
        t = yf.Ticker(ticker)
        expiries = t.options
    except Exception:
        return _empty_frame()

    for expiry in expiries:
        dte = (datetime.strptime(expiry, "%Y-%m-%d").date() - today).days
        try:
            chain = t.option_chain(expiry)
        except Exception:
            continue

        for side, df in [("call", chain.calls), ("put", chain.puts)]:
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                bid  = _safe_float(row.get("bid"))
                ask  = _safe_float(row.get("ask"))
                last = _safe_float(row.get("lastPrice"))
                mid  = (bid + ask) / 2 if (bid > 0 or ask > 0) else last
                rows.append({
                    "side":              side,
                    "strike":            _safe_float(row.get("strike")),
                    "expiry":            expiry,
                    "dte":               dte,
                    "bid":               bid,
                    "ask":               ask,
                    "mid":               round(mid, 4),
                    "last":              last,
                    "volume":            _safe_int(row.get("volume")),
                    "openInterest":      _safe_int(row.get("openInterest")),
                    "impliedVolatility": _safe_float(row.get("impliedVolatility")),
                })

    if not rows:
        return _empty_frame()
    return pd.DataFrame(rows)


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "side", "strike", "expiry", "dte",
        "bid", "ask", "mid", "last",
        "volume", "openInterest", "impliedVolatility",
    ])
