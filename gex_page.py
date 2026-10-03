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
CALL_WALL_CSS = "background-color: rgb(250, 204, 21); color: #111; font-weight: 700;"
PUT_WALL_CSS = "background-color: rgb(45, 212, 191); color: #111; font-weight: 700;"
NET_CSS = "font-weight: 700; border-top: 2px solid #666;"
NET_LABEL = "NET $"

EXP_CURRENT, EXP_WEEK, EXP_ALL, EXP_PICK = "Current", "Current week", "All", "Pick dates"
STRIKE_CHOICES = ["8", "16", "32", "64", "All"]
UNIT_LABELS = {"Per $1 move": gex.UNIT_DOLLAR, "Per 1% move": gex.UNIT_PCT}


def _cell_css(v, vmax: float) -> str:
    if pd.isna(v) or vmax <= 0:
        return ""
    r, g, b = POS_RGB if v >= 0 else NEG_RGB
    alpha = 0.12 + 0.78 * min(abs(float(v)) / vmax, 1.0)
    return f"background-color: rgba({r},{g},{b},{alpha:.2f}); color: #fff;"


def style_matrix(matrix: pd.DataFrame, wall_map: dict, *, spot_strike: float | None = None):
    """Colour by sign and size, mark each expiry's walls, append a NET row.

    Returns a Styler whose row labels are the strikes (the one nearest spot is marked).
    """
    vmax = float(matrix.abs().max().max()) if not matrix.empty else 0.0
    labels = [f"{k:g}  ◀ spot" if k == spot_strike else f"{k:g}" for k in matrix.index]
    shown = matrix.copy()
    shown.index = labels
    shown.loc[NET_LABEL] = [
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
        css.loc[NET_LABEL, exp] = NET_CSS
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
        table = gex.gex_table(latest, spot=spot, as_of=as_of, unit=UNIT_LABELS[unit_label],
                              today=today)
        available = gex.available_expiries(table)
        mode = st.radio("Expirations", [EXP_CURRENT, EXP_WEEK, EXP_ALL, EXP_PICK], index=1,
                        horizontal=True, key="gex_exp_mode")
        picked = None
        if mode == EXP_PICK:
            picked = st.multiselect("Dates", available, default=available[:1],
                                    format_func=_fmt_exp, key="gex_exp_pick")
        strikes = st.radio("Strikes", STRIKE_CHOICES, index=1, horizontal=True,
                           key="gex_strikes")

    if not available:
        st.caption("No live expiry has usable open interest and IV in this snapshot.")
        return
    expiries = pick_expiries(mode, available, picked)
    matrix = gex.gex_matrix(table, spot=spot, expiries=expiries,
                            n_strikes=None if strikes == "All" else int(strikes))
    if matrix.empty:
        st.caption("No contract near spot has usable open interest and IV in this snapshot.")
        return
    wall_map = gex.walls(matrix)
    cov = gex.coverage(table)

    # ── panel 1: the regime, for the nearest shown expiry ───────────────────
    first = str(matrix.columns[0])
    w = wall_map.get(first, {})
    net = w.get("net")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spot", f"${spot:,.2f}")
    m2.metric(f"Net GEX · {_fmt_exp(first)}", gex.fmt_money(net) or "—")
    m3.metric(f"Call wall · {_fmt_exp(first)}",
              "—" if w.get("call_wall") is None else f"${w['call_wall']:g}")
    m4.metric(f"Put wall · {_fmt_exp(first)}",
              "—" if w.get("put_wall") is None else f"${w['put_wall']:g}")
    if net is not None:
        st.caption("Net positive: dealer hedging tends to dampen moves." if net >= 0
                   else "Net negative: dealer hedging tends to amplify moves.")

    # ── panel 2: the map ─────────────────────────────────────────────────────
    styled = style_matrix(matrix, wall_map, spot_strike=gex.nearest_strike(matrix, spot))
    st.dataframe(
        styled, use_container_width=True, key=f"gex_matrix_{ticker}",
        height=min(36 * (len(matrix) + 2) + 4, 1200),
        column_config={e: st.column_config.Column(_fmt_exp(e)) for e in matrix.columns},
    )
    st.caption(
        f"Snapshot {as_of.astimezone(tz):%a %b %d %H:%M ET} · "
        f"{len(matrix.columns)} of {len(available)} expiries · {len(matrix)} strikes · "
        f"{cov['used']} of {cov['contracts']} contracts used · "
        f"$ of dealer hedging {unit_label.lower()}"
    )

    # ── panel 3: how to read it ──────────────────────────────────────────────
    with st.expander("How to read this, and its limits"):
        st.markdown(
            "- **Purple** strikes are call-heavy, **teal** are put-heavy. **Yellow** is the "
            "largest positive strike in each expiry, **bright teal** the most negative.\n"
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
