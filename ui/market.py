"""Market-state helpers shared by pages: clock, VWAP state, RSI text, Best Value frame."""

from __future__ import annotations

from datetime import datetime

from best_value import build_best_value_df
from dailyScaner import market_is_open
from volume_analysis import get_intraday_vwap_state
import pandas as pd
import streamlit as st
from ui.common import ET


def _now_et() -> datetime:
    return datetime.now(ET)


def _market_is_closed() -> bool:
    """True when the regular session is not open right now."""
    return not market_is_open(_now_et())


def _rsi_plain(rsi: float | None) -> str:
    """Plain text RSI label (no HTML) for use in DataFrames."""
    if rsi is None:
        return "—"
    if rsi >= 80:  return f"OVERBOUGHT ({rsi:.1f})"
    if rsi >= 60:  return f"BULLISH ({rsi:.1f})"
    if rsi >= 45:  return f"NEUTRAL ({rsi:.1f})"
    if rsi >= 30:  return f"BEARISH ({rsi:.1f})"
    return f"OVERSOLD ({rsi:.1f})"


@st.cache_data(ttl=60)
def _cached_vwap_state(ticker: str) -> dict:
    """Cached 5m VWAP reclaim state (1 min TTL)."""
    return get_intraday_vwap_state(ticker)


def _build_best_value_df(
    vol_curr: dict,
    spot: float,
    vol_prev: dict | None,
    min_volume: int = 500,
    daily_bias: str | None = None,
    market_state: str | None = None,
    news_bias: str | None = None,
    vwap_state: str | None = None,
    profited_shares_pct: float | None = None,
    *,
    upper_1sd: float | None = None,
    lower_1sd: float | None = None,
    optimal_strategy: str | None = None,
    has_catalyst: bool = False,
    spot_below_support: bool = False,
    odte_info: dict | None = None,
    pov_info: dict | None = None,
) -> pd.DataFrame:
    """Build + score via shared best_value engine (expiry/0DTE + phantom-ΔVol safe)."""
    return build_best_value_df(
        vol_curr, spot, vol_prev,
        min_volume=min_volume,
        daily_bias=daily_bias,
        market_state=market_state,
        news_bias=news_bias,
        vwap_state=vwap_state,
        profited_shares_pct=profited_shares_pct,
        upper_1sd=upper_1sd,
        lower_1sd=lower_1sd,
        optimal_strategy=optimal_strategy,
        has_catalyst=has_catalyst,
        spot_below_support=spot_below_support,
        odte_info=odte_info,
        pov_info=pov_info,
    )


def render_market_banner() -> None:
    """MARKET CLOSED banner, shown on scan-driven pages when the session is not open."""
    if _market_is_closed():
        now = _now_et()
        st.error(
            f"MARKET CLOSED — DATA IS END-OF-DAY  ({now.strftime('%A %H:%M ET')})",
            icon="🔴",
        )
