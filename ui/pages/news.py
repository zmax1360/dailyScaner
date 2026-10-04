"""Market News page — headline timeline for the selected ticker."""

from __future__ import annotations

from datetime import datetime

from news_service import get_market_news
import streamlit as st


@st.cache_data(ttl=300)
def _cached_market_news(tickers: tuple[str, ...], limit: int = 15) -> list[dict]:
    """Cached multi-ticker headline timeline for the Market News tab."""
    return get_market_news(list(tickers), limit=limit)


def _fmt_news_ts(ts: int) -> str:
    """Unix seconds → readable local string; em-dash if missing."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def render(cfg: dict) -> None:
    """Market News — timeline for the sidebar-selected ticker only."""
    ticker = (cfg.get("ticker") or "").strip().upper()
    if not ticker:
        st.info("Pick a ticker in the sidebar.")
        return

    st.caption(f"Headlines for **{ticker}** (same ticker as the sidebar selector).")
    articles = _cached_market_news((ticker,), limit=15)
    if not articles:
        st.info(f"No headlines found for {ticker}.")
        return

    _badge = {
        "BULLISH": ("#00c853", "#0d2818"),
        "BEARISH": ("#d50000", "#2a1010"),
        "NEUTRAL": ("#9e9e9e", "#1a1a1a"),
    }

    st.markdown(f"### Latest {len(articles)} headlines — {ticker}")
    for a in articles:
        bias = a.get("news_bias") or "NEUTRAL"
        fg, bg = _badge.get(bias, _badge["NEUTRAL"])
        headline = (a.get("headline") or "(no title)").replace("<", "&lt;").replace(">", "&gt;")
        url = (a.get("url") or "").replace('"', "&quot;")
        source = (a.get("source") or "Unknown").replace("<", "&lt;")
        when = _fmt_news_ts(int(a.get("datetime") or 0))
        if url:
            title_html = (
                f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
                f'style="color:#e3e3e3;text-decoration:none">{headline}</a>'
            )
        else:
            title_html = f'<span style="color:#e3e3e3">{headline}</span>'

        st.markdown(
            f'<div style="border-left:3px solid {fg};padding:0.7rem 1rem;'
            f'margin:0.5rem 0;background:{bg};border-radius:0 6px 6px 0">'
            f'<div style="display:flex;gap:0.65rem;align-items:center;flex-wrap:wrap;'
            f'margin-bottom:0.35rem">'
            f'<span style="font-size:0.72rem;font-weight:700;color:{fg};'
            f'border:1px solid {fg};padding:0.12rem 0.5rem;border-radius:4px">'
            f'{bias}</span>'
            f'<span style="color:#888;font-size:0.85rem">{source} · {when}</span>'
            f'</div>'
            f'<div style="font-size:1.05rem;font-weight:600;line-height:1.35">'
            f'{title_html}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )
