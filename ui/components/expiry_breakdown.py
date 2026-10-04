"""Expiration Breakdown — call/put volume and P/C bias across the expiry curve."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.context import ScanContext

TITLE = "Expiration Breakdown"


def build_expiry_table(
    vol: dict, prev_vol: dict | None, overall_pc: float
) -> tuple[list[dict], dict[str, float]]:
    """
    Group top_calls + top_puts by expiry, compute P/C and Δ vs previous run.
    Returns (rows_for_table, {expiry: pc_ratio}) for the bar chart.
    """
    curr_exp: dict[str, dict] = {}
    for side_key, vol_key in [("call_vol", "top_calls"), ("put_vol", "top_puts")]:
        for c in (vol.get(vol_key) or []):
            exp = c.get("expiry", "?")
            dte = int(c.get("dte", 0))
            v   = int(c.get("volume") or 0)
            if exp not in curr_exp:
                curr_exp[exp] = {"dte": dte, "call_vol": 0, "put_vol": 0}
            curr_exp[exp][side_key] += v

    prev_exp: dict[str, dict] = {}
    if prev_vol:
        for side_key, vol_key in [("call_vol", "top_calls"), ("put_vol", "top_puts")]:
            for c in (prev_vol.get(vol_key) or []):
                exp = c.get("expiry", "?")
                v   = int(c.get("volume") or 0)
                prev_exp.setdefault(exp, {"call_vol": 0, "put_vol": 0})[side_key] += v

    rows, chart_pc = [], {}
    for exp in sorted(curr_exp):
        d  = curr_exp[exp]
        cv, pv  = d["call_vol"], d["put_vol"]
        dte     = d["dte"]
        data_gap = (cv == 0 or pv == 0)
        pc       = (pv / cv) if not data_gap else None
        chart_pc[exp] = pc   # None means data gap — caller must handle

        if pc is None:
            bias = ""
        elif pc < 0.7:   bias = "▲ BULLISH"
        elif pc < 0.9: bias = "▲ MILD BULLISH"
        elif pc < 1.1: bias = "— NEUTRAL"
        elif pc < 1.5: bias = "▼ MILD BEARISH"
        else:          bias = "▼ BEARISH"

        notable = (
            abs(pc - overall_pc) > 0.25
            if (pc is not None and overall_pc > 0)
            else False
        )

        pd_   = prev_exp.get(exp, {})
        cv_d  = cv - pd_.get("call_vol", 0) if prev_vol else None
        pv_d  = pv - pd_.get("put_vol",  0) if prev_vol else None

        def _ds(v):
            if v is None: return "·0"
            if v > 0: return f"▲+{v:,}"
            if v < 0: return f"▼{v:,}"
            return "·0"

        rows.append({
            "EXPIRY":   exp,
            "DTE":      f"{dte}d",
            "CALL VOL": f"{cv:,}",
            "PUT VOL":  f"{pv:,}",
            "P/C":      f"{pc:.2f}" if pc is not None else "n/a",
            "BIAS":     bias,
            "NOTABLE":  "⚠ data gap" if data_gap else ("◄ notable" if notable else ""),
            "CALL Δ":   _ds(cv_d),
            "PUT Δ":    _ds(pv_d),
        })

    return rows, chart_pc


def render_pc_term_chart(chart_pc: dict[str, float | None]) -> None:
    """
    Altair bar chart — P/C ratio per expiry.
    - Bars blue when < 1 (call-heavy), red when ≥ 1 (put-heavy).
    - Dashed reference line at y = 1.0.
    - ⚠ text marker above any expiry whose P/C is None (data gap).
    - Expiries with either side's volume = 0 are rendered as gap markers only.
    Data comes exclusively from chart_pc (already aggregated from archive).
    All ET-aware dates are preserved as-is from the archive expiry strings.
    """
    import altair as alt

    valid = {exp: pc for exp, pc in chart_pc.items() if pc is not None}
    gaps  = [exp for exp, pc in chart_pc.items() if pc is None]

    layers = []

    if valid:
        bar_df = pd.DataFrame([
            {"expiry": exp, "pc": pc, "side": "Put-heavy" if pc >= 1 else "Call-heavy"}
            for exp, pc in sorted(valid.items())
        ])
        bars = (
            alt.Chart(bar_df)
            .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
            .encode(
                x=alt.X("expiry:O",
                         sort=sorted(valid.keys()),
                         axis=alt.Axis(labelAngle=-40, title=None)),
                y=alt.Y("pc:Q",
                         title="P/C ratio",
                         scale=alt.Scale(domainMin=0)),
                color=alt.Color(
                    "side:N",
                    scale=alt.Scale(
                        domain=["Call-heavy", "Put-heavy"],
                        range=["#1565c0", "#c62828"],
                    ),
                    legend=alt.Legend(title=None, orient="top-right"),
                ),
                tooltip=[
                    alt.Tooltip("expiry:O", title="Expiry"),
                    alt.Tooltip("pc:Q", format=".3f", title="P/C"),
                    alt.Tooltip("side:N", title="Bias"),
                ],
            )
        )
        layers.append(bars)

    # Dashed reference line at y = 1.0
    rule = (
        alt.Chart(pd.DataFrame({"y": [1.0]}))
        .mark_rule(strokeDash=[6, 3], color="#888", strokeWidth=1.5)
        .encode(y="y:Q")
    )
    layers.append(rule)

    # ⚠ gap markers
    if gaps:
        gap_df = pd.DataFrame({"expiry": sorted(gaps), "label": ["⚠"] * len(gaps), "y": [0.05] * len(gaps)})
        gap_marks = (
            alt.Chart(gap_df)
            .mark_text(fontSize=14, color="#ff6d00", dy=-6)
            .encode(
                x=alt.X("expiry:O", sort=sorted(chart_pc.keys())),
                y=alt.Y("y:Q"),
                text=alt.Text("label:N"),
                tooltip=[alt.Tooltip("expiry:O", title="Data gap — zero volume on one side")],
            )
        )
        layers.append(gap_marks)

    if layers:
        chart = alt.layer(*layers).properties(height=200)
        st.altair_chart(chart, use_container_width=True)
        gap_note = f"  ·  ⚠ = data gap: {', '.join(sorted(gaps))}" if gaps else ""
        st.caption(f"< 1 call-heavy · > 1 put-heavy{gap_note}")


def render(ctx: ScanContext) -> None:
    st.markdown("### Volume by Expiry — P/C term structure")
    st.caption("Institutional-style skew across the expiry curve")
    exp_rows, chart_pc = build_expiry_table(ctx.vol, ctx.prev_vol, ctx.pc_ratio)
    if exp_rows:
        exp_df = pd.DataFrame(exp_rows)

        def _bias_style(val: str) -> str:
            if "BULL" in str(val):
                return "color:#00c853;font-weight:bold"
            if "BEAR" in str(val):
                return "color:#d50000;font-weight:bold"
            return "color:#9e9e9e"

        def _delta_style(val: str) -> str:
            s = str(val)
            if s.startswith("▲"):
                return "color:#00c853"
            if s.startswith("▼"):
                return "color:#d50000"
            return "color:#666"

        styled_exp = (
            exp_df.style
            .map(_bias_style, subset=["BIAS"])
            .map(_delta_style, subset=["CALL Δ", "PUT Δ"])
        )
        st.dataframe(styled_exp, use_container_width=True, hide_index=True)
        render_pc_term_chart(chart_pc)
    else:
        st.caption("No expiry data available")
