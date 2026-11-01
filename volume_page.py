"""Volume page — every traded contract, sortable/filterable, with per-contract history.

Reads data/volume_history.db (written by the scanner). Display only.
"""

from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Callable

import pandas as pd
import streamlit as st

import pre_trade_check
import volume_history as vh


def _fmt_ts(ts: str | None, tz) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromisoformat(ts).astimezone(tz).strftime("%a %b %d %H:%M ET")
    except ValueError:
        return str(ts)


def render_volume_page(
    ticker: str,
    *,
    tz,
    spot: float | None,
    scan_ts: str | None,
    greeks_fn: Callable[..., tuple[float | None, float | None, float | None]],
    today: date | None = None,
) -> None:
    ticker = str(ticker or "").upper()
    st.subheader(f"{ticker} volume & open interest")

    status = vh.recording_status(ticker)
    if not status["scans"]:
        st.info(
            f"No volume history for {ticker} yet. Recording starts with the next scanner run "
            "and builds up from there. Rising open interest needs at least 3 sessions."
        )
        return
    st.caption(
        f"Recording since {_fmt_ts(status['first_ts'], tz)} · {status['scans']} scans · "
        f"{status['sessions']} session(s) · latest {_fmt_ts(status['last_ts'], tz)}. "
        "Volume is cumulative for the day; OI is the previous session's close."
    )

    today = today or datetime.now(tz).date()
    _render_bursts(ticker, tz=tz, spot=spot)
    st.divider()
    latest = vh.latest_scan(ticker)
    from ui.components import volume_by_strike
    volume_by_strike.render(latest, spot=spot, today=today)
    st.divider()
    table = vh.explorer_table(latest, vh.daily_summary(ticker), today=today)
    table = table[table["dte"] >= 1].reset_index(drop=True)          # no 0DTE on this page
    if table.empty:
        st.caption("No contracts expiring after today in the latest scan.")
        return

    # ── filters ──────────────────────────────────────────────────────────────
    c1, c2, c3, c4 = st.columns([1.2, 1.2, 1, 1.6])
    vmax = int(table["volume"].max() or 0)
    min_vol = c1.number_input("Min volume", min_value=0, max_value=max(vmax, 0), value=0,
                              step=100, key="vol_min")
    max_vol = c2.number_input("Max volume", min_value=0, max_value=max(vmax, 0), value=vmax,
                              step=100, key="vol_max")
    side = c3.radio("Side", ["All", "Calls", "Puts"], horizontal=True, key="vol_side")
    exp_dates = pd.to_datetime(table["expiry"]).dt.date
    first_exp, last_exp = exp_dates.min(), exp_dates.max()
    picked = c4.date_input("Expiry from – to", value=(first_exp, last_exp),
                           min_value=first_exp, max_value=last_exp, key="vol_expiry")
    picked = tuple(picked) if isinstance(picked, (tuple, list)) else (picked,)
    exp_from = picked[0] if picked else first_exp
    exp_to = picked[1] if len(picked) > 1 else exp_from          # while picking the 2nd date

    c5, c6 = st.columns([2, 1])
    strike_q = c5.text_input("Strike (e.g. 350 or 340-360)", key="vol_strike").strip()
    building_only = c6.toggle("OI rising 2+ sessions", key="vol_building",
                              disabled=status["sessions"] < 3,
                              help="Needs at least 3 recorded sessions."
                              if status["sessions"] < 3 else None)

    view = table[(table["volume"] >= min_vol) & (table["volume"] <= max_vol)
                 & exp_dates.between(exp_from, exp_to)]
    if side != "All":
        view = view[view["side"] == ("CALL" if side == "Calls" else "PUT")]
    if strike_q:
        try:
            if "-" in strike_q:
                lo, hi = (float(x) for x in strike_q.split("-", 1))
                view = view[view["strike"].between(min(lo, hi), max(lo, hi))]
            else:
                view = view[view["strike"] == float(strike_q)]
        except ValueError:
            st.caption("Strike filter not understood — use a number or a range like 340-360.")
    if building_only:
        view = view[view["oi_up_days"] >= 2]
    view = view.reset_index(drop=True)
    st.caption(f"{len(view)} of {len(table)} contracts · click a column header to sort · "
               "select a row for its history")

    event = st.dataframe(
        view,
        hide_index=True,
        use_container_width=True,
        on_select="rerun",
        selection_mode="single-row",
        key=f"vol_table_{ticker}",
        column_config={
            "side": "Side",
            "strike": st.column_config.NumberColumn("Strike", format="$%.1f"),
            "expiry": "Expiry",
            "dte": st.column_config.NumberColumn("DTE", format="%dd"),
            "volume": st.column_config.NumberColumn("Volume", format="%d"),
            "open_interest": st.column_config.NumberColumn("OI", format="%d"),
            "oi_change": st.column_config.NumberColumn("OI Δ vs prev session", format="%+d"),
            "oi_up_days": st.column_config.NumberColumn("OI ↑ sessions", format="%d"),
            "bid": st.column_config.NumberColumn("Bid", format="$%.2f"),
            "ask": st.column_config.NumberColumn("Ask", format="$%.2f"),
            "last": st.column_config.NumberColumn("Last", format="$%.2f"),
            "iv": st.column_config.NumberColumn("IV", format="%.3f"),
        },
    )
    rows = list(getattr(getattr(event, "selection", None), "rows", None) or [])
    if not rows or rows[0] >= len(view):
        return
    _render_contract(view.iloc[rows[0]], ticker=ticker, tz=tz, spot=spot, scan_ts=scan_ts,
                     greeks_fn=greeks_fn)


BIG_PREMIUM = 1_000_000      # $ estimate that earns the alert line


def _render_bursts(ticker: str, *, tz, spot: float | None) -> None:
    """Top N volume jumps of the latest session (``vh.volume_bursts``), with an alert line
    for jumps of $1M+ estimated premium. Display only: which side traded is unknown."""
    st.markdown("**Biggest volume jumps — latest session**")
    c1, c2, c3 = st.columns([1, 2, 2])
    top = c1.number_input("Top", min_value=3, max_value=50, value=10, step=1, key="burst_top")
    sort_by = c2.radio("Rank by", ["Premium $", "Contracts"], horizontal=True, key="burst_sort")
    skip_itm = c3.toggle("Hide deep in-the-money (>5%)", value=True, key="burst_itm",
                         help="Their premium is mostly intrinsic value: usually stock "
                              "replacement or hedges, not bets.")
    b = vh.volume_bursts(ticker, top=int(top), spot=spot, itm_limit=0.05 if skip_itm else None,
                         sort_by="premium" if sort_by == "Premium $" else "added")
    if b.empty:
        st.caption("No volume recorded in the latest session yet.")
        return
    big = b[b["premium"] >= BIG_PREMIUM]
    if not big.empty:
        lines = [f"{r.side} ${r.strike:g} {r.expiry[5:]} · +{r.added:,.0f} at ~${r.mid:.2f} "
                 f"(~${r.premium / 1e6:.1f}M) at {_fmt_ts(r.ts_et, tz)[-9:]}"
                 for r in big.itertuples()]
        st.warning("🔔 Big blocks today: " + " · ".join(lines))
    st.dataframe(
        b, hide_index=True, use_container_width=True,
        column_config={
            "ts_et": "Scan", "side": "Side",
            "strike": st.column_config.NumberColumn("Strike", format="$%.1f"),
            "expiry": "Expiry",
            "added": st.column_config.NumberColumn("Added since last scan", format="%d"),
            "premium": st.column_config.NumberColumn("Premium (est.)", format="$%.0f"),
            "mid": st.column_config.NumberColumn("Mid", format="$%.2f"),
            "bid": st.column_config.NumberColumn("Bid", format="$%.2f"),
            "ask": st.column_config.NumberColumn("Ask", format="$%.2f"),
            "volume": st.column_config.NumberColumn("Day volume", format="%d"),
            "open_interest": st.column_config.NumberColumn("OI", format="%d"),
            "vol_oi": st.column_config.NumberColumn("Vol / OI", format="%.1f"),
        })
    st.caption("Volume added between two scans (~15 min apart). Whether it was bought or "
               "sold is not recorded — next morning's open interest shows if it opened a "
               "position. The 09:30 carry-over of yesterday's volume is filtered out.")


def _render_contract(row: pd.Series, *, ticker, tz, spot, scan_ts, greeks_fn) -> None:
    side, strike, expiry, dte = row["side"], float(row["strike"]), row["expiry"], int(row["dte"])
    st.markdown(f"#### {ticker} {side} ${strike:g} · {expiry} · {dte}d")
    hist = vh.contract_history(ticker, side, strike, expiry)
    if hist.empty:
        st.caption("No history recorded for this contract.")
        return
    hist["time"] = pd.to_datetime(hist["ts_et"], utc=True).dt.tz_convert(tz)

    left, right = st.columns(2)
    with left:
        st.caption("Volume by scan (cumulative within each day)")
        st.line_chart(hist.set_index("time")[["volume"]])
    with right:
        daily = hist.sort_values("ts_et").groupby("session_date").tail(1).set_index("session_date")
        st.caption("End-of-day volume and open interest")
        st.bar_chart(daily[["volume"]])
        st.line_chart(daily[["open_interest"]])

    with st.expander(f"All {len(hist)} recorded scans"):
        st.dataframe(hist.drop(columns=["time"]), hide_index=True, use_container_width=True)

    # ── pre-trade check ──────────────────────────────────────────────────────
    iv = row.get("iv")
    delta = gamma = theta = None
    if spot and iv is not None and not (isinstance(iv, float) and math.isnan(iv)) and iv > 0:
        delta, gamma, theta = greeks_fn(float(spot), strike, float(iv), dte, is_call=side == "CALL")
    num = lambda v: float(v) if v is not None and not pd.isna(v) and float(v) > 0 else None
    prefill_row = {
        "side": side, "strike": strike, "expiry": expiry, "dte": dte,
        "_delta": delta, "_theta": theta, "_theta_units": "per day",
        "_oi": row.get("open_interest"), "_bid": num(row.get("bid")), "_ask": num(row.get("ask")),
    }
    refuse = pre_trade_check.archive_greeks_refusal(delta, theta)
    if refuse:
        st.caption(refuse)
    if st.button("Run pre-trade check", key=f"vol_check_{ticker}", disabled=bool(refuse),
                 help=refuse or "Open Pre-Trade Check with this contract prefilled."):
        prefill = pre_trade_check.archive_prefill_from_row(
            prefill_row, ticker=ticker, underlying=spot, scan_ts=scan_ts)
        if prefill is None:
            st.warning(refuse or "Cannot prefill this contract.")
        else:
            pre_trade_check.stage_archive_prefill(st, prefill)
            st.rerun()
