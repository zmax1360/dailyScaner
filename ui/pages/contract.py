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


CHARTS = [  # (column, title, colour, y-axis label, scale)
    ("price", "Option price", PRICE, "$ per share", 1.0),
    ("spot", "Stock price", SPOT, "$", 1.0),
    ("delta", "Delta", DELTA, "delta", 1.0),
    ("gamma", "Gamma", GAMMA, "gamma", 1.0),
    ("iv", "Implied volatility", IV, "IV %", 100.0),
]


def _wall_clock(ts: pd.Series) -> pd.Series:
    """New York wall-clock time, so the night/weekend gaps line up with the session."""
    return pd.to_datetime(ts, utc=True).dt.tz_convert("America/New_York").dt.tz_localize(None)


def chart(h: pd.DataFrame, column: str, title: str, color: str, ylabel: str,
          scale: float = 1.0, *, height: int = 260):
    """One small line chart of one series over time (a gap where the value is missing)."""
    import plotly.graph_objects as go

    fig = go.Figure(go.Scatter(
        x=_wall_clock(h["ts"]), y=h[column] * scale, mode="lines+markers", name=title,
        line=dict(color=color, width=2), marker=dict(size=3), connectgaps=False,
        hovertemplate="%{x|%a %b %d %H:%M}<br>%{y:.4g}<extra></extra>"))
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        height=height, margin=dict(l=10, r=10, t=34, b=10), showlegend=False,
        title=dict(text=title, font=dict(size=14, color="#e0e0e0"), x=0.01))
    fig.update_yaxes(title_text=ylabel, gridcolor="rgba(255,255,255,0.06)")
    fig.update_xaxes(gridcolor="rgba(255,255,255,0.06)",
                     rangebreaks=[dict(bounds=["sat", "mon"]),
                                  dict(bounds=[16.5, 9.5], pattern="hour")])
    return fig


def charts(h: pd.DataFrame) -> list:
    """The five charts, in display order."""
    return [chart(h, *spec) for spec in CHARTS]


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

    figs = charts(h)
    for i in range(0, len(figs), 2):
        cols = st.columns(2)
        for col, fig in zip(cols, figs[i:i + 2]):
            col.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    _render_what_if(h, side=side, strike=float(strike), expiry=expiry, spot=spot)

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


HOLD_CHOICES = {"15 min": 15, "30 min": 30, "1 hour": 60, "2 hours": 120, "4 hours": 240}
PNL_COLORS = ["#67E8F9", "#C084FC", "#FACC15", "#9CA3AF"]


def what_if_horizons(now, expiry: str, hold_min: int):
    """(label, time) pairs: now, after the hold, today's close (if still ahead), expiry."""
    from datetime import datetime as _dt, time as _time, timedelta as _td
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")
    now = now.astimezone(et)
    close = _dt.combine(now.date(), _time(16, 0), tzinfo=et)
    exp_close = _dt.combine(date.fromisoformat(str(expiry)[:10]), _time(16, 0), tzinfo=et)
    out = [("Now", now), (f"In {hold_min} min", min(now + _td(minutes=hold_min), exp_close))]
    if now < close < exp_close and close > out[1][1]:
        out.append(("Today's close", close))
    out.append(("At expiry", exp_close))
    return out


def what_if_figure(curves: pd.DataFrame, *, spot: float | None):
    import plotly.graph_objects as go

    fig = go.Figure()
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", height=380, hovermode="x unified",
                      margin=dict(l=10, r=10, t=10, b=10),
                      legend=dict(orientation="h", y=1.08, x=0),
                      xaxis_title="stock price", yaxis_title="P&L per contract ($)")
    for col, color in zip(curves.columns, PNL_COLORS):
        fig.add_trace(go.Scatter(x=curves.index, y=curves[col], name=col, mode="lines",
                                 line=dict(color=color, width=2,
                                           dash="dot" if col == "At expiry" else "solid"),
                                 hovertemplate="%{y:+,.0f} $<extra>" + col + "</extra>"))
    fig.add_hline(y=0, line=dict(color="rgba(255,255,255,0.4)", width=1))
    if spot:
        fig.add_vline(x=float(spot), line=dict(color="rgb(250,204,21)", width=1, dash="dot"),
                      annotation_text=f"stock {float(spot):,.2f}",
                      annotation_font=dict(color="rgb(250,204,21)", size=11))
    return fig


def _render_what_if(h: pd.DataFrame, *, side: str, strike: float, expiry: str,
                    spot: float | None) -> None:
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo
    import numpy as np

    ok = h.dropna(subset=["iv", "spot", "price"])
    if ok.empty:
        return
    last = ok.iloc[-1]
    s0 = float(spot or last["spot"])
    st.markdown("**What-if P&L (per contract)**")
    c1, c2, c3 = st.columns(3)
    entry = c1.number_input("Entry price", min_value=0.01, value=round(float(last["price"]), 2),
                            step=0.05, key=f"wi_entry_{expiry}_{side}_{strike}")
    hold = c2.select_slider("Hold for", list(HOLD_CHOICES), value="1 hour",
                            key=f"wi_hold_{expiry}_{side}_{strike}")
    width = c3.select_slider("Price range", ["±1%", "±2%", "±3%", "±5%"], value="±2%",
                             key=f"wi_range_{expiry}_{side}_{strike}")
    pct = float(width.strip("±%")) / 100.0
    now = _dt.now(ZoneInfo("America/New_York"))
    if cv.gex.years_to_expiry(expiry, now) is None:
        st.caption("This contract has expired, so there is nothing to project.")
        return
    prices = np.linspace(s0 * (1 - pct), s0 * (1 + pct), 81)
    curves = cv.pnl_curves(side, strike, expiry, iv=float(last["iv"]), entry=float(entry),
                           horizons=what_if_horizons(now, expiry, HOLD_CHOICES[hold]),
                           prices=prices)
    st.plotly_chart(what_if_figure(curves, spot=s0), use_container_width=True,
                    config={"displayModeBar": False})
    hold_col = curves.columns[1]
    be = cv.breakevens(curves[hold_col])
    flat = curves[hold_col].iloc[int(np.argmin(np.abs(curves.index - s0)))]
    st.caption(
        f"IV held at {float(last['iv']) * 100:.1f}%. If the stock does not move, after "
        f"{hold.lower()} this is {flat:+,.0f} \\$ per contract (time decay). "
        + (f"Break-even then: \\${', '.join(f'{b:,.2f}' for b in be)}." if be
           else "No break-even inside this price range.")
        + " Each line is the same option at a later time: the gap between them is theta, "
          "the bend is gamma.")
