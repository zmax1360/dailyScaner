"""
data_adapter.py — thin market-data adapter for the Streamlit dashboard.

This is the ONLY file in the dashboard layer that talks to a MarketDataSource.
It performs no market analysis — it normalises chain/OHLC data into the shapes
the UI expects. All analytical logic stays in dailyScaner.py.
"""

from __future__ import annotations

import math

import pandas as pd

from sources.base import MarketDataSource


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


def _resolve_source(source: MarketDataSource | None) -> MarketDataSource:
    if source is not None:
        return source
    from config import SCORING
    from sources import get_source
    return get_source(str(SCORING.get("market_data_source", "yahoo")))


def fetch_full_chain(
    ticker: str = "AAPL",
    *,
    source: MarketDataSource | None = None,
) -> pd.DataFrame:
    """
    Fetch the full options chain across expiries for *ticker*.

    Returns a DataFrame with one row per contract and these columns:

        side            "call" or "put"
        strike          float
        expiry          "YYYY-MM-DD"
        dte             int
        bid             float
        ask             float
        mid             float  (bid+ask)/2; falls back to last if both zero
        last            float
        volume          int
        openInterest    int
        impliedVolatility float
        delta             float | None

    Returns an empty DataFrame (correct columns) on any fetch failure.
    """
    src = _resolve_source(source)
    try:
        chain = src.fetch_chain(ticker, max_dte=3650)
    except Exception:
        return _empty_frame()
    if chain is None or chain.empty:
        return _empty_frame()

    rows: list[dict] = []
    for _, r in chain.iterrows():
        bid = _safe_float(r.get("bid"))
        ask = _safe_float(r.get("ask"))
        last = _safe_float(r.get("last"))
        mid = (bid + ask) / 2 if (bid > 0 or ask > 0) else last
        rows.append({
            "side": str(r.get("side", "")).lower(),
            "strike": _safe_float(r.get("strike")),
            "expiry": str(r.get("expiry") or "")[:10],
            "dte": _safe_int(r.get("dte")),
            "bid": bid,
            "ask": ask,
            "mid": round(mid, 4),
            "last": last,
            "volume": _safe_int(r.get("volume")),
            "openInterest": _safe_int(r.get("openInterest")),
            "impliedVolatility": _safe_float(r.get("iv")),
            "delta": None,  # filled below via BS when possible
        })
    if not rows:
        return _empty_frame()

    from greeks import bs_delta, effective_dte_days
    from config import SCORING
    from attribution import now_et

    spot = src.fetch_spot(ticker)
    r_free = float(SCORING.get("risk_free_rate", 0.045))
    asof = now_et()
    for row in rows:
        if spot and spot > 0:
            t_days = effective_dte_days(
                row["dte"], expiry=row["expiry"], now_et=asof,
            )
            d = bs_delta(
                row["side"], spot, row["strike"], t_days,
                row["impliedVolatility"], r=r_free,
            )
        else:
            d = None
        row["delta"] = d
    return pd.DataFrame(rows)


def fetch_daily_ohlc(
    ticker: str = "AAPL",
    *,
    source: MarketDataSource | None = None,
) -> dict | None:
    """
    Fetch today's daily Open / High / Low / Close for *ticker*.

    Returns:
        {"open": float, "high": float, "low": float, "close": float,
         "prev_close": float | None}
    or None on any failure. No analysis is performed here.
    """
    src = _resolve_source(source)
    try:
        hist = src.fetch_history(ticker, interval="1d", period="5d")
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


def fetch_macro_snapshot(
    *,
    source: MarketDataSource | None = None,
) -> dict | None:
    """
    Fetch SPY, QQQ, and ^VIX daily bars for macro gravity filtering.

    Returns a dict keyed by SPY/QQQ/VIX, or None if any required ticker fails.
    """
    src = _resolve_source(source)
    out: dict = {}
    for key, symbol in [("SPY", "SPY"), ("QQQ", "QQQ"), ("VIX", "^VIX")]:
        bar = fetch_daily_ohlc(symbol, source=src)
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
