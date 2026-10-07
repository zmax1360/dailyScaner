"""Scorecard page — what your actual trades did, from a broker activity export.

Reads the newest CSV in data/broker/ (not in version control). Display only.
"""

from __future__ import annotations

import os
import re

import pandas as pd
import streamlit as st

import trade_history as th
from scoring_pool import POOL_0DTE, POOL_1DTE
from ui.services import _SCANNER_DIR
from ui.widgets import centered_table_html

BROKER_DIR = os.path.join(_SCANNER_DIR, "data", "broker")
SECTIONS = [("pool", "Same-day or later expiry"), ("hold", "Holding time"),
            ("premium", "Premium paid per contract"), ("time", "Time of entry"),
            ("side", "Calls or puts")]
UP, DOWN = "#00C853", "#FF1744"


def latest_export(folder: str | None = None) -> str | None:
    """Newest .csv in the broker folder, or None."""
    folder = folder or BROKER_DIR
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".csv")]
    except OSError:
        return None
    return max(files, key=os.path.getmtime) if files else None


def save_upload(name: str, data: bytes, folder: str | None = None) -> str:
    """Store an uploaded export in the broker folder after checking it is one.

    The file is written under a temporary name, validated, then moved into place. An
    invalid file is removed and ValueError is raised."""
    folder = folder or BROKER_DIR
    os.makedirs(folder, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name or "")) or "export.csv"
    if not safe.lower().endswith(".csv"):
        safe += ".csv"
    tmp = os.path.join(folder, f".{safe}.part")
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        th.load_activities(tmp)
    except Exception as exc:
        os.remove(tmp)
        raise ValueError(f"That file is not a broker activity export ({exc}).") from exc
    dest = os.path.join(folder, safe)
    os.replace(tmp, dest)
    return dest


def _md(text: str) -> str:
    """Escape dollar signs so two amounts in one caption are not read as LaTeX."""
    return text.replace("$", "\\$")


def money(v, signed: bool = True) -> str:
    if v is None or pd.isna(v):
        return "—"
    return f"{'-' if v < 0 else '+' if signed and v > 0 else ''}${abs(v):,.2f}"


def format_breakdown(table: pd.DataFrame) -> pd.DataFrame:
    """A breakdown as text columns for display."""
    return pd.DataFrame({
        "": table["bucket"],
        "Trades": table["trades"].astype(int),
        "Result": table["pnl"].map(money),
        "Win rate": table["win_rate"].map(lambda x: f"{x:.0%}"),
        "Average win": table["avg_win"].map(lambda x: money(x, signed=False)),
        "Average loss": table["avg_loss"].map(lambda x: money(x, signed=False)),
    })


def format_payoff(trades: pd.DataFrame) -> pd.DataFrame:
    """Targets and stops as actually traded, for all trades and each expiry type."""
    rows = []
    for label, part in (("All trades", trades),
                        (POOL_0DTE, trades[trades["pool"] == POOL_0DTE]),
                        (POOL_1DTE, trades[trades["pool"] == POOL_1DTE])):
        p = th.payoff(part)
        if not p:
            continue
        rows.append({
            "": label,
            "Typical win": f"{p['median_win_pct']:+.0%}",
            "Typical loss": f"{p['median_loss_pct']:+.0%}",
            "Win rate": f"{p['win_rate']:.0%}",
            "Win rate to break even": f"{p['breakeven_win_rate']:.0%}",
            "Reward to risk": f"{p['reward_to_risk']:.2f}",
            "Reward to risk to break even": f"{p['reward_to_risk_needed']:.2f}",
            "Per trade": money(p["per_trade"]),
        })
    return pd.DataFrame(rows)


def format_recent(trades: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """The latest trades with what was paid, what it sold for, and the result in R."""
    r = th.recent(trades, n)
    return pd.DataFrame({
        "Contract": r["contract"],
        "Entered": r["entry"].dt.strftime("%b %d %H:%M"),
        "Contracts": r["qty"].astype(int),
        "Bought at": r["bought"].map(lambda v: f"${v:.2f}"),
        "Sold at": r["sold"].map(lambda v: f"${v:.2f}"),
        "Change": [f"{'+' if c > 0 else '-' if c < 0 else ''}${abs(c):.2f} ({p:+.0%})"
                   for c, p in zip(r["change"], r["change_pct"])],
        "Result": r["pnl"].map(money),
        "In R": r["r_multiple"].map(lambda v: "—" if pd.isna(v) else f"{v:+.1f}R"),
        "Held (min)": r["hold_min"].round(0).astype(int),
    })


def daily_figure(days: pd.DataFrame):
    import plotly.graph_objects as go

    fig = go.Figure(go.Bar(
        x=[str(d) for d in days["day"]], y=days["pnl"],
        marker_color=[UP if v > 0 else DOWN for v in days["pnl"]],
        customdata=days["trades"],
        hovertemplate="%{x}<br>%{y:+,.2f} USD · %{customdata} trades<extra></extra>",
    ))
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", height=260, showlegend=False,
                      margin=dict(l=10, r=10, t=10, b=10), bargap=0.25)
    fig.update_yaxes(tickprefix="$", gridcolor="rgba(255,255,255,0.06)")
    fig.update_xaxes(type="category", showgrid=False)
    return fig


def render(cfg: dict) -> None:
    head, tools = st.columns([4, 1])
    head.subheader("Scorecard")
    with tools.popover("Trade file", use_container_width=True):
        up = st.file_uploader("Broker activity export (.csv)", type=["csv"],
                              key="scorecard_upload")
        if up is not None and st.session_state.get("scorecard_saved") != (up.name, up.size):
            try:
                save_upload(up.name, up.getvalue())
                st.session_state["scorecard_saved"] = (up.name, up.size)
                st.success("Saved.")
            except ValueError as exc:
                st.error(str(exc))
        st.caption("Stored on this computer in data/broker/, which is not in version control.")

    path = latest_export()
    if path is None:
        st.info("No trade file yet. Export your activity from your broker as CSV, then add "
                "it with the Trade file button. The newest file is used.")
        return
    try:
        trades, notes = th.closed_trades(th.load_activities(path))
    except ValueError as exc:
        st.error(f"{os.path.basename(path)} could not be read: {exc}")
        return

    ticker = str(cfg.get("ticker") or "").upper()
    tickers = sorted(trades["underlying"].unique()) if not trades.empty else []
    scope = "All"
    if len(tickers) > 1 and ticker in tickers:
        scope = st.radio("Trades", ["All", ticker], horizontal=True, key="scorecard_scope")
    if scope != "All":
        trades = trades[trades["underlying"] == scope]

    s = th.summary(trades)
    if not s:
        st.info(f"{os.path.basename(path)} has no closed option trades.")
        return

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Realized result", money(s["pnl"]))
    m2.metric("Win rate", f"{s['win_rate']:.0%}")
    m3.metric("Average win / loss",
              f"{money(s['avg_win'], signed=False)} / {money(s['avg_loss'], signed=False)}")
    m4.metric("Closed trades", f"{s['trades']:,}", help=f"{s['contracts']:,} contracts over "
              f"{s['days']} trading days. Median holding time {s['median_hold_min']:.0f} minutes.")
    pf = "—" if s["profit_factor"] is None else f"{s['profit_factor']:.2f}"
    st.caption(f"{s['first']:%b %d} to {s['last']:%b %d, %Y} · {s['green_days']} days up, "
               f"{s['red_days']} days down · wins ÷ losses in dollars: {pf} "
               "(above 1.00 means profitable)")

    st.markdown("**Result by day**")
    st.plotly_chart(daily_figure(th.daily(trades)), use_container_width=True,
                    config={"displayModeBar": False})

    targets = format_payoff(trades)
    if not targets.empty:
        st.markdown("**Targets and stops, as you actually traded them**")
        st.markdown(centered_table_html(targets, "targets"), unsafe_allow_html=True)
        st.caption("Typical win and loss are the median change in the option's price, from "
                   "your entry to your exit. Reward to risk is the average win per contract "
                   "divided by the average loss per contract. \"To break even\" shows what "
                   "each would have to be, with everything else unchanged.")

    units = th.risk_units(trades)
    st.markdown("**Latest trades**")
    st.markdown(centered_table_html(format_recent(trades), "recent"), unsafe_allow_html=True)
    if units:
        st.caption(_md("1R is your average loss per contract: "
                       + " and ".join(f"{money(v, signed=False)} for {k}"
                                      for k, v in units.items())
                       + ". A result of +1.5R made one and a half times what you typically "
                       "lose. The export has fills only, not the stop you planned, so your "
                       "typical loss stands in for the risk."))

    for by, title in SECTIONS:
        table = th.breakdown(trades, by)
        if table.empty:
            continue
        st.markdown(f"**{title}**")
        st.markdown(centered_table_html(format_breakdown(table), by), unsafe_allow_html=True)

    st.markdown("**Largest losses**")
    w = th.worst(trades, 5)
    st.markdown(centered_table_html(pd.DataFrame({
        "Contract": w["symbol"],
        "Entered": w["entry"].dt.strftime("%b %d %H:%M"),
        "Held (min)": w["hold_min"].round(0).astype(int),
        "Contracts": w["qty"].astype(int),
        "Result": w["pnl"].map(money),
        "Expired": w["expired"].map({True: "yes", False: ""}),
    }), "worst"), unsafe_allow_html=True)

    extra = []
    if notes["open_contracts"]:
        extra.append(f"{notes['open_contracts']} contracts still open are not counted")
    if notes["unmatched_sells"]:
        extra.append(f"{notes['unmatched_sells']} contracts sold with no matching buy in the file")
    if notes["skipped_rows"]:
        extra.append(f"{notes['skipped_rows']} rows could not be valued in US dollars")
    st.caption(
        f"From {os.path.basename(path)}. Realized results in US dollars; buys and sells are "
        "matched first in, first out. Small buckets can change a lot with a few more trades."
        + (" " + "; ".join(extra) + "." if extra else "")
    )
