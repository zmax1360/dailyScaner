#!/usr/bin/env python3
"""
pov_leakage.py — Institutional Signal Leakage (Percent of Volume) detector.

Rolling 15-bar participation spike on 5-minute volume:
  Participation_Spike_Ratio = Volume / Avg_Vol_15
  Over-participation when ratio ≥ 3.0×
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

OVER_PARTICIPATION_THRESH = 3.0
ROLLING_WINDOW = 15
URGENCY_TAG = "🚨 INSTITUTIONAL URGENCY DETECTED"
URGENCY_CALL_MULT = 1.25

COLOR_HIDDEN = "#00C853"       # emerald — target / hidden participation
COLOR_LEAKAGE = "#FF00FF"      # magenta — over-participation leak
COLOR_VOLUME = "rgba(158,158,158,0.35)"


def compute_pov_metrics(
    df: pd.DataFrame,
    *,
    window: int = ROLLING_WINDOW,
) -> pd.DataFrame:
    """
    Attach Avg_Vol_15 and Participation_Spike_Ratio to an OHLC+Volume frame.
    Expects a Volume column (5-minute bars preferred).
    """
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    if "Volume" not in df.columns:
        return pd.DataFrame()

    out = df.copy()
    vol = pd.to_numeric(out["Volume"], errors="coerce").fillna(0.0).clip(lower=0)
    # Rolling mean of prior bars only would be shift(1); include current for
    # stability on short sessions (matches POV-style participation charts).
    out["Avg_Vol_15"] = vol.rolling(window=window, min_periods=max(3, window // 3)).mean()
    avg = out["Avg_Vol_15"].replace(0, np.nan)
    out["Participation_Spike_Ratio"] = (vol / avg).replace([np.inf, -np.inf], np.nan)
    out["Over_Participation"] = out["Participation_Spike_Ratio"] >= OVER_PARTICIPATION_THRESH
    return out


def detect_pov_urgency(df: pd.DataFrame) -> dict[str, Any]:
    """
    Inspect the last completed 5m bar for over-participation above VWAP.

    Returns urgency flag + last-bar metrics for UI / Best Value boosts.
    """
    empty = {
        "urgency": False,
        "ratio": None,
        "above_vwap": False,
        "last_close": None,
        "last_vwap": None,
        "last_volume": None,
        "tag": "",
    }
    if df is None or getattr(df, "empty", True):
        return empty
    work = df
    if "Participation_Spike_Ratio" not in work.columns:
        work = compute_pov_metrics(work)
    if work.empty or "Participation_Spike_Ratio" not in work.columns:
        return empty

    # Prefer prior bar as "completed"; fall back to last if only one row
    idx = -2 if len(work) >= 2 else -1
    row = work.iloc[idx]
    try:
        ratio = float(row["Participation_Spike_Ratio"])
    except (TypeError, ValueError):
        return empty
    if not np.isfinite(ratio):
        return empty

    close = None
    vwap = None
    try:
        close = float(row["Close"]) if "Close" in work.columns else None
    except (TypeError, ValueError):
        close = None
    try:
        vwap = float(row["VWAP"]) if "VWAP" in work.columns else None
    except (TypeError, ValueError):
        vwap = None

    above_vwap = bool(
        close is not None and vwap is not None and close > vwap
    )
    over = ratio >= OVER_PARTICIPATION_THRESH
    urgency = over and above_vwap

    try:
        last_vol = float(row["Volume"])
    except (TypeError, ValueError):
        last_vol = None

    return {
        "urgency": urgency,
        "ratio": round(ratio, 3),
        "above_vwap": above_vwap,
        "last_close": round(close, 4) if close is not None else None,
        "last_vwap": round(vwap, 4) if vwap is not None else None,
        "last_volume": int(round(last_vol)) if last_vol is not None else None,
        "over_participation": over,
        "tag": URGENCY_TAG if urgency else "",
    }


def fetch_pov_leakage(
    ticker: str,
    *,
    last_session_only: bool = True,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Fetch 5m OHLC+VWAP, compute POV metrics, return (df, urgency_info).
    yfinance lives in volume_analysis — kept out of app.py.
    """
    from volume_analysis import fetch_intraday_vwap_df

    ticker = (ticker or "").strip().upper()
    raw = fetch_intraday_vwap_df(
        ticker, last_session_only=last_session_only, timeframe="5M",
    )
    if raw is None or raw.empty:
        return pd.DataFrame(), detect_pov_urgency(pd.DataFrame())
    pov = compute_pov_metrics(raw)
    return pov, detect_pov_urgency(pov)


def render_pov_leakage_chart(df: pd.DataFrame, ticker: str = "") -> Any:
    """
    Dual-axis POV chart:
      Left  — 5m volume (grey filled area)
      Right — Participation_Spike_Ratio (emerald < 3×, magenta ≥ 3×)
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    ticker = (ticker or "").strip().upper()
    title = f"{ticker} Institutional POV Leakage" if ticker else "Institutional POV Leakage"

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    if df is None or getattr(df, "empty", True):
        fig.update_layout(
            title=title,
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=300,
            margin=dict(l=10, r=10, t=36, b=10),
        )
        return fig

    work = df if "Participation_Spike_Ratio" in df.columns else compute_pov_metrics(df)
    if work.empty:
        return render_pov_leakage_chart(pd.DataFrame(), ticker=ticker)

    x = work.index
    vol = pd.to_numeric(work["Volume"], errors="coerce").fillna(0)
    ratio = pd.to_numeric(work["Participation_Spike_Ratio"], errors="coerce")

    # Primary: volume pool
    fig.add_trace(
        go.Scatter(
            x=x,
            y=vol,
            name="5m Volume",
            fill="tozeroy",
            mode="lines",
            line=dict(width=0.5, color="rgba(158,158,158,0.5)"),
            fillcolor=COLOR_VOLUME,
            hovertemplate="Vol %{y:,.0f}<extra>5m Volume</extra>",
        ),
        secondary_y=False,
    )

    # Secondary: split ratio into hidden vs leakage (NaN gaps)
    green = ratio.where(ratio < OVER_PARTICIPATION_THRESH)
    magenta = ratio.where(ratio >= OVER_PARTICIPATION_THRESH)

    fig.add_trace(
        go.Scatter(
            x=x,
            y=green,
            name="Target Participation (Hidden)",
            mode="lines",
            line=dict(color=COLOR_HIDDEN, width=2.2),
            connectgaps=False,
            hovertemplate="POV %{y:.2f}×<extra>Hidden</extra>",
        ),
        secondary_y=True,
    )
    fig.add_trace(
        go.Scatter(
            x=x,
            y=magenta,
            name="Over-Participation (Signal Leakage)",
            mode="lines",
            line=dict(color=COLOR_LEAKAGE, width=2.5),
            connectgaps=False,
            hovertemplate="POV %{y:.2f}×<extra>Leakage</extra>",
        ),
        secondary_y=True,
    )

    # Baseline 1.0× on secondary axis
    fig.add_hline(
        y=1.0,
        line_dash="dash",
        line_color=COLOR_HIDDEN,
        line_width=1.2,
        opacity=0.85,
        secondary_y=True,
        annotation_text="1.0× baseline",
        annotation_position="bottom right",
        annotation_font_color=COLOR_HIDDEN,
        annotation_font_size=11,
    )
    # Threshold reference
    fig.add_hline(
        y=OVER_PARTICIPATION_THRESH,
        line_dash="dot",
        line_color=COLOR_LEAKAGE,
        line_width=1,
        opacity=0.5,
        secondary_y=True,
    )

    fig.update_layout(
        title=dict(
            text=title,
            font=dict(size=14, color="#e0e0e0"),
            x=0.01,
            xanchor="left",
        ),
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=320,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=11, color="#b0bec5"),
        ),
        hovermode="x unified",
    )
    fig.update_yaxes(
        title_text="Volume",
        secondary_y=False,
        showgrid=True,
        gridcolor="rgba(255,255,255,0.05)",
    )
    fig.update_yaxes(
        title_text="Participation Spike (×)",
        secondary_y=True,
        showgrid=False,
        rangemode="tozero",
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(255,255,255,0.05)")
    return fig
