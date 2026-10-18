"""Contract page — one option over time: price, delta, gamma, IV, and what moved its
price between two moments. Display only."""

from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

import contract_view as cv

PRICE, SPOT, DELTA, GAMMA, IV = "#C084FC", "#67E8F9", "#22C55E", "#FB923C", "#FACC15"
TERM_LABEL = {"delta": "Delta (direction)", "gamma": "Gamma (curvature)",
              "theta": "Theta (time)", "vega": "Vega (IV)", "residual": "Residual"}


@st.cache_data(ttl=300, show_spinner=False)
def _contracts(ticker: str) -> pd.DataFrame:
    return cv.contracts(ticker)


@st.cache_data(ttl=300, show_spinner=False)
def _spot(scan_id: str) -> float | None:
    return cv.archive_spot(scan_id)


@st.cache_data(ttl=120, show_spinner=False)
def _history(ticker: str, side: str, strike: float, expiry: str) -> pd.DataFrame:
    raw = cv.raw_history(ticker, side, strike, expiry)
    return cv.history(raw, side=side, strike=strike, expiry=expiry, spot_of=_spot)


def _fmt_exp(e: str) -> str:
    d = date.fromisoformat(str(e)[:10])
    return f"{d:%a %b} {d.day}, {d:%Y}"


def default_strike(strikes: list[float], spot: float | None) -> float | None:
    if not strikes:
        return None
    if not spot:
        return strikes[len(strikes) // 2]
    return min(strikes, key=lambda k: (abs(k - float(spot)), k))


def figures(h: pd.DataFrame):
    """(price & spot, delta & gamma, IV) — three small charts on one time axis."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.05,
                        row_heights=[0.45, 0.30, 0.25],
                        specs=[[{"secondary_y": True}], [{"secondary_y": True}],
                               [{"secondary_y": False}]])
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", height=720, hovermode="x unified",
                      margin=dict(l=10, r=10, t=30, b=10),
                      legend=dict(orientation="h", y=1.04, x=0))
    # New York wall-clock time, so the night/weekend gaps below line up with the session
    x = pd.to_datetime(h["ts"], utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None)
    fig.add_trace(go.Scatter(x=x, y=h["price"], name="Option price", mode="lines+markers",
                             line=dict(color=PRICE, width=2), marker=dict(size=4),
                             connectgaps=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=x, y=h["spot"], name="Stock", mode="lines",
                             line=dict(color=SPOT, width=1, dash="dot")),
                  row=1, col=1, secondary_y=True)
    fig.add_trace(go.Scatter(x=x, y=h["delta"], name="Delta", mode="lines",
                             line=dict(color=DELTA, width=2)), row=2, col=1)
    fig.add_trace(go.Scatter(x=x, y=h["gamma"], name="Gamma", mode="lines",
                             line=dict(color=GAMMA, width=2)), row=2, col=1, secondary_y=True)
    fig.add_trace(go.Scatter(x=x, y=h["iv"] * 100, name="IV %", mode="lines",
                             line=dict(color=IV, width=2)), row=3, col=1)
    fig.update_yaxes(title_text="$ option", row=1, col=1)
    fig.update_yaxes(title_text="$ stock", row=1, col=1, secondary_y=True, showgrid=False)
    fig.update_yaxes(title_text="delta", row=2, col=1)
    fig.update_yaxes(title_text="gamma", row=2, col=1, secondary_y=True, showgrid=False)
    fig.update_yaxes(title_text="IV %", row=3, col=1)
    # no x gaps for nights and weekends
    fig.update_xaxes(rangebreaks=[dict(bounds=["sat", "mon"]),
                                  dict(bounds=[16.5, 9.5], pattern="hour")])
    return fig


def waterfall(a: dict):
    import plotly.graph_objects as go

    cents = [a["start_price"] * 100] + [a[k] * 100 for k in cv.TERMS] + [0]
    fig = go.Figure(go.Waterfall(
        measure=["absolute"] + ["relative"] * len(cv.TERMS) + ["total"],
        x=["Start"] + [TERM_LABEL[k] for k in cv.TERMS] + ["End"], y=cents,
        text=[f"{a['start_price'] * 100:,.1f}¢"] + [f"{a[k] * 100:+,.1f}¢" for k in cv.TERMS]
        + [f"{a['end_price'] * 100:,.1f}¢"], textposition="outside",
        increasing={"marker": {"color": "#22C55E"}},
        decreasing={"marker": {"color": "#C026D3"}},
        totals={"marker": {"color": "#8B8194"}}, connector={"line": {"color": "#3A3340"}}))
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", height=340, showlegend=False,
                      margin=dict(l=10, r=10, t=20, b=10), yaxis_title="¢ per share")
    return fig


def render(cfg: dict) -> None:
    ticker = str(cfg.get("ticker") or "").upper()
    spot = (cfg.get("latest_archive") or {}).get("spot")
    st.subheader(f"{ticker} contract over time")

    allc = _contracts(ticker)
    if allc.empty:
        st.info("No recorded contracts yet. They are recorded with each scan.")
        return
    today = date.today().isoformat()
    c1, c2, c3, c4 = st.columns([2, 1, 2, 1])
    show_expired = c4.checkbox("Expired", value=False, key="cv_expired",
                               help="Include contracts that have already expired.")
    pool = allc if show_expired else allc[allc["expiry"] >= today]
    expiries = sorted(pool["expiry"].unique())
    if not expiries:
        st.info("No live recorded contracts. Tick 'Expired' to look at past ones.")
        return
    expiry = c1.selectbox("Expiry", expiries, format_func=_fmt_exp, key="cv_expiry")
    side = c2.radio("Type", ["CALL", "PUT"], horizontal=True, key="cv_side")
    strikes = sorted(pool[(pool["expiry"] == expiry) & (pool["side"] == side)]["strike"])
    if not strikes:
        st.caption(f"No recorded {side.lower()}s for this expiry.")
        return
    dflt = default_strike(strikes, spot)
    strike = c3.selectbox("Strike", strikes, index=strikes.index(dflt),
                          format_func=lambda k: f"${k:g}", key=f"cv_strike_{expiry}_{side}")

    h = _history(ticker, side, float(strike), expiry)
    if h.empty:
        st.caption("No snapshots for this contract.")
        return
    last = h.dropna(subset=["price"]).iloc[-1] if h["price"].notna().any() else h.iloc[-1]
    m = st.columns(6)
    m[0].metric("Price", "—" if pd.isna(last["price"]) else f"${last['price']:.2f}",
                help=f"From the {last['price_from'] or 'n/a'} at {last['ts']:%a %H:%M} ET.")
    m[1].metric("Delta", "—" if pd.isna(last["delta"]) else f"{last['delta']:.3f}")
    m[2].metric("Gamma", "—" if pd.isna(last["gamma"]) else f"{last['gamma']:.4f}")
    m[3].metric("Theta / day", "—" if pd.isna(last["theta"]) else f"${last['theta']:.3f}")
    m[4].metric("Vega / IV pt", "—" if pd.isna(last["vega"]) else f"${last['vega']:.3f}")
    m[5].metric("IV", "—" if pd.isna(last["iv"]) else f"{last['iv'] * 100:.1f}%",
                help="Solved from the price at that scan's stock price"
                     + (" (vendor IV: the price did not solve)." if last["iv_from"] == "vendor"
                        else "."))

    st.plotly_chart(figures(h), use_container_width=True, config={"displayModeBar": False})

    usable = h.dropna(subset=["spot", "price", "iv"])
    if len(usable) < 2:
        st.caption("Fewer than two usable snapshots, so there is nothing to attribute yet.")
        return
    st.markdown("**Where the price went**")
    times = list(usable["ts"])
    start, end = st.select_slider(
        "From / to", options=times, value=(times[0], times[-1]),
        format_func=lambda t: f"{t:%a %b %d %H:%M}", key=f"cv_range_{expiry}_{side}_{strike}")
    a = cv.attribution(h, side=side, strike=float(strike), start=start, end=end)
    if a is None:
        st.caption("Pick two different snapshots.")
        return
    st.plotly_chart(waterfall(a), use_container_width=True, config={"displayModeBar": False})
    st.caption(
        f"{a['start_price']:.2f} → {a['end_price']:.2f} ({a['change'] * 100:+.1f}¢ per share, "
        f"{'-' if a['change'] < 0 else '+'}\\${abs(a['change']) * 100:,.0f} per contract) "
        f"over {a['steps']} steps. "
        "Each step re-prices the option one input at a time: time, then IV, then the stock.")

    with st.expander("How to read this, and its limits"):
        st.markdown(
            "- **Delta**: the part explained by the stock's move at the start delta. "
            "**Gamma**: the extra from delta changing along the way (long options gain it "
            "when the stock swings). **Theta**: time passing. **Vega**: implied volatility "
            "changing. **Residual**: what the model does not explain.\n"
            "- A contract is recorded only on scans where it has traded that day, at most "
            "every 15 minutes, so quiet contracts have gaps and a short trade may show only "
            "one or two points.\n"
            "- Price is the bid/ask mid, or the last trade when only that exists (a stale "
            "last trade shows up as residual or a jump in IV).\n"
            "- Overnight the clock still runs (theta) while the price is frozen, so IV "
            "absorbs it: after-hours steps show theta and vega that cancel.\n"
            "- Option quotes lag the stock by minutes. IV solved at the scan's stock price "
            "therefore jumps around on fast days, and over a short window part of a move "
            "can land in vega instead of delta/gamma. The total is always exact."
        )
