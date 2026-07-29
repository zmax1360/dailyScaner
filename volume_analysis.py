#!/usr/bin/env python3
"""
volume_analysis.py — Intraday buy / sell / neutral share-volume breakdown.

Primary feeds (via MarketDataSource) do not expose tick-level bid/ask prints, so we
approximate order flow from 1-minute bars using the Tick Rule:
  close > prev_close → Buy
  close < prev_close → Sell
  close == prev_close → Neutral

Average_Price is a volume-weighted average of bar closes (intraday VWAP proxy).
Total_Count is the number of 1m bars with volume > 0 (print-count proxy).
"""

from __future__ import annotations

from typing import Any

import pandas as pd


def _empty_result(ticker: str) -> dict[str, Any]:
    return {
        "ticker": (ticker or "").upper(),
        "Average_Price": 0.0,
        "Total_Count": 0,
        "Total_Volume": 0,
        "Buy_Volume": 0,
        "Sell_Volume": 0,
        "Neutral_Volume": 0,
        "source": "none",
    }


def _classify_tick_rule(df: pd.DataFrame) -> dict[str, Any]:
    """
    Apply tick rule on OHLC bars. Expects columns: Close, Volume
    (Open/High/Low optional — used for a richer VWAP typical price).
    """
    work = df.copy()
    work = work[work["Volume"].fillna(0) > 0]
    if work.empty:
        return _empty_result("")

    closes = work["Close"].astype(float)
    vols = work["Volume"].astype(float)
    prev = closes.shift(1)

    buy_mask = closes > prev
    sell_mask = closes < prev
    # First bar (no prev) + unchanged prints → neutral
    neutral_mask = ~(buy_mask | sell_mask)

    buy_vol = float(vols[buy_mask].sum())
    sell_vol = float(vols[sell_mask].sum())
    neut_vol = float(vols[neutral_mask].sum())
    total_vol = buy_vol + sell_vol + neut_vol

    # VWAP proxy: typical price * volume when H/L present, else close * volume
    if {"High", "Low"}.issubset(work.columns):
        typical = (
            work["High"].astype(float)
            + work["Low"].astype(float)
            + closes
        ) / 3.0
    else:
        typical = closes
    vwap_num = float((typical * vols).sum())
    avg_px = (vwap_num / total_vol) if total_vol > 0 else float(closes.iloc[-1])

    return {
        "Average_Price": round(avg_px, 4),
        "Total_Count": int(len(work)),
        "Total_Volume": int(round(total_vol)),
        "Buy_Volume": int(round(buy_vol)),
        "Sell_Volume": int(round(sell_vol)),
        "Neutral_Volume": int(round(neut_vol)),
    }


def _resolve_source(source=None):
    if source is not None:
        return source
    from config import SCORING
    from sources import get_source
    return get_source(str(SCORING.get("market_data_source", "yahoo")))


def get_stock_volume_analysis(ticker: str, *, source=None) -> dict[str, Any]:
    """
    Intraday volume analysis for *ticker*.

    Returns dict with Average_Price, Total_Count, Total_Volume,
    Buy_Volume, Sell_Volume, Neutral_Volume. Never raises — empty zeros
    on failure.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _empty_result("")

    src = _resolve_source(source)

    # Prefer fine bars; fall back to coarser intervals when 1m is empty / rate-limited.
    attempts: list[tuple[str, str]] = [
        ("1d", "1m"),
        ("5d", "5m"),
        ("5d", "15m"),
        ("10d", "60m"),
        ("1mo", "1h"),
    ]

    last_err: Exception | None = None
    for period, interval in attempts:
        try:
            hist = src.fetch_history(ticker, interval=interval, period=period)
            if hist is None or hist.empty:
                continue
            hist = hist.copy()
            # Keep only the latest ET session when we pulled multi-day bars
            try:
                idx = hist.index
                if getattr(idx, "tz", None) is not None:
                    days = idx.tz_convert("America/New_York").date
                else:
                    days = pd.DatetimeIndex(idx).tz_localize("UTC").tz_convert(
                        "America/New_York"
                    ).date
                hist["_day"] = list(days)
                last_day = hist["_day"].iloc[-1]
                hist = hist[hist["_day"] == last_day].drop(columns=["_day"])
            except Exception:
                pass
            if hist.empty or "Volume" not in hist.columns:
                continue
            classified = _classify_tick_rule(hist)
            if int(classified.get("Total_Volume") or 0) <= 0:
                continue
            classified["ticker"] = ticker
            classified["source"] = f"{src.name}_{interval}"
            return classified
        except Exception as exc:
            last_err = exc
            continue

    # Last resort: reuse the same 5m series the VWAP chart uses
    try:
        chart = fetch_intraday_vwap_df(ticker, timeframe="5M", source=src)
        if chart is not None and not chart.empty and "Volume" in chart.columns:
            classified = _classify_tick_rule(chart)
            if int(classified.get("Total_Volume") or 0) > 0:
                classified["ticker"] = ticker
                classified["source"] = "vwap_chart_5m_fallback"
                return classified
    except Exception as exc:
        last_err = exc

    out = _empty_result(ticker)
    if last_err is not None:
        out["error"] = f"{type(last_err).__name__}: {last_err}"
    return out


def _empty_vwap(ticker: str) -> dict[str, Any]:
    return {
        "ticker": (ticker or "").upper(),
        "VWAP": None,
        "VWAP_State": "UNKNOWN",
        "current_price": None,
        "prev_close": None,
        "prev_vwap": None,
    }


def compute_intraday_vwap(df: pd.DataFrame, *, session_reset: bool = True) -> pd.Series:
    """
    VWAP from OHLC bars: cumsum(typical * vol) / cumsum(vol).

    When session_reset=True (default), resets at each ET trading day —
    correct for intraday charts. When False, uses a continuous cumulative
    VWAP (better overlay for daily bars).
    """
    work = df.copy()
    if work.empty:
        return pd.Series(dtype=float)

    typical = (
        work["High"].astype(float)
        + work["Low"].astype(float)
        + work["Close"].astype(float)
    ) / 3.0
    vol = work["Volume"].astype(float).clip(lower=0)
    tp_vol = typical * vol

    if not session_reset:
        cum_vol = vol.cumsum().replace(0, float("nan"))
        return tp_vol.cumsum() / cum_vol

    # Day key for session reset
    idx = work.index
    try:
        if getattr(idx, "tz", None) is not None:
            days = idx.tz_convert("America/New_York").date
        else:
            days = pd.DatetimeIndex(idx).tz_localize("UTC").tz_convert(
                "America/New_York"
            ).date
    except Exception:
        days = pd.DatetimeIndex(idx).date

    work = work.copy()
    work["_day"] = list(days)
    cum_tp_vol = tp_vol.groupby(work["_day"]).cumsum()
    cum_vol = vol.groupby(work["_day"]).cumsum().replace(0, float("nan"))
    return cum_tp_vol / cum_vol


# Chart timeframe → history fetch + optional resample (mirrors dailyScaner)
_CHART_TF_SPEC: dict[str, dict[str, Any]] = {
    "5M":  {"interval": "5m", "period": "5d",  "resample": None,    "last_session": True,  "vwap_reset": True},
    "10M": {"interval": "5m", "period": "5d",  "resample": "10min", "last_session": True,  "vwap_reset": True},
    "45M": {"interval": "5m", "period": "5d",  "resample": "45min", "last_session": True,  "vwap_reset": True},
    "1H":  {"interval": "1h", "period": "30d", "resample": None,    "last_session": False, "vwap_reset": True},
    "4H":  {"interval": "1h", "period": "60d", "resample": "4h",    "last_session": False, "vwap_reset": True},
    "1D":  {"interval": "1d", "period": "6mo", "resample": None,    "last_session": False, "vwap_reset": False},
}

CHART_TIMEFRAMES = list(_CHART_TF_SPEC.keys())


def _ohlc_resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate OHLC bars to a coarser timeframe."""
    if df is None or df.empty:
        return pd.DataFrame()
    agg = df.resample(rule).agg({
        "Open": "first",
        "High": "max",
        "Low": "min",
        "Close": "last",
        "Volume": "sum",
    }).dropna(subset=["Open", "High", "Low", "Close"])
    return agg


def fetch_intraday_vwap_df(
    ticker: str,
    last_session_only: bool | None = None,
    timeframe: str = "5M",
    *,
    source=None,
) -> pd.DataFrame:
    """
    Fetch OHLC for *ticker* at *timeframe* and attach a VWAP column.

    Supported timeframes: 5M, 10M, 45M, 1H, 4H, 1D.
    10M/45M are resampled from 5m; 4H from 1h.

    Returns a DataFrame with Open/High/Low/Close/Volume/VWAP, or empty on failure.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return pd.DataFrame()

    tf = (timeframe or "5M").strip().upper()
    if tf not in _CHART_TF_SPEC:
        tf = "5M"
    spec = _CHART_TF_SPEC[tf]
    use_last_session = (
        spec["last_session"] if last_session_only is None else bool(last_session_only)
    )

    src = _resolve_source(source)

    try:
        hist = src.fetch_history(
            ticker, interval=spec["interval"], period=spec["period"],
        )
        if hist is None or hist.empty:
            return pd.DataFrame()

        hist = hist.copy()
        # Keep only OHLC + Volume
        need = ["Open", "High", "Low", "Close", "Volume"]
        for c in need:
            if c not in hist.columns:
                return pd.DataFrame()
        hist = hist[need]

        if spec["resample"]:
            hist = _ohlc_resample(hist, spec["resample"])
            if hist.empty:
                return pd.DataFrame()

        hist["VWAP"] = compute_intraday_vwap(
            hist, session_reset=bool(spec["vwap_reset"]),
        )

        if use_last_session and len(hist) > 0:
            idx = hist.index
            try:
                if getattr(idx, "tz", None) is not None:
                    days = idx.tz_convert("America/New_York").date
                else:
                    days = pd.DatetimeIndex(idx).tz_localize("UTC").tz_convert(
                        "America/New_York"
                    ).date
            except Exception:
                days = pd.DatetimeIndex(idx).date
            last_day = list(days)[-1]
            hist = hist[[d == last_day for d in days]]

        hist = hist.dropna(subset=["Open", "High", "Low", "Close", "VWAP"])
        return hist
    except Exception:
        return pd.DataFrame()


def get_intraday_vwap_state(ticker: str) -> dict[str, Any]:
    """
    5-minute intraday VWAP + reclaim state for *ticker*.

    VWAP_State:
      RECLAIMED UP   — prev close < prev VWAP, current > current VWAP
      RECLAIMED DOWN — prev close > prev VWAP, current < current VWAP
      TRENDING ABOVE / TRENDING BELOW — otherwise
      UNKNOWN — insufficient data
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _empty_vwap("")

    try:
        # Use full multi-day series for reclaim (needs prior bar); keep all days
        hist = fetch_intraday_vwap_df(ticker, last_session_only=False)
        if hist is None or len(hist) < 2:
            return _empty_vwap(ticker)

        closes = hist["Close"].astype(float)
        vwap = hist["VWAP"].astype(float)
        if len(closes) < 2:
            return _empty_vwap(ticker)

        cur_px = float(closes.iloc[-1])
        prev_px = float(closes.iloc[-2])
        cur_vwap = float(vwap.iloc[-1])
        prev_vwap = float(vwap.iloc[-2])

        prev_below = prev_px < prev_vwap
        prev_above = prev_px > prev_vwap
        cur_above = cur_px > cur_vwap
        cur_below = cur_px < cur_vwap

        if prev_below and cur_above:
            state = "RECLAIMED UP"
        elif prev_above and cur_below:
            state = "RECLAIMED DOWN"
        elif cur_above or (cur_px == cur_vwap and prev_above):
            state = "TRENDING ABOVE"
        else:
            state = "TRENDING BELOW"

        return {
            "ticker": ticker,
            "VWAP": round(cur_vwap, 4),
            "VWAP_State": state,
            "current_price": round(cur_px, 4),
            "prev_close": round(prev_px, 4),
            "prev_vwap": round(prev_vwap, 4),
        }
    except Exception:
        return _empty_vwap(ticker)


def render_vwap_chart(
    intraday_df: pd.DataFrame,
    ticker: str = "",
    timeframe: str = "5M",
) -> Any:
    """
    Build a Plotly candlestick + VWAP overlay figure.

    Expects columns: Open, High, Low, Close, VWAP (DatetimeIndex preferred).
    Returns a plotly.graph_objects.Figure (caller renders with st.plotly_chart).
    """
    import plotly.graph_objects as go

    ticker = (ticker or "").strip().upper() or "Ticker"
    tf = (timeframe or "5M").strip().upper()
    title = f"{ticker} {tf} Chart & Live VWAP"
    fig = go.Figure()

    if intraday_df is None or getattr(intraday_df, "empty", True):
        fig.update_layout(
            title=title,
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            xaxis_rangeslider_visible=False,
            margin=dict(l=10, r=10, t=30, b=10),
            height=380,
            hovermode="x unified",
        )
        return fig

    df = intraday_df.copy()
    x = df.index

    fig.add_trace(
        go.Candlestick(
            x=x,
            open=df["Open"],
            high=df["High"],
            low=df["Low"],
            close=df["Close"],
            name="Price",
            increasing_line_color="#00C853",
            increasing_fillcolor="#00C853",
            decreasing_line_color="#FF1744",
            decreasing_fillcolor="#FF1744",
            showlegend=False,
        )
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=df["VWAP"],
            mode="lines",
            name="VWAP",
            line=dict(color="#00E5FF", width=2),
            hovertemplate="VWAP %{y:.2f}<extra></extra>",
        )
    )
    fig.update_layout(
        title=dict(
            text=title,
            font=dict(size=15, color="#e0e0e0"),
            x=0.01,
            xanchor="left",
        ),
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        xaxis_rangeslider_visible=False,
        margin=dict(l=10, r=10, t=30, b=10),
        height=380,
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(color="#b0bec5"),
        ),
        xaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.06)"),
        yaxis=dict(
            showgrid=True,
            gridcolor="rgba(255,255,255,0.06)",
            tickprefix="$",
            side="right",
        ),
    )
    return fig
