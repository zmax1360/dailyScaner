"""Flow Magnets — the highest-volume calls and puts, with a Vol/OI heat column."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from ui.context import ScanContext

TITLE = "Flow Magnets"


def voi_style(val: str) -> str:
    """Pandas Styler cell function — heat-gradient background for VOL/OI column."""
    try:
        v = float(str(val).replace("x", "").replace("🔥", "").strip())
    except (ValueError, AttributeError):
        return ""
    if v >= 100: return "background-color:#7f0000;color:#fff;font-weight:bold"
    if v >= 50:  return "background-color:#b71c1c;color:#fff;font-weight:bold"
    if v >= 20:  return "background-color:#d50000;color:#fff;font-weight:bold"
    if v >= 10:  return "background-color:#e65100;color:#fff;font-weight:bold"
    if v >= 5:   return "background-color:#ff6d00;color:#fff;font-weight:bold"
    if v >= 2:   return "background-color:#ffa726;color:#000;font-weight:bold"
    return ""


def contracts_table(contracts: list, n: int = 5) -> pd.DataFrame | None:
    """Build a styled DataFrame from a top_calls / top_puts list."""
    rows = []
    for c in contracts[:n]:
        v   = int(c.get("volume") or 0)
        oi  = max(int(c.get("openInterest") or 0), 1)
        voi = v / oi
        rows.append({
            "EXPIRY":  c.get("expiry", ""),
            "STRIKE":  f"${float(c.get('strike', 0)):.1f}",
            "PRICE":   f"${float(c.get('lastPrice') or 0):.2f}",
            "VOLUME":  f"{v:,}",
            "OI":      f"{oi:,}",
            "VOL/OI":  f"{voi:.2f}x 🔥" if voi >= 2 else f"{voi:.2f}x",
            "_voi":    voi,
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    styled = df.drop(columns=["_voi"]).style.map(voi_style, subset=["VOL/OI"])
    return styled


def render(ctx: ScanContext) -> None:
    top_n = int(ctx.top_n)
    vol = ctx.vol
    st.markdown(f"### The Magnets — Top {top_n} Calls / Top {top_n} Puts")
    st.caption("🔥 Vol/OI heatmap — values ≥ 2.0x glow hot (unusual vs open interest)")
    call_col, put_col = st.columns(2, gap="small")
    for col, key, label in [
        (call_col, "top_calls", f"🟢 Top {top_n} CALLS"),
        (put_col,  "top_puts",  f"🔴 Top {top_n} PUTS"),
    ]:
        contracts = vol.get(key) or []
        with col:
            st.markdown(f"**{label}**")
            styled = contracts_table(contracts, n=top_n)
            if styled is not None:
                st.dataframe(styled, use_container_width=True, hide_index=True)
            else:
                st.caption("No data")
