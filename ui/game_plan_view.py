"""Draws a game plan (game_plan.Plan). No calculation happens here."""

from __future__ import annotations

import html

import streamlit as st

import game_plan as gp
from ui.common import ET

SIDE_STYLE = {gp.CALLS: ("🟢", "rgb(34, 197, 94)"), gp.PUTS: ("🔴", "rgb(239, 68, 68)"),
              gp.STAND_ASIDE: ("⛔", "rgb(250, 204, 21)"), gp.UNKNOWN: ("ℹ️", "#9e9e9e")}
KIND_COLOR = {"resistance": "#C084FC", "support": "#FACC15", "put_wall": "#2DD4BF",
              "flip": "#5EEAD4", "vwap": "#00E5FF", "range": "#90A4AE", "spot": "#FFFFFF"}


def _md(text: str) -> str:
    """Escape dollar signs so two amounts on a line are not read as LaTeX math."""
    return text.replace("$", "\\$")


def ladder_html(plan: gp.Plan) -> str:
    """Levels from highest to lowest with spot among them, and the distance from spot."""
    rows = []
    for lv in plan.levels:
        color = KIND_COLOR.get(lv.kind, "#e0e0e0")
        if lv.kind == "spot" or plan.spot is None:
            dist = ""
        else:
            diff = lv.price - plan.spot
            dist = f"{diff:+.2f} ({diff / plan.spot * 100:+.1f}%)"
        weight = "700" if lv.kind == "spot" else "500"
        bg = "background:rgba(255,255,255,0.08);" if lv.kind == "spot" else ""
        rows.append(
            f'<tr style="{bg}">'
            f'<td style="text-align:left;color:{color};font-weight:{weight};">'
            f'{html.escape(lv.name)}</td>'
            f'<td style="font-weight:{weight};">&#36;{lv.price:,.2f}</td>'
            f'<td style="color:#90A4AE;">{dist}</td></tr>'
        )
    return (
        '<table style="width:100%;border-collapse:collapse;font-size:0.9rem;'
        'font-variant-numeric:tabular-nums;">'
        '<thead><tr style="color:#b0bec5;"><th style="text-align:left;">Level</th>'
        '<th style="text-align:center;">Price</th>'
        '<th style="text-align:center;">From spot</th></tr></thead><tbody style="text-align:center;">'
        + "".join(rows) + "</tbody></table>"
    )


def render(plan: gp.Plan) -> None:
    st.subheader(f"{plan.ticker} game plan")

    icon, _ = SIDE_STYLE[plan.side]
    c1, c2, c3 = st.columns(3)
    c1.metric("Allowed side", f"{icon} {gp.SIDE_LABEL[plan.side]}",
              help="From the 15-minute EMA 9/21/50 trend rule. " + plan.side_reason)
    c2.metric("Type of day", gp.REGIME_LABEL[plan.regime],
              help=plan.regime_note + (f" Net gamma for {plan.gamma_expiry}."
                                       if plan.gamma_expiry else ""))
    c3.metric("Candidate", _md(gp.candidate_text(plan.candidate)).split(" · ")[0],
              help=plan.candidate_note)
    if plan.candidate:
        st.caption(_md("Candidate: " + gp.candidate_text(plan.candidate)
                       + " — " + plan.candidate_note))
    else:
        st.caption(plan.candidate_note)

    st.markdown("**What to expect**")
    st.markdown("\n".join(f"- {_md(t)}" for t in plan.expect))

    if len(plan.levels) > 1:
        st.markdown("**Levels**")
        st.markdown(ladder_html(plan), unsafe_allow_html=True)

    if plan.changes:
        st.markdown("**What changes the plan**")
        st.markdown("\n".join(f"- {_md(t)}" for t in plan.changes))

    when = f"scan {plan.as_of.astimezone(ET):%a %b %d %H:%M ET}" if plan.as_of else "scan time unknown"
    missing = f" · not available: {', '.join(plan.missing)}" if plan.missing else ""
    st.caption(f"Built by fixed rules from the {when}{missing}. "
               "It describes where things stand, not where price will go.")
