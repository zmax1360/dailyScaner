"""Volume vs open interest by strike, one expiry: calls above, puts below. Display only."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

import positioning as pos

CALL_VOL, CALL_OI = "#C084FC", "rgba(192,132,252,0.35)"
PUT_VOL, PUT_OI = "#22D3EE", "rgba(34,211,238,0.35)"


def figure(table: pd.DataFrame, *, spot: float | None, title: str = ""):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=("Calls", "Puts"))
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", height=520, barmode="group",
                      bargap=0.15, margin=dict(l=10, r=10, t=40, b=10),
                      legend=dict(orientation="h", y=1.08, x=0),
                      title=dict(text=title, font=dict(size=14, color="#b0bec5"), x=0.01))
    if table is None or table.empty:
        return fig
    x = [float(k) for k in table.index]
    step = float(pd.Series(x).diff().dropna().min()) if len(x) > 1 else 1.0
    for row, (pre, vc, oc) in enumerate((("call", CALL_VOL, CALL_OI),
                                         ("put", PUT_VOL, PUT_OI)), start=1):
        fig.add_trace(go.Bar(x=x, y=table[f"{pre}_oi"], name=f"{pre.title()} open interest",
                             marker_color=oc, width=step * 0.4, hovertemplate="$%{x:g}: OI %{y:,.0f}<extra></extra>"),
                      row=row, col=1)
        fig.add_trace(go.Bar(x=x, y=table[f"{pre}_volume"], name=f"{pre.title()} volume today",
                             marker_color=vc,
                             width=step * 0.4,
                             hovertemplate="$%{x:g}: volume %{y:,.0f}<extra></extra>"),
                      row=row, col=1)
    if spot:
        for row in (1, 2):
            fig.add_vline(x=float(spot), line=dict(color="rgb(250,204,21)", width=1, dash="dot"),
                          row=row, col=1)
    fig.update_xaxes(range=[x[0] - step, x[-1] + step], dtick=step * 2 if len(x) > 16 else step)
    fig.update_xaxes(title_text="strike", row=2, col=1)
    return fig


def render(latest: pd.DataFrame, *, spot: float | None, today: date) -> None:
    st.markdown("**Volume and open interest by strike**")
    if latest is None or latest.empty:
        st.caption("No contracts in the latest scan.")
        return
    exps = sorted(e for e in latest["expiry"].astype(str).str[:10].unique() if e >= today.isoformat())
    if not exps:
        st.caption("No live expiry in the latest scan.")
        return
    c1, c2 = st.columns([2, 1])
    expiry = c1.selectbox("Expiry", exps, key="vbs_expiry",
                          format_func=lambda e: f"{date.fromisoformat(e):%a %b %d} "
                                                f"({(date.fromisoformat(e) - today).days}d)")
    n = c2.select_slider("Strikes", [10, 16, 24, 40, 80], value=24, key="vbs_n")
    table = pos.by_strike(latest, expiry, spot=spot, n_strikes=n)
    st.plotly_chart(figure(table, spot=spot), use_container_width=True,
                    config={"displayModeBar": False})
    fresh = pos.new_positioning(table)
    if not fresh.empty:
        st.caption("Volume at or above open interest (likely new positions; tomorrow's OI "
                   "Change confirms): " + " · ".join(
                       f"{r.side.title()} \\${r.strike:g} {r.volume:,.0f} vs OI "
                       + ("—" if r.open_interest is None else f"{r.open_interest:,.0f}")
                       for r in fresh.sort_values("volume", ascending=False).head(6).itertuples()))
    st.caption("Faint bars: open interest from the previous close. Solid bars: volume today. "
               "Only contracts that traded today are recorded, so quiet strikes are missing.")
