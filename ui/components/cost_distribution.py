"""Cost Distribution — 6-month volume profile by price, with overhead supply."""

from __future__ import annotations

import streamlit as st

from cost_distribution import calculate_cost_distribution, render_cost_distribution_chart
from ui.context import ScanContext

TITLE = "Cost Distribution"


@st.cache_data(ttl=3600)
def cached_cost_distribution(ticker: str, spot: float | None = None) -> dict:
    """Cached 6-month cost distribution / overhead supply profile (1 hr TTL)."""
    return calculate_cost_distribution(ticker, days=180, spot=spot)


def render(ctx: ScanContext, cost_info: dict | None = None) -> None:
    """Macro Cost Distribution & Overhead Supply: metrics + chart."""
    ticker, spot = ctx.ticker, ctx.spot
    st.markdown("### 📊 Macro Cost Distribution & Overhead Supply")
    st.caption(
        "6-month daily volume profile by Typical Price (H+L+C)/3 · "
        "teal = cost below spot (in profit) · orange = overhead supply"
    )

    info = cost_info or cached_cost_distribution(ticker, spot if spot > 0 else None)
    poc = info.get("Average_Cost_POC")
    prof = info.get("Profited_Shares_Pct")
    r90 = info.get("Cost_Range_90") or (None, None)
    r70 = info.get("Cost_Range_70") or (None, None)
    prices = info.get("price_bins") or []
    vols = info.get("volume_bins") or []

    if not prices or poc is None:
        st.info("Cost distribution unavailable — daily history fetch failed.")
        return

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Average Cost (POC)", f"${float(poc):.2f}")
    with m2:
        delta = None
        if prof is not None and float(prof) >= 95.0:
            delta = "near zero overhead"
        st.metric(
            "Profited Shares %",
            f"{float(prof):.1f}%" if prof is not None else "—",
            delta=delta,
        )
    with m3:
        if r90[0] is not None and r90[1] is not None:
            st.metric(
                "90% Cost Range",
                f"${float(r90[0]):.2f} – ${float(r90[1]):.2f}",
            )
        else:
            st.metric("90% Cost Range", "—")

    if r70[0] is not None and r70[1] is not None:
        st.caption(
            f"70% Cost Range: **\\${float(r70[0]):.2f} – \\${float(r70[1]):.2f}** · "
            f"{info.get('days', '—')} sessions · "
            f"total vol {int(info.get('total_volume') or 0):,}"
        )

    spot_px = float(info.get("spot") or spot or 0)
    fig = render_cost_distribution_chart(
        prices,
        vols,
        spot_price=spot_px,
        avg_cost=float(poc),
        range_70=r70 if r70[0] is not None else None,
        range_90=r90 if r90[0] is not None else None,
        ticker=ticker,
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
