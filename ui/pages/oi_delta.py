"""OI Change page — what yesterday's trading did to open interest. Display only."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

import oi_delta as od
import volume_history as vh
from ui.widgets import centered_table_html

POS, NEG = "rgb(192, 38, 211)", "rgb(34, 211, 238)"


@st.cache_data(ttl=300, show_spinner=False)
def _daily(ticker: str, days: int) -> pd.DataFrame:
    return vh.daily_summary(ticker, days=days)


def _fmt_day(s: str | None) -> str:
    if not s:
        return "—"
    d = date.fromisoformat(s)
    return f"{d:%a %b} {d.day}"


def _num(v) -> str:
    if v is None or pd.isna(v):
        return ""
    return f"{v:+,.0f}"


def profile_figure(prof: pd.DataFrame, *, spot: float | None):
    """Horizontal bars per strike: call OI change and put OI change side by side."""
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", barmode="group", bargap=0.25,
                      margin=dict(l=10, r=10, t=10, b=10),
                      height=min(max(26 * len(prof) + 90, 260), 1100),
                      legend=dict(orientation="h", y=1.02, x=0))
    if prof.empty:
        return fig
    for side, color in (("CALL", POS), ("PUT", NEG)):
        fig.add_trace(go.Bar(y=list(prof.index), x=prof[side].values, orientation="h",
                             name=f"{side.title()} OI change", marker_color=color,
                             hovertemplate="$%{y:g}: %{x:+,.0f}<extra></extra>"))
    if spot:
        fig.add_hline(y=float(spot), line=dict(color="rgb(103, 232, 249)", width=1.5, dash="dot"),
                      annotation_text=f"spot {float(spot):,.2f}", annotation_position="top left")
    fig.add_vline(x=0, line=dict(color="rgba(255,255,255,0.35)", width=1))
    fig.update_yaxes(tickmode="array", tickvals=list(prof.index),
                     ticktext=[f"{k:g}" for k in prof.index])
    return fig


def chart_rows(table: pd.DataFrame) -> pd.DataFrame:
    """Rows the chart draws: everything except suspect (⚠) rows."""
    return table[~table["suspect"].astype(bool)]


def render(cfg: dict) -> None:
    ticker = str(cfg.get("ticker") or "").upper()
    spot = (cfg.get("latest_archive") or {}).get("spot")
    head, tools = st.columns([4, 1])
    head.subheader(f"{ticker} open interest change")

    daily = _daily(ticker, 15)
    days = od.sessions(daily)
    if len(days) < 2:
        st.info("Two recorded sessions are needed. Open interest is read from the "
                "scanner's recorded chains; it fills in as sessions are recorded.")
        return

    with tools.popover("Panel settings", use_container_width=True):
        session = st.selectbox("OI as reported on", list(reversed(days[1:])),
                               format_func=_fmt_day, key="oid_session")
        max_dte = st.select_slider("Expiries within (days)", [0, 1, 2, 5, 7, 14, 30, 60, 365],
                                   value=7, key="oid_dte")
        band = st.select_slider("Strikes within (% of spot)", [1, 2, 3, 5, 10, 20, 100],
                                value=5, key="oid_band")
        min_change = st.number_input("Smallest change shown", 0, 100000, 100, step=100,
                                     key="oid_min")

    table, meta = od.change_table(daily, session=session)
    shown = od.filtered(table, spot=spot, max_dte=max_dte, band_pct=band, min_change=min_change)
    h = od.headline(od.filtered(table, spot=spot, max_dte=max_dte, band_pct=band))

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Trading day", _fmt_day(meta["previous"]),
              help=f"OI reported on {_fmt_day(meta['session'])} counts positions after the "
                   f"{_fmt_day(meta['previous'])} session.")
    m2.metric("Call OI change", _num(h["call_change"]) or "—")
    m3.metric("Put OI change", _num(h["put_change"]) or "—")
    for col, label, t in ((m4, "Largest call add", h["top_call"]),
                          (m5, "Largest put add", h["top_put"])):
        col.metric(label, f"${t['strike']:g} {t['oi_change']:+,.0f}" if t else "—",
                   help=f"Expiry {_fmt_day(t['expiry'])}." if t else None)
    st.caption(f"{h['contracts']} contracts matched on both days within {max_dte} days and "
               f"{band}% of spot. A contract that did not trade on one of the two days was "
               "not recorded that day and is left out."
               + (f" {h['suspect']} changed by more than that day's volume (⚠): one of the "
                  "two OI readings is wrong, so they are left out of the totals."
                  if h["suspect"] else ""))

    if shown is None or shown.empty:
        st.caption("No change at or above the size shown.")
        return

    # Suspect rows (⚠) stay in the table but not in the chart: one impossible reading
    # would set the scale for every real bar.
    st.plotly_chart(profile_figure(od.by_strike(chart_rows(shown)), spot=spot),
                    use_container_width=True, config={"displayModeBar": False})

    view = shown.head(40).copy()
    view["expiry"] = [_fmt_day(str(e)[:10]) for e in view["expiry"]]
    view["strike"] = [f"${k:g}" for k in view["strike"]]
    for c in ("oi_before", "oi_after", "traded"):
        view[c] = [("" if pd.isna(v) else f"{v:,.0f}") for v in view[c]]
    view["oi_change"] = [_num(v) for v in view["oi_change"]]
    view["oi_change_pct"] = [("" if pd.isna(v) else f"{v:+.0f}%") for v in view["oi_change_pct"]]
    view["opened_share"] = [("" if pd.isna(v) else f"{v:+.2f}") for v in view["opened_share"]]
    view["oi_change"] = [f"⚠ {c}" if bad else c for c, bad in zip(view["oi_change"], view["suspect"])]
    view = view.drop(columns=["suspect"])
    view = view.rename(columns={"side": "Side", "strike": "Strike", "expiry": "Expiry",
                                "dte": "DTE", "oi_before": "OI before", "oi_after": "OI after",
                                "oi_change": "Change", "oi_change_pct": "Change %",
                                "traded": "Volume that day", "opened_share": "Change ÷ volume"})
    st.markdown(centered_table_html(view, uuid="oid"), unsafe_allow_html=True)

    with st.expander("How to read this, and its limits"):
        st.markdown(
            "- Open interest is settled overnight. The OI shown on a session is the count "
            "after the **previous** session's trading, so this page says what that day did.\n"
            "- **Change ÷ volume** near +1: that day's volume mostly opened positions. Near "
            "0: it traded and closed the same day, or opens and closes cancelled. Negative: "
            "positions were closed.\n"
            "- It does not say who bought or sold. A rise in put OI can be bought protection "
            "or sold premium.\n"
            "- Only contracts that traded on both days are recorded, so quiet strikes are "
            "missing rather than shown as zero.\n"
            "- **⚠** marks a change larger than that day's volume. One day's trading cannot "
            "do that, so one of the two OI readings from the data source is wrong."
        )
