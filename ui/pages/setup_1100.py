"""11:00 Setup page — the paper record of the 11:00 setup (``setup_1100``). Display only."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

import setup_1100 as su
from ui.common import ET
from ui.widgets import centered_table_html

RESULT = {su.WIN: "✅ target", su.LOSS_STOP: "🛑 stop", su.TIME_EXIT: "⌛ time",
          su.NO_QUOTE: "❔ no quote", su.OPEN: "⏳ open"}


def _hm(v) -> str:
    if not v or pd.isna(v):
        return ""
    return datetime.fromisoformat(str(v)).strftime("%H:%M")


def _money(v) -> str:
    return "" if v is None or pd.isna(v) else f"${float(v):.2f}"


def table(hist: pd.DataFrame) -> pd.DataFrame:
    """One row per session, newest first, in words."""
    rows = []
    for r in hist.to_dict("records"):
        traded = r["action"] == su.TRADE
        d = datetime.fromisoformat(r["day"])
        rows.append({
            "Date": f"{d:%a %b} {d.day}",
            "Decision": {su.TRADE: f"{r['side'].title()}" if traded else "",
                         su.NO_TRADE: "No trade", su.MISSED: "Missed"}.get(r["action"], ""),
            "Contract": (f"{r['strike']:g} exp {datetime.fromisoformat(r['expiry']):%b %d}"
                         if traded else ""),
            "Entry": f"{_money(r['entry'])} at {_hm(r['decided_at'])}" if traded else "",
            "Exit": (f"{_money(r['exit_price'])} at {_hm(r['exit_at'])}"
                     if traded and r.get("exit_at") else ""),
            "Result": RESULT.get(r.get("status"), "") if traded else "",
            "P&L": "" if r.get("pnl") is None or pd.isna(r.get("pnl")) else f"{r['pnl']:+.0f}",
            "Why": r["reason"],
        })
    return pd.DataFrame(rows)


def _today(row: dict | None) -> None:
    now = datetime.now(ET)
    if row is None:
        when = "at the first scan after 11:00 ET" if now.time() < su.DECIDE_UNTIL else "tomorrow"
        st.info(f"No decision yet today. It is made {when}.")
        return
    if row["action"] == su.TRADE:
        line = (f"**Buy 1 {row['ticker']} {row['strike']:g} {row['side'].lower()}** "
                f"(exp {datetime.fromisoformat(row['expiry']):%b %d}) at "
                f"{_money(row['entry'])} · target {_money(row['target'])} · "
                f"stop {_money(row['stop'])} · out by {_hm(row['exit_by'])} ET")
        result = RESULT.get(row["status"], row["status"])
        if row["status"] != su.OPEN and row.get("pnl") is not None:
            result += f" {row['pnl']:+.0f} USD"
        (st.success if row["status"] == su.WIN else st.info)(f"{line}\n\n{result} · {row['reason']}")
    else:
        st.warning(f"**No trade today.** {row['reason']}")


def render(cfg: dict) -> None:
    ticker = str(cfg.get("ticker") or "").upper()
    st.subheader(f"{ticker} 11:00 setup · paper record")
    st.caption("Recorded by the scanner; nothing is bought or sold. Compare its results "
               "with the replay before trading it with money.")
    if ticker not in su.TICKERS:
        st.info(f"The 11:00 setup runs for {', '.join(sorted(su.TICKERS))} only: the replay "
                "behind it used that ticker's quotes.")
        return

    hist = su.history(ticker)
    today = datetime.now(ET).date().isoformat()
    _today(next((r for r in hist.to_dict("records") if r["day"] == today), None))

    s = su.stats(hist)
    rp = su.REPLAY
    c = st.columns(4)
    c[0].metric("Trades closed", s["closed"],
                help=f"Sessions recorded: {s['sessions']} · no trade: {s['no_trade']} · "
                     f"missed: {s['missed']}. Replay: {rp['trades']} trades in "
                     f"{rp['sessions']} sessions.")
    c[1].metric("Winners", "—" if s["win_pct"] is None else f"{s['win_pct']}%",
                help=f"Replay: {rp['win_pct']}%")
    c[2].metric("Total (USD)", f"{s['total']:+.0f}")
    c[3].metric("Average per trade", "—" if s["avg"] is None else f"{s['avg']:+.2f}",
                help=f"Replay: +{rp['avg_usd']:.2f} per trade, with stops slipped")

    with st.expander("The rule"):
        st.markdown(
            f"- **When:** the first scan between {su.DECIDE_FROM:%H:%M} and "
            f"{su.DECIDE_UNTIL:%H:%M} ET. One decision a day.\n"
            "- **Side:** only when both agree: the 15-minute EMAs are stacked (9 > 21 > 50 "
            "calls, 9 < 21 < 50 puts) and price is on the same side of the morning "
            f"reference (the first scan at or after {su.MORNING_REF:%H:%M}). Otherwise no trade.\n"
            "- **Contract:** 1 at-the-money option of that side, nearest expiry, at the ask; "
            f"no trade if the spread is over ${su.MAX_SPREAD:.2f} or the ask under "
            f"${su.MIN_ASK:.2f}.\n"
            f"- **Exit:** +${su.TARGET:.2f} target, −${su.STOP:.2f} stop on the bid, or "
            f"{su.HOLD_MIN} minutes after entry.\n"
            "- Scans run every 5 minutes, so a stop is recorded at the bid of the scan that "
            "saw it, usually worse than −$10. A target is recorded at the target, as a limit "
            "order would fill.")

    if hist.empty:
        st.caption("No sessions recorded yet.")
        return
    st.markdown(centered_table_html(table(hist), uuid="setup1100"), unsafe_allow_html=True)
