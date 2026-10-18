"""News & Moves page — the stock with pre-market and after-hours, its unusual moves, the
headlines just before each, and how often news spikes faded. Display only."""

from __future__ import annotations

from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

import news_history as nh
import news_moves as nm
from ui.widgets import centered_table_html

ET_TZ = "America/New_York"
UP, DOWN, NEWS = "#22C55E", "#EF4444", "#FACC15"
PERIODS = {"1 day": "1d", "5 days": "5d", "1 month": "1mo"}


@st.cache_data(ttl=300, show_spinner=False)
def _bars(ticker: str, period: str) -> pd.DataFrame:
    from sources import get_source
    src = get_source("yahoo")
    if not hasattr(src, "fetch_extended_history"):
        return pd.DataFrame()
    return src.fetch_extended_history(ticker, interval="5m", period=period)


@st.cache_data(ttl=300, show_spinner=False)
def _news(ticker: str, start_iso: str, end_iso: str) -> pd.DataFrame:
    return nh.load(ticker, datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso))


def figure(bars: pd.DataFrame, moves: pd.DataFrame, news: pd.DataFrame):
    """Candles with extended hours shaded, move markers, and a headline strip."""
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", height=560, showlegend=False,
                      margin=dict(l=10, r=10, t=10, b=10), xaxis_rangeslider_visible=False,
                      hovermode="closest")
    if bars is None or bars.empty:
        return fig
    fig.add_trace(go.Candlestick(
        x=bars.index, open=bars["Open"], high=bars["High"], low=bars["Low"],
        close=bars["Close"], increasing_line_color=UP, decreasing_line_color=DOWN,
        name="AAPL"))
    for day in sorted(set(bars.index.date)):            # shade pre-market and after-hours
        for a, b in (("04:00", "09:30"), ("16:00", "20:00")):
            fig.add_vrect(x0=pd.Timestamp(f"{day} {a}"), x1=pd.Timestamp(f"{day} {b}"),
                          fillcolor="rgba(255,255,255,0.04)", line_width=0, layer="below")
    if moves is not None and not moves.empty:
        for d, sym, col in (("up", "triangle-up", UP), ("down", "triangle-down", DOWN)):
            m = moves[moves["direction"] == d]
            if m.empty:
                continue
            y = [bars.loc[t, "High"] * 1.002 if d == "up" else bars.loc[t, "Low"] * 0.998
                 for t in m["end"]]
            text = [f"{r['pct']:+.2f}% · {r['session']}<br>"
                    + ("<br>".join(h[:90] for h in r["headlines"][:3]) or "no headline")
                    for _, r in m.iterrows()]
            fig.add_trace(go.Scatter(x=m["end"], y=y, mode="markers", text=text,
                                     marker=dict(symbol=sym, size=12, color=col,
                                                 line=dict(width=1, color="#111")),
                                     hovertemplate="%{x|%a %H:%M}<br>%{text}<extra></extra>"))
    if news is not None and not news.empty:
        lo = float(bars["Low"].min())
        fig.add_trace(go.Scatter(
            x=news["ts_et"], y=[lo * 0.997] * len(news), mode="markers",
            marker=dict(symbol="diamond", size=7, color=NEWS),
            text=[h[:110] for h in news["headline"]],
            hovertemplate="%{x|%a %H:%M}<br>%{text}<extra></extra>"))
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]),
                                  dict(bounds=[20, 4], pattern="hour")])
    return fig


def _pct(v) -> str:
    return "" if v is None or pd.isna(v) else f"{v:+.2f}%"


def _share(v) -> str:
    return "—" if v is None or pd.isna(v) else f"{v:.0%}"


def render(cfg: dict) -> None:
    ticker = str(cfg.get("ticker") or "").upper()
    head, tools = st.columns([4, 1])
    head.subheader(f"{ticker} news & moves")
    with tools.popover("Panel settings", use_container_width=True):
        period = st.radio("Period", list(PERIODS), index=1, horizontal=True, key="nm_period")
        k = st.slider("Sensitivity (× typical 5-minute move)", 2.0, 6.0, 3.0, 0.5,
                      key="nm_k", help="Lower finds more moves.")
        if st.button("Fetch past headlines (30 days)", use_container_width=True,
                     key="nm_backfill"):
            try:
                n = nh.backfill(ticker, 30)
                st.success(f"{n} new headlines saved.")
                _news.clear()
            except Exception as exc:
                st.error(f"Could not fetch headlines: {exc}")

    bars = _bars(ticker, PERIODS[period])
    if bars is None or bars.empty:
        st.info("No price bars right now (the data source did not answer).")
        return
    start = bars.index.min().tz_localize(ET_TZ) - timedelta(hours=1)
    end = bars.index.max().tz_localize(ET_TZ) + timedelta(hours=1)
    news = _news(ticker, start.isoformat(), end.isoformat())
    moves = nm.attach_news(nm.find_moves(bars, k=k), news)
    stats = nm.fade_stats(moves)

    def _rate(direction, with_news, col):
        r = stats[(stats["direction"] == direction) & (stats["with_news"] == with_news)]
        return (None, 0) if r.empty else (r.iloc[0][col], int(r.iloc[0]["moves"]))

    c = st.columns(4)
    for col, d, w, label in ((c[0], "up", True, "Up spikes with news"),
                             (c[1], "up", False, "Up spikes, no news"),
                             (c[2], "down", True, "Drops with news"),
                             (c[3], "down", False, "Drops, no news")):
        rate, n = _rate(d, w, "faded_60")
        col.metric(label, "no moves" if not n else f"{_share(rate)} faded",
                   help=f"{n} moves. Faded = gave back at "
                   "least half within 60 minutes.")
    st.caption(f"{len(moves)} unusual moves and {len(news)} saved headlines in this period. "
               "Shaded: pre-market and after-hours. ▲▼ moves (hover for the headlines just "
               "before), ◆ headlines. A small sample says little: read the rates as a "
               "running count, not a rule.")

    st.plotly_chart(figure(bars, moves, news), use_container_width=True,
                    config={"displayModeBar": False})

    if not moves.empty:
        view = moves.sort_values("start", ascending=False).head(40)
        table = pd.DataFrame({
            "When": [f"{t:%a %b %d %H:%M}" for t in view["start"]],
            "Session": view["session"],
            "Move": [_pct(v) for v in view["pct"]],
            "+15 min": [_pct(v) for v in view["ret_15"]],
            "+60 min": [_pct(v) for v in view["ret_60"]],
            "Gave back (60 min)": ["" if pd.isna(v) else f"{v:.0%}" for v in view["retrace_60"]],
            "By close": ["" if pd.isna(v) else f"{v:.0%}" for v in view["retrace_close"]],
            "Headlines before": ["; ".join(h[:70] for h in hs[:2]) or "—"
                                 for hs in view["headlines"]],
        })
        st.markdown("**Unusual moves**")
        st.markdown(centered_table_html(table, uuid="nmmoves"), unsafe_allow_html=True)

    with st.expander("How to read this, and its limits"):
        st.markdown(
            "- A **move** is a 5-minute bar several times larger than the usual 5-minute "
            "move around it. **Gave back** is how much of it was undone later: 0% held, "
            "100% fully undone, above 100% reversed past where it started.\n"
            "- **Headlines before** are those published from 30 minutes before the move to 5 "
            "minutes after it. A headline nearby is not proof it caused the move.\n"
            "- Headlines are only as complete as what was saved: every news fetch is kept from "
            "now on, and 'Fetch past headlines' fills in the last 30 days from Finnhub.\n"
            "- Pre-market and after-hours bars are thin, so their moves are noisier."
        )
