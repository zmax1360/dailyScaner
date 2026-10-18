"""Gamma page — net gamma exposure by strike and expiry. One question per panel.

Reads the latest full-chain snapshot from data/volume_history.db. Display only:
no scoring, no network, no writes.
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pandas as pd
import streamlit as st

import gex
import volume_history as vh

POS_RGB = (147, 51, 234)     # purple — call-heavy (positive)
NEG_RGB = (13, 148, 136)     # teal   — put-heavy (negative)
# Walls get an inset outline as well as a fill, so they stand out at a glance.
CALL_WALL_OUTLINE = "rgb(236, 72, 153)"
PUT_WALL_OUTLINE = "rgb(165, 243, 252)"
CALL_WALL_CSS = ("background-color: rgb(250, 204, 21); color: #111; font-weight: 700; "
                 f"box-shadow: inset 0 0 0 2px {CALL_WALL_OUTLINE};")
PUT_WALL_CSS = ("background-color: rgb(45, 212, 191); color: #111; font-weight: 700; "
                f"box-shadow: inset 0 0 0 2px {PUT_WALL_OUTLINE};")
NET_CSS = "font-weight: 700; border-top: 2px solid #666;"
NET_LABEL = "NET $"

EXP_CURRENT, EXP_WEEK, EXP_ALL, EXP_PICK = "Current", "Current week", "All", "Pick dates"
STRIKE_CHOICES = ["8", "16", "32", "64", "All"]
UNIT_LABELS = {"Dollars per $1": gex.UNIT_DOLLAR, "Shares per $1": gex.UNIT_SHARES,
               "Dollars per 1%": gex.UNIT_PCT}
UNIT_CAPTION = {gex.UNIT_DOLLAR: "dollars of dealer hedging per $1 move",
                gex.UNIT_SHARES: "shares of dealer hedging per $1 move",
                gex.UNIT_PCT: "dollars of dealer hedging per 1% move"}
NET_LABELS = {gex.UNIT_DOLLAR: "NET $", gex.UNIT_SHARES: "NET sh", gex.UNIT_PCT: "NET $"}
IV_LABELS = {"From quotes": gex.IV_QUOTE, "Vendor": gex.IV_VENDOR}


def _cell_css(v, vmax: float) -> str:
    if pd.isna(v) or vmax <= 0:
        return ""
    r, g, b = POS_RGB if v >= 0 else NEG_RGB
    alpha = 0.12 + 0.78 * min(abs(float(v)) / vmax, 1.0)
    return f"background-color: rgba({r},{g},{b},{alpha:.2f}); color: #fff;"


COLOR_SCALE_PCTL = 90         # the colour scale tops out here, not at the largest cell


def color_scale_max(matrix: pd.DataFrame) -> float:
    """Top of the colour scale: the 90th percentile of the non-zero |values|.

    GEX usually has one cell near the money an order of magnitude above its
    neighbours; scaling to that one cell paints every other cell the same faint
    shade. Cells above this value get the full colour. 0.0 for an empty map."""
    if matrix is None or matrix.empty:
        return 0.0
    vals = matrix.abs().to_numpy(dtype=float).ravel()
    vals = vals[np.isfinite(vals) & (vals > 0)]
    if vals.size == 0:
        return 0.0
    return float(np.percentile(vals, COLOR_SCALE_PCTL))


def style_matrix(matrix: pd.DataFrame, wall_map: dict, *, spot_strike: float | None = None,
                 net_label: str = NET_LABEL):
    """Colour by sign and size, mark each expiry's walls, append a NET row.

    Returns a Styler whose row labels are the strikes (the one nearest spot is marked).
    """
    vmax = color_scale_max(matrix)
    labels = [f"{k:g}  ◀ spot" if k == spot_strike else f"{k:g}" for k in matrix.index]
    shown = matrix.copy()
    shown.index = labels
    shown.columns.name = None            # otherwise "expiry" prints above the strike column
    shown.loc[net_label] = [
        (wall_map.get(str(exp)) or {}).get("net", float("nan")) for exp in matrix.columns
    ]
    css = pd.DataFrame("", index=shown.index, columns=shown.columns)
    for exp in matrix.columns:
        w = wall_map.get(str(exp)) or {}
        for strike, label in zip(matrix.index, labels):
            if w.get("call_wall") is not None and strike == w["call_wall"]:
                css.loc[label, exp] = CALL_WALL_CSS
            elif w.get("put_wall") is not None and strike == w["put_wall"]:
                css.loc[label, exp] = PUT_WALL_CSS
            else:
                css.loc[label, exp] = _cell_css(matrix.loc[strike, exp], vmax)
        css.loc[net_label, exp] = NET_CSS
    return shown.style.apply(lambda _: css, axis=None).format(gex.fmt_money, na_rep="")


def pick_expiries(mode: str, available: list[str], picked: list[str] | None) -> list[str]:
    if mode == EXP_CURRENT:
        return available[:1]
    if mode == EXP_WEEK:
        return gex.current_week(available)
    if mode == EXP_PICK:
        chosen = [e for e in available if e in set(picked or [])]
        return chosen or available[:1]
    return list(available)


def _fmt_exp(e: str) -> str:
    d = date.fromisoformat(e)
    return f"{d:%b} {d.day}"


TABLE_STYLES = [
    {"selector": "", "props": "width:100%; border-collapse:collapse; font-size:0.88rem; "
                              "font-variant-numeric:tabular-nums;"},
    {"selector": "th, td", "props": "text-align:center; padding:7px 10px; border:0; "
                                    "border-bottom:1px solid rgba(255,255,255,0.07);"},
    {"selector": "th.col_heading", "props": "font-weight:600; color:#b0bec5;"},
    {"selector": "th.row_heading, th.blank",
     "props": "text-align:left; width:9rem; font-weight:500; color:#b0bec5; white-space:nowrap;"},
]


def matrix_html(styled, col_labels: list[str]) -> str:
    """The styled matrix as an HTML table with every value centred in its cell.

    st.dataframe always right-aligns numbers, so the map is drawn as HTML instead.
    Leading whitespace is stripped so Markdown never reads a line as a code block.
    """
    html = (styled.set_uuid("gex").relabel_index(col_labels, axis=1)
            .set_table_styles(TABLE_STYLES).to_html())
    flat = "".join(line.strip() for line in html.splitlines() if line.strip())
    return f'<div style="max-height:75vh; overflow:auto;">{flat}</div>'


LAYOUT_HEAT, LAYOUT_PROFILE = "Heat", "Profile"
PROFILE_POS, PROFILE_NEG = "rgb(192, 38, 211)", "rgb(34, 211, 238)"
PROFILE_CALL_WALL, PROFILE_PUT_WALL = "rgb(250, 204, 21)", "rgb(45, 212, 191)"


def profile_series(matrix: pd.DataFrame) -> pd.Series:
    """Net GEX per strike across the expiries shown (a strike with no usable contract in
    any of them is left out, never drawn as zero)."""
    if matrix is None or matrix.empty:
        return pd.Series(dtype=float)
    return matrix.sum(axis=1, min_count=1).dropna().sort_index(ascending=False)


PROFILE_CURVE = "rgb(250, 250, 210)"


def profile_figure(matrix: pd.DataFrame, *, spot: float, title: str = "",
                   flip: float | None = None, curve: tuple | None = None):
    """Horizontal bars of net GEX by strike on a price axis: positive to the right,
    negative to the left, the largest of each highlighted, and a line at spot.

    ``curve`` = (prices, net): the book's total net GEX if the stock were at each price,
    drawn as a line on its own scale (top axis). Where it crosses zero is the flip."""
    import plotly.graph_objects as go

    net = profile_series(matrix)
    fig = go.Figure()
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=10, r=10, t=34 if title else 10, b=10), showlegend=False,
        title=dict(text=title, font=dict(size=14, color="#b0bec5"), x=0.5, xanchor="center"),
        height=min(max(34 * len(net) + 90, 260), 1200), bargap=0.25,
    )
    if net.empty:
        return fig
    pos, neg = net[net > 0], net[net < 0]
    call_wall = pos.idxmax() if not pos.empty else None
    put_wall = neg.idxmin() if not neg.empty else None
    colors = [PROFILE_CALL_WALL if k == call_wall else PROFILE_PUT_WALL if k == put_wall
              else PROFILE_POS if v >= 0 else PROFILE_NEG for k, v in net.items()]
    step = float(pd.Series(net.index).sort_values().diff().dropna().min()) if len(net) > 1 else 1.0
    fig.add_trace(go.Bar(
        x=net.values, y=list(net.index), orientation="h", name="Net GEX",
        marker_color=colors, width=step * 0.7,
        text=[gex.fmt_money(v) for v in net.values], textposition="outside",
        textfont=dict(color="#e0e0e0", size=12), cliponaxis=False,
        hovertemplate="$%{y:g}: %{text}<extra></extra>",
    ))
    fig.add_hline(y=float(spot), line=dict(color="rgb(103, 232, 249)", width=1.5, dash="dot"),
                  annotation_text=f"spot {float(spot):,.2f}", annotation_position="top left",
                  annotation_font=dict(color="rgb(103, 232, 249)", size=11))
    if flip is not None:
        fig.add_hline(y=float(flip), line=dict(color="rgb(251, 146, 60)", width=1.5, dash="dash"),
                      annotation_text=f"gamma flip {float(flip):,.2f}",
                      annotation_position="bottom left",
                      annotation_font=dict(color="rgb(251, 146, 60)", size=11))
    fig.add_vline(x=0, line=dict(color="rgba(255,255,255,0.35)", width=1))
    if curve is not None and len(curve[0]) and np.isfinite(np.asarray(curve[1], float)).any():
        prices, total = np.asarray(curve[0], float), np.asarray(curve[1], float)
        reach = float(np.nanmax(np.abs(total))) or 1.0
        fig.add_trace(go.Scatter(
            x=total, y=prices, mode="lines", name="Net GEX if price were here",
            line=dict(color=PROFILE_CURVE, width=2), xaxis="x2",
            hovertemplate="if at $%{y:.2f}: %{x:,.0f}<extra>whole book</extra>"))
        fig.update_layout(xaxis2=dict(overlaying="x", side="top", showgrid=False,
                                      zeroline=False, range=[-reach * 1.25, reach * 1.25],
                                      showticklabels=False))
    span = float(net.abs().max())
    # the bars' axis only: update_xaxes would also rescale the curve's own axis (x2)
    fig.layout.xaxis.update(range=[-span * 1.25, span * 1.25], showgrid=False, zeroline=False,
                            showticklabels=False)
    fig.update_yaxes(tickmode="array", tickvals=list(net.index),
                     ticktext=[f"{k:g}" for k in net.index], showgrid=False,
                     range=[float(net.index.min()) - step, float(net.index.max()) + step])
    return fig


def regime_line(*, spot: float, flip: float | None, net: float | None,
                flip_nearest: float | None = None, nearest_label: str = "") -> str:
    """One line on the regime. First the nearest expiry: when spot is within the Game
    Plan's unsettled band of that expiry's flip, say so (the same rule the Game Plan uses).
    Then all expiries: where spot sits against their flip, or the sign of their net when
    there is no flip within the sweep."""
    from game_plan import UNSETTLED_PCT

    lead = ""
    if flip_nearest is not None and spot and \
            abs(spot - flip_nearest) / spot * 100.0 <= UNSETTLED_PCT:
        lead = (f"Spot is within {UNSETTLED_PCT:g}% of the {nearest_label or 'nearest'} gamma "
                f"flip (\\${flip_nearest:,.2f}): unsettled, no regime read for that expiry. ")
    return lead + _all_expiries_line(spot=spot, flip=flip, net=net)


def _all_expiries_line(*, spot: float, flip: float | None, net: float | None) -> str:
    if flip is not None:
        side = "above" if spot >= flip else "below"
        effect = "dampen" if spot >= flip else "amplify"
        return (f"Spot is {side} the gamma flip (\\${flip:,.2f}, {abs(spot - flip) / spot:.1%} "
                f"away): dealer hedging tends to {effect} moves.")
    if net is None:
        return ""
    return ("Net positive across all expiries, no flip within 5%: dealer hedging tends to "
            "dampen moves." if net >= 0 else
            "Net negative across all expiries, no flip within 5%: dealer hedging tends to "
            "amplify moves.")


LAG_NOTE_MIN_PCT = 0.10          # say so when quotes sit this far (percent) from spot


def lag_note(quote_prices: dict, expiry: str, spot: float) -> str:
    """A line for the caption when the option quotes were made at a different stock price
    than the one shown (they lag the stock). Empty when they agree or it is unknown."""
    q = quote_prices.get(expiry)
    if q is None or not spot or abs(q - spot) / spot * 100.0 < LAG_NOTE_MIN_PCT:
        return ""
    return (f"⚠️ The option quotes behind this map were made with the stock at ${q:,.2f}; "
            f"the price shown is ${spot:,.2f}. Quotes lag the stock, so on a fast move the "
            "strikes nearest the price are the least certain.")


CURVE_POINTS = 121
CURVE_BAND_PCT = 0.04          # the curve is drawn within this share of spot


def profile_curve(table: pd.DataFrame, matrix: pd.DataFrame, *, as_of: datetime,
                  unit: str = gex.UNIT_DOLLAR, spot: float | None = None) -> tuple | None:
    """(prices, net) for the shown expiries over the shown strikes, kept within
    +/- CURVE_BAND_PCT of spot (far from spot the numbers swamp the part that matters)."""
    if matrix is None or matrix.empty:
        return None
    lo, hi = float(matrix.index.min()), float(matrix.index.max())
    if spot:
        lo = max(lo, float(spot) * (1 - CURVE_BAND_PCT))
        hi = min(hi, float(spot) * (1 + CURVE_BAND_PCT))
    if hi <= lo:
        return None
    prices = np.linspace(lo, hi, CURVE_POINTS)
    net = gex.net_gex_curve(table, as_of=as_of, spots=prices, unit=unit,
                            expiries=[str(e) for e in matrix.columns])
    return (prices, net)


def build_view(latest: pd.DataFrame, *, spot: float, as_of: datetime, today: date,
               unit: str = gex.UNIT_DOLLAR, iv_source: str = gex.IV_QUOTE,
               mode: str = EXP_WEEK, picked: list[str] | None = None,
               strikes: str = "16") -> dict:
    """Everything the page draws, computed without Streamlit: the per-contract table,
    the expiries on offer, the matrix for the chosen window, its walls and coverage."""
    table = gex.gex_table(latest, spot=spot, as_of=as_of, unit=unit, today=today,
                          iv_source=iv_source)
    available = gex.available_expiries(table)
    matrix = pd.DataFrame()
    if available:
        matrix = gex.gex_matrix(table, spot=spot,
                                expiries=pick_expiries(mode, available, picked),
                                n_strikes=None if strikes == "All" else int(strikes))
    first = [str(matrix.columns[0])] if not matrix.empty else []
    return {"table": table, "available": available, "matrix": matrix,
            "walls": gex.walls(matrix) if not matrix.empty else {},
            "coverage": gex.coverage(table),
            # every usable strike, not only the window: the first shown expiry, all expiries
            "net_all_strikes": gex.full_chain_net(table, first) if first else None,
            "net_full_chain": gex.full_chain_net(table),
            "flip": (gex.gamma_flip(table, spot=spot, as_of=as_of) or {}).get("flip")
            if available else None,
            # net GEX of the shown expiries as price moves across the shown strikes
            "curve": profile_curve(table, matrix, as_of=as_of, unit=unit, spot=spot),
            # the first shown expiry's own flip: the one the Game Plan reads
            "flip_nearest": (gex.gamma_flip(table, spot=spot, as_of=as_of, expiries=first)
                             or {}).get("flip") if first else None}


def render_gex_page(ticker: str, *, tz, spot: float | None, today: date | None = None) -> None:
    ticker = str(ticker or "").upper()
    head, settings = st.columns([4, 1])
    head.subheader(f"{ticker} gamma exposure")

    latest = vh.latest_scan(ticker)
    if latest.empty:
        st.info(f"No chain snapshot for {ticker} yet. It is recorded with the next scanner run.")
        return
    if not spot or spot <= 0:
        st.info("No spot price in the latest scan archive, so gamma cannot be computed.")
        return
    spot = float(spot)
    today = today or datetime.now(tz).date()
    as_of = datetime.fromisoformat(str(latest["ts_et"].iloc[0]))

    # ── panel settings (tucked away, like the reference view) ───────────────
    with settings.popover("Panel settings", use_container_width=True):
        layout = st.radio("Layout", [LAYOUT_HEAT, LAYOUT_PROFILE], horizontal=True,
                          key="gex_layout",
                          help="Heat: a table of strikes by expiry. Profile: bars by strike, "
                               "summed across the expiries shown.")
        unit_label = st.radio("Units", list(UNIT_LABELS), horizontal=True, key="gex_unit")
        iv_label = st.radio("Implied volatility", list(IV_LABELS), horizontal=True,
                            key="gex_iv_source",
                            help="'From quotes' solves IV from each contract's bid/ask mid; "
                                 "'Vendor' uses the IV the data source reports.")
        unit = UNIT_LABELS[unit_label]
        mode = st.radio("Expirations", [EXP_CURRENT, EXP_WEEK, EXP_ALL, EXP_PICK], index=1,
                        horizontal=True, key="gex_exp_mode")
        picked = None
        if mode == EXP_PICK:
            options = build_view(latest, spot=spot, as_of=as_of, today=today, unit=unit,
                                 iv_source=IV_LABELS[iv_label])["available"]
            picked = st.multiselect("Dates", options, default=options[:1],
                                    format_func=_fmt_exp, key="gex_exp_pick")
        strikes = st.radio("Strikes", STRIKE_CHOICES, index=1, horizontal=True,
                           key="gex_strikes")

    view = build_view(latest, spot=spot, as_of=as_of, today=today, unit=unit,
                      iv_source=IV_LABELS[iv_label], mode=mode, picked=picked, strikes=strikes)
    available, matrix, wall_map, cov = (view["available"], view["matrix"], view["walls"],
                                        view["coverage"])
    if not available:
        st.caption("No live expiry has usable open interest and IV in this snapshot.")
        return
    if matrix.empty:
        st.caption("No contract near spot has usable open interest and IV in this snapshot.")
        return

    # ── panel 1: the regime, for the nearest shown expiry ───────────────────
    first = str(matrix.columns[0])
    w = wall_map.get(first, {})
    net = w.get("net")
    sr = gex.support_resistance(matrix, spot).get(first, {})

    def _px(v):
        return "—" if v is None else f"${v:g}"

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Spot", f"${spot:,.2f}")
    m2.metric("Net · shown", gex.fmt_money(net) or "—",
              help=f"Net GEX of the {_fmt_exp(first)} strikes in the table only. Changing the strike count "
                   "can change its sign; the row below sums every strike.")
    m3.metric("Gamma support", _px(sr.get("support")),
              help="Largest positive strike at or below spot. Dips toward it tend to be bought.")
    m4.metric("Call resistance", _px(sr.get("resistance")),
              help="Largest positive strike above spot. Rallies toward it tend to stall.")
    m5.metric("Put wall", _px(w.get("put_wall")),
              help="Most negative strike. Moves can speed up around and below it.")
    flip, full_net, flip_near = view["flip"], view["net_full_chain"], view["flip_nearest"]
    n1, n2, n3, n4, _n5 = st.columns(5)
    n1.metric(f"Net · {_fmt_exp(first)}",
              gex.fmt_money(view["net_all_strikes"]) or "—",
              help="Net GEX of every usable strike of this expiry, not only the shown ones.")
    n2.metric(f"Flip · {_fmt_exp(first)}",
              _px(None if flip_near is None else round(flip_near, 2)),
              help="The price where this expiry's net GEX changes sign (every contract's "
                   "gamma re-priced within 5% of spot). The Game Plan reads this one. "
                   "'—' means no sign change within 5%.")
    n3.metric("Net · all exp.", gex.fmt_money(full_net) or "—",
              help="Every usable strike of every live expiry in the snapshot.")
    n4.metric("Flip · all exp.", _px(None if flip is None else round(flip, 2)),
              help="The same, for every live expiry together. '—' means the net keeps one "
                   "sign across the whole ±5% range.")
    st.caption(regime_line(spot=spot, flip=flip, net=full_net, flip_nearest=flip_near,
                           nearest_label=_fmt_exp(first)))

    # ── panel 2: the map ─────────────────────────────────────────────────────
    if layout == LAYOUT_PROFILE:
        shown = ", ".join(_fmt_exp(e) for e in matrix.columns)
        total = gex.fmt_money(profile_series(matrix).sum())
        st.plotly_chart(
            profile_figure(matrix, spot=spot, title=f"Net GEX · {shown} · net {total}",
                           flip=flip, curve=view.get("curve")),
            use_container_width=True, config={"displayModeBar": False},
        )
    else:
        styled = style_matrix(matrix, wall_map, spot_strike=gex.nearest_strike(matrix, spot),
                              net_label=NET_LABELS[unit])
        st.markdown(matrix_html(styled, [_fmt_exp(e) for e in matrix.columns]),
                    unsafe_allow_html=True)
    st.caption(
        f"Snapshot {as_of.astimezone(tz):%a %b %d %H:%M ET} · "
        f"{len(matrix.columns)} of {len(available)} expiries · {len(matrix)} strikes · "
        f"{cov['used']} of {cov['contracts']} contracts used · "
        + (f"IV from quotes on {cov['iv_from_quote']} of {cov['used']}"
           + (f" ({cov['iv_from_pair']} via the same-strike pair)" if cov["iv_from_pair"] else "")
           + " · " if cov["iv_from_quote"] else "")
        + UNIT_CAPTION[unit].replace("$", "\\$")
    )
    note = lag_note(view["table"].attrs.get("quote_price", {}), str(matrix.columns[0]), spot)
    if note:
        st.caption(note.replace("$", "\\$"))

    # ── panel 3: how to read it ──────────────────────────────────────────────
    with st.expander("How to read this, and its limits"):
        st.markdown(
            "- **Purple** strikes are call-heavy, **teal** are put-heavy. **Yellow** is the "
            "largest positive strike in each expiry (the call wall), **bright teal** the "
            "most negative (the put wall).\n"
            "- **Gamma support** is the largest positive strike at or below spot and "
            "**Call resistance** the largest above it. The call wall is one of the two; "
            "which side of spot it sits on says how it is likely to act.\n"
            "- **Gamma flip** is where net GEX across all expiries changes sign. Above "
            "it, dealer hedging tends to dampen moves; below it, to amplify them. It "
            "holds IV, time and open interest fixed and moves only the price.\n"
            "- **Net GEX · shown strikes** sums the table only, so it depends on the "
            "strike count; the **all strikes** and **all expiries** figures do not.\n"
            "- In **Profile**, the pale line is the whole book's net GEX if the stock were at "
            "each price (top scale; shown expiries only). Where it crosses zero is those "
            "expiries' flip; a steep line means behaviour changes quickly around it.\n"
            "- Large positive strikes tend to act as walls and magnets. "
            "Negative strikes are where moves can speed up.\n"
            "- The sign assumes dealers are long calls and short puts. That is a "
            "model assumption, not observed positioning.\n"
            "- Open interest updates once a day. Intraday changes here come from "
            "price and time, not new positions.\n"
            "- Only contracts that have traded today are in the snapshot, so the map "
            "is thin early in the session.\n"
            "- It shows where hedging pressure sits, not which way price will go."
        )
