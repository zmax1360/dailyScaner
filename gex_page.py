"""Gamma page — net gamma exposure by strike and expiry. One question per panel.

Reads the latest full-chain snapshot from data/volume_history.db. Display only:
no scoring, no network, no writes.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

import gex
import volume_history as vh

POS_RGB = (147, 51, 234)     # purple — call-heavy (positive)
NEG_RGB = (13, 148, 136)     # teal   — put-heavy (negative)
WALL_CSS = "background-color: rgb(250, 204, 21); color: #111; font-weight: 700;"


def _cell_css(v, vmax: float) -> str:
    if pd.isna(v) or vmax <= 0:
        return ""
    r, g, b = POS_RGB if v >= 0 else NEG_RGB
    alpha = 0.12 + 0.78 * min(abs(float(v)) / vmax, 1.0)
    return f"background-color: rgba({r},{g},{b},{alpha:.2f}); color: #fff;"


def style_matrix(matrix: pd.DataFrame, wall_map: dict, *, spot_strike: float | None = None):
    """Colour by sign and size; the largest positive strike per expiry is highlighted.

    Returns a Styler whose row labels are the strikes (the one nearest spot is marked).
    """
    vmax = float(matrix.abs().max().max()) if not matrix.empty else 0.0
    labels = [f"{k:g}  ◀ spot" if k == spot_strike else f"{k:g}" for k in matrix.index]
    shown = matrix.copy()
    shown.index = labels
    css = pd.DataFrame("", index=labels, columns=matrix.columns)
    for exp in matrix.columns:
        cw = (wall_map.get(str(exp)) or {}).get("call_wall")
        for strike, label in zip(matrix.index, labels):
            css.loc[label, exp] = (
                WALL_CSS if cw is not None and strike == cw
                else _cell_css(matrix.loc[strike, exp], vmax)
            )
    return shown.style.apply(lambda _: css, axis=None).format(gex.fmt_money, na_rep="")


def render_gex_page(ticker: str, *, tz, spot: float | None) -> None:
    ticker = str(ticker or "").upper()
    st.subheader(f"{ticker} gamma exposure")

    latest = vh.latest_scan(ticker)
    if latest.empty:
        st.info(f"No chain snapshot for {ticker} yet. It is recorded with the next scanner run.")
        return
    if not spot or spot <= 0:
        st.info("No spot price in the latest scan archive, so gamma cannot be computed.")
        return

    as_of = datetime.fromisoformat(str(latest["ts_et"].iloc[0]))
    table = gex.gex_table(latest, spot=float(spot), as_of=as_of)
    cov = gex.coverage(table)

    c1, c2 = st.columns(2)
    n_exp = c1.radio("Expiries", [1, 2, 4, 6], index=2, horizontal=True, key="gex_n_exp")
    window = c2.radio("Strikes around spot", ["3%", "6%", "10%"], index=1, horizontal=True,
                      key="gex_window")
    matrix = gex.gex_matrix(table, spot=float(spot), window_pct=float(window[:-1]) / 100.0,
                            max_expiries=int(n_exp))
    if matrix.empty:
        st.caption("No contract near spot has usable open interest and IV in this snapshot.")
        return
    wall_map = gex.walls(matrix)

    # ── panel 1: the regime, for the nearest expiry ──────────────────────────
    first = str(matrix.columns[0])
    w = wall_map.get(first, {})
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spot", f"${float(spot):,.2f}")
    net = w.get("net")
    m2.metric(f"Net GEX · {first}", gex.fmt_money(net) or "—",
              "positive: moves dampened" if (net or 0) >= 0 else "negative: moves amplified",
              delta_color="off")
    m3.metric("Call wall", "—" if w.get("call_wall") is None else f"${w['call_wall']:g}")
    m4.metric("Put wall", "—" if w.get("put_wall") is None else f"${w['put_wall']:g}")

    # ── panel 2: the map ─────────────────────────────────────────────────────
    styled = style_matrix(matrix, wall_map,
                          spot_strike=gex.nearest_strike(matrix, float(spot)))
    st.dataframe(styled, use_container_width=True,
                 height=min(38 * (len(matrix) + 1) + 4, 900), key=f"gex_matrix_{ticker}")
    st.caption(
        f"Snapshot {as_of.astimezone(tz):%a %b %d %H:%M ET} · "
        f"{cov['used']} of {cov['contracts']} contracts used "
        f"({cov['excluded']} excluded: missing open interest or IV) · "
        "$ of dealer hedging per 1% move"
    )

    # ── panel 3: how to read it ──────────────────────────────────────────────
    with st.expander("How to read this, and its limits"):
        st.markdown(
            "- **Purple** strikes are call-heavy, **teal** are put-heavy. "
            "**Yellow** is the largest positive strike in each expiry.\n"
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
