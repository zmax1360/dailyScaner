#!/usr/bin/env python3
"""
cost_distribution.py — Macro Cost Distribution & Overhead Supply (volume profile).

Broker-style Position Cost Distribution from ~6 months of daily OHLCV:
  • Bin volume by each day's Typical Price (H+L+C)/3
  • POC = highest-volume bin
  • Profited_Shares_Pct = % of volume sitting below spot (already in profit)
  • Cost_Range_70 / Cost_Range_90 = value-area style bounds around the POC
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _empty_result(ticker: str = "") -> dict[str, Any]:
    return {
        "ticker": (ticker or "").upper(),
        "spot": None,
        "price_bins": [],
        "volume_bins": [],
        "Average_Cost_POC": None,
        "Profited_Shares_Pct": None,
        "Cost_Range_90": (None, None),
        "Cost_Range_70": (None, None),
        "total_volume": 0,
        "days": 0,
    }


def _value_area_bounds(
    price_mids: np.ndarray,
    volumes: np.ndarray,
    poc_idx: int,
    target_pct: float,
) -> tuple[float, float]:
    """
    Expand from the POC bin (preferring the richer adjacent side each step)
    until cumulative volume >= target_pct of total. Returns (low, high) prices.
    """
    total = float(volumes.sum())
    if total <= 0 or len(volumes) == 0:
        return float(price_mids[poc_idx]), float(price_mids[poc_idx])

    target = total * float(target_pct)
    lo = hi = int(poc_idx)
    cum = float(volumes[poc_idx])

    while cum < target and (lo > 0 or hi < len(volumes) - 1):
        vol_below = float(volumes[lo - 1]) if lo > 0 else -1.0
        vol_above = float(volumes[hi + 1]) if hi < len(volumes) - 1 else -1.0
        if vol_above >= vol_below and hi < len(volumes) - 1:
            hi += 1
            cum += float(volumes[hi])
        elif lo > 0:
            lo -= 1
            cum += float(volumes[lo])
        elif hi < len(volumes) - 1:
            hi += 1
            cum += float(volumes[hi])
        else:
            break

    # Use bin edges via half-step between neighbors for a slightly wider bound
    half = (price_mids[1] - price_mids[0]) / 2.0 if len(price_mids) > 1 else 0.0
    low = float(price_mids[lo] - half)
    high = float(price_mids[hi] + half)
    return low, high


def calculate_cost_distribution(
    ticker: str,
    days: int = 180,
    n_bins: int = 50,
    spot: float | None = None,
    *,
    source=None,
) -> dict[str, Any]:
    """
    Fetch ~`days` of daily bars and build a volume-by-cost histogram.

    Returns dict with price_bins, volume_bins, Average_Cost_POC,
    Profited_Shares_Pct, Cost_Range_90, Cost_Range_70.
    Never raises — empty zeros on failure.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _empty_result("")

    try:
        if source is None:
            from config import SCORING
            from sources import get_source
            source = get_source(str(SCORING.get("market_data_source", "yahoo")))

        # Prefer period string; fall back to start/end if needed
        period = "6mo" if days >= 150 else f"{max(int(days), 30)}d"
        hist = source.fetch_history(ticker, interval="1d", period=period)
        if hist is None or hist.empty:
            return _empty_result(ticker)

        hist = hist.dropna(subset=["High", "Low", "Close", "Volume"]).copy()
        if hist.empty:
            return _empty_result(ticker)

        # Trim to last `days` calendar rows if we got more
        if days and len(hist) > days:
            hist = hist.iloc[-int(days):]

        typical = (
            hist["High"].astype(float)
            + hist["Low"].astype(float)
            + hist["Close"].astype(float)
        ) / 3.0
        vol = hist["Volume"].astype(float).clip(lower=0)

        px_low = float(typical.min())
        px_high = float(typical.max())
        if not np.isfinite(px_low) or not np.isfinite(px_high) or px_high <= px_low:
            return _empty_result(ticker)

        bins = int(max(10, min(n_bins, 100)))
        edges = np.linspace(px_low, px_high, bins + 1)
        mids = (edges[:-1] + edges[1:]) / 2.0
        # Digitize typical prices into bins [0, bins-1]
        idx = np.clip(np.digitize(typical.to_numpy(), edges) - 1, 0, bins - 1)
        volumes = np.zeros(bins, dtype=float)
        for i, v in zip(idx, vol.to_numpy()):
            volumes[int(i)] += float(v)

        total_vol = float(volumes.sum())
        if total_vol <= 0:
            return _empty_result(ticker)

        poc_i = int(np.argmax(volumes))
        poc_px = float(mids[poc_i])

        spot_px = float(spot) if spot is not None and float(spot) > 0 else float(
            hist["Close"].iloc[-1]
        )

        # Volume whose cost basis sits below spot → already "in profit"
        below = volumes[mids < spot_px].sum()
        # Mid exactly at spot: split half (rare); treat as at-cost, not profited
        profited_pct = round(100.0 * float(below) / total_vol, 2)

        range_90 = _value_area_bounds(mids, volumes, poc_i, 0.90)
        range_70 = _value_area_bounds(mids, volumes, poc_i, 0.70)

        return {
            "ticker": ticker,
            "spot": round(spot_px, 4),
            "price_bins": [round(float(x), 4) for x in mids],
            "volume_bins": [float(x) for x in volumes],
            "Average_Cost_POC": round(poc_px, 4),
            "Profited_Shares_Pct": profited_pct,
            "Cost_Range_90": (round(range_90[0], 4), round(range_90[1], 4)),
            "Cost_Range_70": (round(range_70[0], 4), round(range_70[1], 4)),
            "total_volume": int(round(total_vol)),
            "days": int(len(hist)),
        }
    except Exception:
        return _empty_result(ticker)


def render_cost_distribution_chart(
    price_bins: list[float] | np.ndarray,
    volume_bins: list[float] | np.ndarray,
    spot_price: float,
    avg_cost: float,
    *,
    range_70: tuple[float, float] | None = None,
    range_90: tuple[float, float] | None = None,
    ticker: str = "",
) -> Any:
    """
    Horizontal volume-profile bar chart (Plotly).

    Teal bars = cost basis below spot (in profit).
    Orange/red bars = cost basis above spot (overhead supply).
    Blue dashed hline = Average Cost / POC.
    Orange dashed hline = current spot.
    """
    import plotly.graph_objects as go

    ticker = (ticker or "").strip().upper()
    prices = list(price_bins) if price_bins is not None else []
    vols = list(volume_bins) if volume_bins is not None else []
    title = f"{ticker} Cost Distribution" if ticker else "Cost Distribution"

    fig = go.Figure()
    if not prices or not vols or len(prices) != len(vols):
        fig.update_layout(
            title=title,
            template="plotly_dark",
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            height=420,
            margin=dict(l=10, r=10, t=36, b=10),
        )
        return fig

    spot = float(spot_price)
    colors = [
        "#2a9d8f" if float(p) < spot else "#e07a5f"
        for p in prices
    ]
    # At-spot bins: muted gold
    colors = [
        "#f4a261" if abs(float(p) - spot) < 1e-9 else c
        for p, c in zip(prices, colors)
    ]

    # Plot low→high bottom-to-top
    fig.add_trace(
        go.Bar(
            x=vols,
            y=prices,
            orientation="h",
            marker=dict(color=colors, line=dict(width=0)),
            name="Volume @ Cost",
            hovertemplate="Cost $%{y:.2f}<br>Vol %{x:,.0f}<extra></extra>",
        )
    )

    fig.add_hline(
        y=float(avg_cost),
        line_dash="dash",
        line_color="#42a5f5",
        line_width=2,
        annotation_text=f"POC ${float(avg_cost):.2f}",
        annotation_position="top right",
        annotation_font_color="#42a5f5",
    )
    fig.add_hline(
        y=spot,
        line_dash="dash",
        line_color="#ff9800",
        line_width=2,
        annotation_text=f"Spot ${spot:.2f}",
        annotation_position="bottom right",
        annotation_font_color="#ff9800",
    )

    # Optional value-area bands as shaded shapes
    shapes = []
    for bounds, fill, label in [
        (range_90, "rgba(66,165,245,0.08)", "90%"),
        (range_70, "rgba(66,165,245,0.14)", "70%"),
    ]:
        if not bounds or bounds[0] is None or bounds[1] is None:
            continue
        lo, hi = float(bounds[0]), float(bounds[1])
        shapes.append(
            dict(
                type="rect",
                xref="paper",
                x0=0,
                x1=1,
                y0=lo,
                y1=hi,
                fillcolor=fill,
                line=dict(width=0),
                layer="below",
            )
        )

    fig.update_layout(
        title=dict(text=title, font=dict(size=15, color="#e0e0e0"), x=0.01, xanchor="left"),
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=460,
        margin=dict(l=10, r=10, t=40, b=10),
        showlegend=False,
        bargap=0.05,
        shapes=shapes,
        xaxis=dict(
            title="Accumulated Volume",
            showgrid=True,
            gridcolor="rgba(255,255,255,0.06)",
        ),
        yaxis=dict(
            title="Price (Typical Cost)",
            tickprefix="$",
            showgrid=True,
            gridcolor="rgba(255,255,255,0.06)",
        ),
        hovermode="closest",
    )
    return fig


def is_blue_sky_breakout(
    profited_shares_pct: float | None,
    daily_bias: str | None,
    *,
    threshold: float = 95.0,
) -> bool:
    """True when ≥threshold% of cost-basis volume is below spot + HEAVY BULLISH."""
    if profited_shares_pct is None or daily_bias != "HEAVY BULLISH":
        return False
    try:
        return float(profited_shares_pct) >= float(threshold)
    except (TypeError, ValueError):
        return False


BLUE_SKY_TAG = "🌌 BLUE SKY BREAKOUT: Zero Overhead Supply"
