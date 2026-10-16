"""Market-state helpers shared by pages: clock, VWAP state, RSI text, Best Value frame."""

from __future__ import annotations

from datetime import datetime

from dailyScaner import market_is_open
from volume_analysis import get_intraday_vwap_state
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


def render_market_banner() -> None:
    """MARKET CLOSED banner, shown on scan-driven pages when the session is not open."""
    if _market_is_closed():
        now = _now_et()
        st.error(
            f"MARKET CLOSED — DATA IS END-OF-DAY  ({now.strftime('%A %H:%M ET')})",
            icon="🔴",
        )


def live_session_snapshot(bars) -> dict:
    """Latest price and the session's high and low from intraday bars (the last session
    only). Empty when there are no bars. Nothing is estimated."""
    if bars is None or getattr(bars, "empty", True):
        return {}
    need = {"High", "Low", "Close"}
    if not need <= set(bars.columns):
        return {}
    close = bars["Close"].dropna()
    if close.empty:
        return {}
    return {
        "price": float(close.iloc[-1]),
        "high": float(bars["High"].max()),
        "low": float(bars["Low"].min()),
        "at": close.index[-1],
    }
