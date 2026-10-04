"""Gamma page — net gamma exposure by strike and expiry. One question per panel.

Reads the latest full-chain snapshot from data/volume_history.db. Display only:
no scoring, no network, no writes.
"""

from __future__ import annotations

from datetime import date, datetime

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


def style_matrix(matrix: pd.DataFrame, wall_map: dict, *, spot_strike: float | None = None,
                 net_label: str = NET_LABEL):
    """Colour by sign and size, mark each expiry's walls, append a NET row.

    Returns a Styler whose row labels are the strikes (the one nearest spot is marked).
    """
    vmax = float(matrix.abs().max().max()) if not matrix.empty else 0.0
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
    return {"table": table, "available": available, "matrix": matrix,
            "walls": gex.walls(matrix) if not matrix.empty else {},
            "coverage": gex.coverage(table)}


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
    m2.metric(f"Net GEX · {_fmt_exp(first)}", gex.fmt_money(net) or "—")
    m3.metric("Gamma support", _px(sr.get("support")),
              help="Largest positive strike at or below spot. Dips toward it tend to be bought.")
    m4.metric("Call resistance", _px(sr.get("resistance")),
              help="Largest positive strike above spot. Rallies toward it tend to stall.")
    m5.metric("Put wall", _px(w.get("put_wall")),
              help="Most negative strike. Moves can speed up around and below it.")
    if net is not None:
        st.caption("Net positive: dealer hedging tends to dampen moves." if net >= 0
                   else "Net negative: dealer hedging tends to amplify moves.")

    # ── panel 2: the map ─────────────────────────────────────────────────────
    styled = style_matrix(matrix, wall_map, spot_strike=gex.nearest_strike(matrix, spot),
                          net_label=NET_LABELS[unit])
    st.markdown(matrix_html(styled, [_fmt_exp(e) for e in matrix.columns]),
                unsafe_allow_html=True)
    st.caption(
        f"Snapshot {as_of.astimezone(tz):%a %b %d %H:%M ET} · "
        f"{len(matrix.columns)} of {len(available)} expiries · {len(matrix)} strikes · "
        f"{cov['used']} of {cov['contracts']} contracts used · "
        + (f"IV from quotes on {cov['iv_from_quote']} of {cov['used']} · "
           if cov["iv_from_quote"] else "")
        + UNIT_CAPTION[unit].replace("$", "\\$")
    )

    # ── panel 3: how to read it ──────────────────────────────────────────────
    with st.expander("How to read this, and its limits"):
        st.markdown(
            "- **Purple** strikes are call-heavy, **teal** are put-heavy. **Yellow** is the "
            "largest positive strike in each expiry (the call wall), **bright teal** the "
            "most negative (the put wall).\n"
            "- **Gamma support** is the largest positive strike at or below spot and "
            "**Call resistance** the largest above it. The call wall is one of the two; "
            "which side of spot it sits on says how it is likely to act.\n"
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
