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


def trade_html(trade: gp.TradePlan) -> str:
    """Entry, stop and targets as rows, with the distance from the current price."""
    def row(label, price, detail, color="#e0e0e0", bold=False):
        w = "700" if bold else "500"
        return (f'<tr><td style="text-align:left;color:{color};font-weight:{w};">'
                f'{html.escape(label)}</td>'
                f'<td style="font-weight:{w};">&#36;{price:,.2f}</td>'
                f'<td style="color:#90A4AE;text-align:left;">'
                f'{html.escape(detail).replace("$", "&#36;")}</td></tr>')

    rows = []
    if trade.entry_limit is not None and trade.better_entry is not None:
        calls = trade.side == gp.CALLS
        rows.append(row(
            f"Enter at or {'below' if calls else 'above'}", trade.entry_limit,
            f"Entry zone runs from {trade.better_entry.name} at "
            f"${trade.better_entry.price:,.2f} to here. Past this price the reward to "
            f"target 1 is less than {trade.min_reward_to_risk:g} times the risk.",
            "rgb(96, 165, 250)", True))
    rows.append(row("Current price", trade.entry,
                    "Inside the entry zone." if trade.in_zone else
                    "Outside the entry zone: wait." if trade.entry_limit is not None else ""))
    if trade.stop is not None:
        risk = f" Risk from here {trade.risk:.2f}." if trade.risk is not None else ""
        rows.append(row("Stop", trade.stop, trade.stop_note + risk, "rgb(248, 113, 113)", True))
    for i, t in enumerate(trade.targets, start=1):
        rows.append(row(f"Target {i}", t.price,
                        f"{t.name}. Close {t.share:.0%}. Reward from here {t.reward:.2f}.",
                        "rgb(74, 222, 128)", True))
    return (
        '<table style="width:100%;border-collapse:collapse;font-size:0.9rem;'
        'font-variant-numeric:tabular-nums;">'
        '<thead><tr style="color:#b0bec5;"><th style="text-align:left;">Step</th>'
        '<th style="text-align:center;">Stock price</th>'
        '<th style="text-align:left;">Rule</th></tr></thead><tbody style="text-align:center;">'
        + "".join(rows) + "</tbody></table>"
    )


def render_trade(trade: gp.TradePlan | None) -> None:
    if trade is None:
        return
    word = "calls" if trade.side == gp.CALLS else "puts"
    st.markdown(f"**Trade plan · {word}**")
    if not trade.ready:
        st.info(_md(trade.notes[0]))
    else:
        if trade.poor:
            st.warning(_md(trade.notes[0]), icon="⚠️")
        st.markdown(trade_html(trade), unsafe_allow_html=True)
        if trade.reward_to_risk is not None:
            st.caption(f"Reward to risk to target 1, entering at the current price: "
                       f"**{trade.reward_to_risk:.1f}** (the plan needs "
                       f"{trade.min_reward_to_risk:g})")
        st.caption(_md("Scale-out applies with two or more contracts. "
                       + trade.single_contract + " Time exit: " + trade.time_exit))
    st.caption(_md(trade.other_side) + " Prices are for the stock, not the option premium.")


def render(plan: gp.Plan) -> None:
    st.subheader(f"{plan.ticker} game plan")

    icon, _ = SIDE_STYLE[plan.side]
    cols = st.columns(2 + len(plan.candidates))
    cols[0].metric("Allowed side", f"{icon} {gp.SIDE_LABEL[plan.side]}",
                   help="From the 15-minute EMA 9/21/50 trend rule. " + plan.side_reason)
    cols[1].metric("Type of day", gp.REGIME_LABEL[plan.regime],
                   help=plan.regime_note + (f" Net gamma for {plan.gamma_expiry}."
                                            if plan.gamma_expiry else ""))
    # Metric values are shown as plain text, so no Markdown escaping here.
    for col, cand in zip(cols[2:], plan.candidates):
        col.metric(f"{cand.pool} candidate", gp.candidate_text(cand.pick).split(" · ")[0],
                   help=cand.note)
    for cand in plan.candidates:
        if cand.pick:
            st.caption(_md(f"{cand.pool}: {gp.candidate_text(cand.pick)} — {cand.note}"))
        else:
            st.caption(f"{cand.pool}: {cand.note}")

    st.markdown("**What to expect**")
    st.markdown("\n".join(f"- {_md(t)}" for t in plan.expect))

    render_trade(plan.trade)

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
