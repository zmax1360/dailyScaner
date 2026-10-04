"""Price chart — candles with EMA 9/21/50 and VWAP, volume with participation, and
stochastic / ATR panels on one shared time axis."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from chart_indicators import add_indicators, last_session
from pov_leakage import OVER_PARTICIPATION_THRESH, compute_pov_metrics
from ui.context import ScanContext
from ui.widgets import _choice_control
from volume_analysis import CHART_TIMEFRAMES, fetch_intraday_vwap_df

TITLE = "Chart"
DEFAULT_TIMEFRAME = "15M"                      # the timeframe the trend rule reads
SESSION_TIMEFRAMES = ("5M", "10M", "15M", "45M")   # shown as the latest session only
INTRADAY_TIMEFRAMES = SESSION_TIMEFRAMES + ("1H", "4H")

UP, DOWN = "#00C853", "#FF1744"
EMA_COLORS = {"EMA9": "#7CFC00", "EMA21": "#2196F3", "EMA50": "#F44336"}
VWAP_COLOR = "#00E5FF"
GRID = "rgba(255,255,255,0.06)"


def prepare(bars: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Indicators on the full history, then the window to display."""
    full = add_indicators(bars)
    if full.empty:
        return full
    if "Volume" in full.columns:
        pov = compute_pov_metrics(full)
        if not pov.empty:
            full["Participation"] = pov["Participation_Spike_Ratio"]
    if timeframe in SESSION_TIMEFRAMES:
        return last_session(full)
    return full


def build_figure(df: pd.DataFrame, *, ticker: str = "", timeframe: str = DEFAULT_TIMEFRAME,
                 show_stoch: bool = True, show_atr: bool = False,
                 show_participation: bool = True):
    """One Plotly figure: price / volume / (stochastic) / (ATR), sharing the x axis."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    panels = ["price", "volume"] + (["stoch"] if show_stoch else []) + (["atr"] if show_atr else [])
    heights = {"price": 0.58, "volume": 0.16, "stoch": 0.15, "atr": 0.11}
    total = sum(heights[p] for p in panels)
    fig = make_subplots(
        rows=len(panels), cols=1, shared_xaxes=True, vertical_spacing=0.025,
        row_heights=[heights[p] / total for p in panels],
        specs=[[{"secondary_y": p == "volume"}] for p in panels],
    )
    title = f"{(ticker or '').upper()} {timeframe}".strip()
    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color="#e0e0e0"), x=0.01, xanchor="left"),
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=34, b=10), height=300 + 130 * len(panels),
        hovermode="x unified", xaxis_rangeslider_visible=False, bargap=0.15,
        legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1,
                    font=dict(color="#b0bec5")),
    )
    if df is None or getattr(df, "empty", True):
        return fig

    x = df.index
    row = {p: i + 1 for i, p in enumerate(panels)}

    fig.add_trace(go.Candlestick(
        x=x, open=df["Open"], high=df["High"], low=df["Low"], close=df["Close"], name="Price",
        increasing_line_color=UP, increasing_fillcolor=UP,
        decreasing_line_color=DOWN, decreasing_fillcolor=DOWN, showlegend=False,
    ), row=row["price"], col=1)
    for name, color in EMA_COLORS.items():
        if name in df.columns and df[name].notna().any():
            fig.add_trace(go.Scatter(
                x=x, y=df[name], mode="lines", name=name.replace("EMA", "EMA "),
                line=dict(color=color, width=1.4),
                hovertemplate=f"{name.replace('EMA', 'EMA ')} %{{y:.2f}}<extra></extra>",
            ), row=row["price"], col=1)
    if "VWAP" in df.columns and df["VWAP"].notna().any():
        fig.add_trace(go.Scatter(
            x=x, y=df["VWAP"], mode="lines", name="VWAP",
            line=dict(color=VWAP_COLOR, width=1.6, dash="dot"),
            hovertemplate="VWAP %{y:.2f}<extra></extra>",
        ), row=row["price"], col=1)

    if "Volume" in df.columns:
        colors = [UP if c >= o else DOWN for o, c in zip(df["Open"], df["Close"])]
        fig.add_trace(go.Bar(
            x=x, y=df["Volume"], name="Volume", marker_color=colors, opacity=0.55,
            showlegend=False, hovertemplate="Vol %{y:,.0f}<extra></extra>",
        ), row=row["volume"], col=1, secondary_y=False)
        if show_participation and "Participation" in df.columns \
                and df["Participation"].notna().any():
            ratio = df["Participation"]
            fig.add_trace(go.Scatter(
                x=x, y=ratio, mode="lines", name="Participation ×", showlegend=False,
                line=dict(color="#B0BEC5", width=1.3),
                hovertemplate="Participation %{y:.2f}×<extra></extra>",
            ), row=row["volume"], col=1, secondary_y=True)
            hot = ratio.where(ratio >= OVER_PARTICIPATION_THRESH)
            if hot.notna().any():
                fig.add_trace(go.Scatter(
                    x=x, y=hot, mode="markers", name=f"≥ {OVER_PARTICIPATION_THRESH:g}×", showlegend=False,
                    marker=dict(color="#FF00FF", size=7),
                    hovertemplate="Over-participation %{y:.2f}×<extra></extra>",
                ), row=row["volume"], col=1, secondary_y=True)
            # The ratio's own scale is not labelled (it collided with the volume ticks);
            # hover shows the value and magenta dots mark the bars at or above the threshold.
            fig.update_yaxes(showgrid=False, rangemode="tozero", showticklabels=False,
                             row=row["volume"], col=1, secondary_y=True)

    if show_stoch and {"StochK", "StochD"} <= set(df.columns):
        fig.add_trace(go.Scatter(x=x, y=df["StochK"], mode="lines", name="Stoch %K", showlegend=False,
                                 line=dict(color="#42A5F5", width=1.3),
                                 hovertemplate="%K %{y:.1f}<extra></extra>"),
                      row=row["stoch"], col=1)
        fig.add_trace(go.Scatter(x=x, y=df["StochD"], mode="lines", name="Stoch %D", showlegend=False,
                                 line=dict(color="#EC407A", width=1.3),
                                 hovertemplate="%D %{y:.1f}<extra></extra>"),
                      row=row["stoch"], col=1)
        for level in (20, 80):
            fig.add_hline(y=level, line=dict(color="rgba(255,255,255,0.25)", width=1, dash="dash"),
                          row=row["stoch"], col=1)
        fig.update_yaxes(range=[0, 100], tickvals=[20, 50, 80], row=row["stoch"], col=1)

    if show_atr and "ATR" in df.columns:
        fig.add_trace(go.Scatter(x=x, y=df["ATR"], mode="lines", name="ATR 14", showlegend=False,
                                 line=dict(color="#FFA726", width=1.3),
                                 hovertemplate="ATR %{y:.2f}<extra></extra>"),
                      row=row["atr"], col=1)

    fig.update_xaxes(showgrid=True, gridcolor=GRID)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, side="right")
    fig.update_yaxes(tickprefix="$", row=row["price"], col=1)
    if timeframe not in SESSION_TIMEFRAMES:           # hide the gaps between sessions
        breaks = [dict(bounds=["sat", "mon"])]
        if timeframe in INTRADAY_TIMEFRAMES:
            breaks.append(dict(bounds=[16, 9.5], pattern="hour"))
        fig.update_xaxes(rangebreaks=breaks)
    return fig


@st.cache_data(ttl=60)
def _cached_bars(ticker: str, timeframe: str) -> pd.DataFrame:
    """OHLCV + VWAP with history (not just the last session) so the EMAs are warmed up."""
    return fetch_intraday_vwap_df(ticker, last_session_only=False, timeframe=timeframe)


def render(ctx: ScanContext) -> None:
    ticker = ctx.ticker
    left, right = st.columns([5, 1])
    with left:
        timeframe = _choice_control(
            "Chart timeframe", list(CHART_TIMEFRAMES), default=DEFAULT_TIMEFRAME,
            key=f"chart_tf_{ticker}",
            help="10M/15M/45M are built from 5-minute bars; 4H from 1-hour bars.",
        )
    with right.popover("Indicators", use_container_width=True):
        show_stoch = st.checkbox("Stochastic (14, 3)", value=True, key="chart_stoch")
        show_atr = st.checkbox("ATR (14)", value=False, key="chart_atr")
        show_part = st.checkbox("Participation", value=True, key="chart_participation")

    df = prepare(_cached_bars(ticker, timeframe), timeframe)
    if df.empty:
        st.caption(f"{timeframe} chart unavailable right now.")
        return
    fig = build_figure(df, ticker=ticker, timeframe=timeframe, show_stoch=show_stoch,
                       show_atr=show_atr, show_participation=show_part)
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    last = df.iloc[-1]
    parts = [f"{name.replace('EMA', 'EMA ')} {last[name]:.2f}"
             for name in EMA_COLORS if pd.notna(last.get(name))]
    if pd.notna(last.get("VWAP")):
        parts.append(f"VWAP {last['VWAP']:.2f}")
    if pd.notna(last.get("StochK")) and pd.notna(last.get("StochD")):
        parts.append(f"Stoch {last['StochK']:.0f} / {last['StochD']:.0f}")
    if pd.notna(last.get("ATR")):
        parts.append(f"ATR {last['ATR']:.2f}")
    missing = [n.replace("EMA", "EMA ") for n in EMA_COLORS if pd.isna(last.get(n))]
    note = f" · not enough bars for {', '.join(missing)}" if missing else ""
    st.caption("Last bar: " + " · ".join(parts) + note)
