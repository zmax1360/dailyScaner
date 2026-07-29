"""
data_adapter.py — thin yfinance adapter for the Streamlit dashboard.

This is the ONLY file in the dashboard layer that imports yfinance.
It performs no market analysis — it normalises raw chain data into a
flat per-contract DataFrame and returns it. All analytical logic stays
in dailyScaner.py.
"""

import math
from datetime import datetime

import pandas as pd
import yfinance as yf

from attribution import now_et


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
        delta             float | None  (Black-Scholes; None when IV/DTE unusable)

    The DataFrame is never filtered or sorted here — callers decide what
    they want to keep.  Returns an empty DataFrame (correct columns) on
    any fetch failure so the caller can display a graceful error.
    """
    today = now_et().date()
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
                    "delta":             None,  # filled below
                })

    if not rows:
        return _empty_frame()

    from greeks import bs_delta
    from config import SCORING

    # Spot for delta: prefer underlying last from first successful history, else mid heuristic
    spot = None
    try:
        hist = t.history(period="1d")
        if hist is not None and not hist.empty:
            spot = _safe_float(hist["Close"].iloc[-1])
    except Exception:
        spot = None
    r_free = float(SCORING.get("risk_free_rate", 0.045))
    for row in rows:
        if spot and spot > 0:
            d = bs_delta(
                row["side"], spot, row["strike"], row["dte"],
                row["impliedVolatility"], r=r_free,
            )
        else:
            d = None
        row["delta"] = d
    return pd.DataFrame(rows)


def fetch_daily_ohlc(ticker: str = "AAPL") -> dict | None:
    """
    Fetch today's daily Open / High / Low / Close for *ticker*.

    Returns:
        {"open": float, "high": float, "low": float, "close": float,
         "prev_close": float | None}
    or None on any failure. No analysis is performed here.
    """
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="5d", interval="1d")
        if hist is None or hist.empty:
            return None
        row = hist.iloc[-1]
        o = _safe_float(row.get("Open"))
        h = _safe_float(row.get("High"))
        l = _safe_float(row.get("Low"))
        c = _safe_float(row.get("Close"))
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            return None
        prev_close = None
        if len(hist) >= 2:
            prev_close = _safe_float(hist.iloc[-2].get("Close"))
            if prev_close <= 0:
                prev_close = None
        return {
            "open": o, "high": h, "low": l, "close": c,
            "prev_close": prev_close,
        }
    except Exception:
        return None


def fetch_macro_snapshot() -> dict | None:
    """
    Fetch SPY, QQQ, and ^VIX daily bars for macro gravity filtering.

    Returns a dict:
      {
        "SPY":  {"open","high","low","close","prev_close"},
        "QQQ":  {...},
        "VIX":  {"open","high","low","close","prev_close"},
      }
    or None if any required ticker fails. No analysis performed here.
    """
    out: dict = {}
    for key, symbol in [("SPY", "SPY"), ("QQQ", "QQQ"), ("VIX", "^VIX")]:
        bar = fetch_daily_ohlc(symbol)
        if not bar:
            return None
        out[key] = bar
    return out


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=[
        "side", "strike", "expiry", "dte",
        "bid", "ask", "mid", "last",
        "volume", "openInterest", "impliedVolatility", "delta",
    ])
