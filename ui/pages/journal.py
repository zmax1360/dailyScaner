"""Journal page — the trade log."""

from __future__ import annotations

import json
import os

from scanner.journal_io import journal_path_for_day
from scanner.journal_io import list_journal_days
from scanner.journal_io import load_journal_day
from scanner.journal_view import closed_on_day
from scanner.journal_view import concat_all_fills
from scanner.journal_view import day_fill_counts
from scanner.journal_view import load_all_day_frames
from scanner.journal_view import metrics_from_match
from scanner.journal_view import positions_frame
from scanner.journal_view import today_et as journal_today_et
from scanner.journal_view import try_match
import pandas as pd
import streamlit as st


def _fmt_journal_money(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    try:
        return f"${float(x):+,.2f}"
    except Exception:
        return "—"


def _fmt_journal_pct(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    try:
        return f"{float(x):+.1%}"
    except Exception:
        return "—"


def _fmt_journal_ts(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)) or str(x).strip() in ("", "nan", "None"):
        return "—"
    s = str(x).strip()
    if "T" in s:
        return s.replace("T", " ")[:16]
    return s[:16]


def render() -> None:
    """Trade journal — bought / sold options with performance tracking."""
    st.markdown("### Trade Journal")
    st.caption(
        "Tracks options you **buy** (＋ on Best Value) and **sell** (− close). "
        "Each day is saved to `data/journal/YYYY-MM-DD.json`. "
        "P&L is FIFO from fill prices — stored PnL columns are ignored."
    )

    day_frames = load_all_day_frames()
    all_fills = concat_all_fills(day_frames)
    closed_all, open_all, global_err = try_match(all_fills)
    if global_err:
        st.error(f"Journal FIFO: {global_err}")
    stats = metrics_from_match(closed_all, open_all)
    journal = positions_frame(closed_all, open_all)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Closed trades", stats["n_closed"])
    m2.metric(
        "Win rate",
        "—" if stats["win_rate"] is None else f"{stats['win_rate']:.0%}",
        help=f"{stats['wins']} wins / {stats['losses']} losses",
    )
    m3.metric(
        "Realized PnL",
        f"${stats['total_realized_pnl']:+,.2f}",
    )
    m4.metric(
        "Avg PnL %",
        "—" if stats["avg_pnl_pct"] is None else f"{stats['avg_pnl_pct']:+.1%}",
    )
    m5.metric(
        "Open / unrealized",
        f"{stats['n_open']} · ${stats['unrealized_pnl']:+,.2f}",
    )
    if not open_all.empty:
        st.warning(
            f"{len(open_all)} unmatched BUY lot(s) across the journal — "
            "phantom fill until a SELL matches."
        )

    st.markdown("#### Daily record")
    days = list_journal_days()
    today = journal_today_et()
    day_options = days if days else [today]
    if today not in day_options:
        day_options = [today] + day_options

    d1, d2 = st.columns([2, 3])
    with d1:
        selected_day = st.selectbox(
            "Day",
            day_options,
            index=0,
            key="journal_day_filter",
            help="One JSON file per ET calendar day",
        )
    day_df = (
        day_frames[selected_day]
        if selected_day in day_frames
        else load_journal_day(selected_day)
    )
    n_buys, n_sells = day_fill_counts(day_df)
    day_closed = closed_on_day(closed_all, selected_day)
    day_pnl = (
        float(day_closed["PnL_Dollars"].sum()) if not day_closed.empty else 0.0
    )
    day_pnl_s = f"${day_pnl:+,.2f}"
    day_file = journal_path_for_day(selected_day)
    with d2:
        st.caption(
            f"File: `{os.path.relpath(day_file, os.path.dirname(os.path.abspath(__file__)))}` "
            f"· buys {n_buys} · sells {n_sells} · "
            f"day PnL {day_pnl_s}"
        )

    if day_df.empty:
        st.info(f"No buy/sell events saved for {selected_day} yet.")
    else:
        st.caption("Fills (as logged). P&L is derived below — not stored on the row.")
        fill_show = day_df.copy()
        fill_show["When"] = fill_show["At"].map(_fmt_journal_ts)
        fill_show["Price $"] = fill_show["Price"].map(
            lambda x: f"${float(x):.2f}" if pd.notna(x) else "—"
        )
        fill_show["Strike"] = fill_show["Strike"].map(
            lambda x: f"${float(x):.1f}" if pd.notna(x) else "—"
        )
        fill_show["Qty"] = fill_show["Quantity"].map(
            lambda x: f"{float(x):.0f}" if pd.notna(x) else "—"
        )
        fill_cols = [
            c for c in (
                "Action", "Ticker", "Side", "Strike", "Expiry", "Qty",
                "When", "Price $", "Source",
            ) if c in fill_show.columns
        ]
        st.dataframe(
            fill_show[fill_cols],
            use_container_width=True,
            hide_index=True,
            height=min(240, 48 + 36 * len(fill_show)),
        )

        lots = day_closed
        st.markdown("##### Closed lots (FIFO)")
        if lots.empty:
            st.caption("No matched lots yet.")
        else:
            lot_show = lots.copy()
            lot_show["Entry"] = lot_show["Entry_Price"].map(
                lambda x: f"${float(x):.2f}" if pd.notna(x) else "—"
            )
            lot_show["Exit"] = lot_show["Exit_Price"].map(
                lambda x: f"${float(x):.2f}" if pd.notna(x) else "—"
            )
            lot_show["Strike"] = lot_show["Strike"].map(
                lambda x: f"${float(x):.1f}" if pd.notna(x) else "—"
            )
            lot_show["Qty"] = lot_show["Quantity"].map(
                lambda x: f"{float(x):.0f}" if pd.notna(x) else "—"
            )
            lot_show["PnL %"] = lot_show["PnL_Pct"].map(_fmt_journal_pct)
            lot_show["PnL $"] = lot_show["PnL_Dollars"].map(_fmt_journal_money)
            lot_show["When"] = lot_show["Exit_At"].map(_fmt_journal_ts)
            st.dataframe(
                lot_show[
                    ["Ticker", "Side", "Strike", "Expiry", "Qty",
                     "Entry", "Exit", "When", "PnL %", "PnL $"]
                ],
                use_container_width=True,
                hide_index=True,
                height=min(280, 48 + 36 * len(lot_show)),
            )

        st.download_button(
            f"Download {selected_day} JSON",
            data=json.dumps(
                day_df.to_dict(orient="records"),
                indent=2,
            ),
            file_name=f"journal_{selected_day}.json",
            mime="application/json",
            key="journal_day_json_dl",
        )

    st.markdown("#### All positions (open + closed)")
    tickers = sorted(
        {t for t in journal["Ticker"].astype(str).tolist() if t and t != "nan"}
    ) if not journal.empty else []
    f1, f2 = st.columns(2)
    with f1:
        status_filter = st.selectbox(
            "Status",
            ["All", "OPEN", "CLOSED"],
            index=0,
            key="journal_status_filter",
        )
    with f2:
        ticker_filter = st.selectbox(
            "Ticker",
            ["All"] + tickers,
            index=0,
            key="journal_ticker_filter",
        )

    view = journal.copy()
    if status_filter != "All" and not view.empty:
        view = view[view["Status"] == status_filter]
    if ticker_filter != "All" and not view.empty:
        view = view[view["Ticker"] == ticker_filter]

    if view.empty:
        st.info(
            "No journal entries yet. Use **＋** on a Best Value row to log a buy, "
            "then **−** on My Open Positions to log the sell."
        )
        return

    show = view.copy()
    show["Bought"] = show["Bought_At"].map(_fmt_journal_ts)
    show["Bought $"] = show["Bought_Price"].map(
        lambda x: f"${float(x):.2f}" if pd.notna(x) else "—"
    )
    show["Sold"] = show["Sold_At"].map(_fmt_journal_ts)
    show["Sold $"] = show["Sold_Price"].map(
        lambda x: f"${float(x):.2f}" if pd.notna(x) else "—"
    )
    show["PnL %"] = show.apply(
        lambda r: _fmt_journal_pct(r["PnL_Pct"] if r["Status"] == "CLOSED" else None),
        axis=1,
    )
    show["PnL $"] = show.apply(
        lambda r: _fmt_journal_money(
            r["PnL_Dollars"] if r["Status"] == "CLOSED" else None
        ),
        axis=1,
    )
    show["Strike"] = show["Strike"].map(
        lambda x: f"${float(x):.1f}" if pd.notna(x) else "—"
    )
    show["Qty"] = show["Quantity"].map(
        lambda x: f"{float(x):.0f}" if pd.notna(x) else "—"
    )

    cols = [
        "Status", "Ticker", "Side", "Strike", "Expiry", "Qty",
        "Bought", "Bought $", "Sold", "Sold $", "PnL %", "PnL $",
    ]
    st.dataframe(
        show[cols],
        use_container_width=True,
        hide_index=True,
        height=min(480, 48 + 36 * len(show)),
    )

    closed_only = view[view["Status"] == "CLOSED"]
    if not closed_only.empty:
        st.markdown("#### By ticker (closed)")
        grp = (
            closed_only.groupby("Ticker", dropna=False)
            .agg(
                Trades=("Ticker", "count"),
                Realized_PnL=("PnL_Dollars", "sum"),
                Avg_Pct=("PnL_Pct", "mean"),
            )
            .reset_index()
            .sort_values("Realized_PnL", ascending=False)
        )
        grp["Realized_PnL"] = grp["Realized_PnL"].map(
            lambda x: f"${float(x):+,.2f}" if pd.notna(x) else "—"
        )
        grp["Avg_Pct"] = grp["Avg_Pct"].map(_fmt_journal_pct)
        st.dataframe(grp, use_container_width=True, hide_index=True, height=200)

    csv_bytes = view.to_csv(index=False).encode("utf-8")
    st.download_button(
        "Download journal CSV",
        data=csv_bytes,
        file_name="options_journal.csv",
        mime="text/csv",
        key="journal_csv_dl",
    )
