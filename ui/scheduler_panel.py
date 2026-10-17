"""Settings section: background jobs, what the scheduler scans, restart."""

from __future__ import annotations

import contextlib

import pandas as pd
import streamlit as st

import jobs_status as js


def render(boxed: bool = True) -> None:
    """``boxed`` puts it in a collapsed expander; Settings' Scheduler tab shows it open."""
    box = (st.expander("⏱️ Scheduler & background jobs", expanded=False) if boxed
           else contextlib.nullcontext())
    with box:
        rows = js.jobs()
        sched = next((r for r in rows if r["label"] == js.SCHEDULER_LABEL), None)
        if sched and sched["running"]:
            st.success(f"Scheduler: {sched['status']}", icon="✅")
        elif sched:
            st.error(f"Scheduler: {sched['status']}", icon="⛔")

        st.markdown("**Jobs**")
        bot = js.find_processes("telegram_bot.py")
        table = [{"Job": r["name"], "How often": r["schedule"], "Status": r["status"]}
                 for r in rows]
        table.append({"Job": "Telegram bot (menus)", "How often": "Always on while started",
                      "Status": f"Running · pid {', '.join(map(str, bot))}" if bot
                      else "Not running"})
        st.dataframe(pd.DataFrame(table), hide_index=True, use_container_width=True)

        st.markdown("**What the scheduler scans**")
        tickers = js.ticker_rows()
        if tickers:
            st.dataframe(pd.DataFrame([{
                "Ticker": t["ticker"], "Every (min)": t["interval_min"],
                "Last scan": t["last_scan"], "Next scan": t["next_scan"],
                "Telegram": t["telegram"]} for t in tickers]),
                hide_index=True, use_container_width=True)
            c1, c2, c3 = st.columns([1, 1, 1])
            pick = c1.selectbox("Ticker", [t["ticker"] for t in tickers],
                                key="sched_interval_ticker")
            cur = next(t["interval_min"] for t in tickers if t["ticker"] == pick)
            minutes = c2.number_input("Scan every (min)", 1, 240, int(cur), 1,
                                      key=f"sched_interval_{pick}")
            c3.write("")
            if c3.button("Save interval", use_container_width=True,
                         disabled=int(minutes) == int(cur)):
                js.set_interval(pick, int(minutes))
                st.success(f"{pick} now scans every {int(minutes)} min. The scheduler "
                           "picks it up within 30 seconds, no restart needed.")
                st.rerun()
        else:
            st.caption("No tickers to scan: none has an archive outside the exclusion list.")
        st.caption("Tickers come from the archive, minus the Tickers page's exclusions. "
                   "Scans run from the market open to the end-of-day scan time, ET.")

        st.markdown("**Restart the scheduler**")
        st.caption("Needed after a code change. A scan in progress is stopped and runs again on "
                   "the next cycle, so avoid restarting in the last minutes before the close.")
        sure = st.checkbox("Yes, restart it now", key="sched_restart_sure")
        if st.button("🔄 Restart scheduler", disabled=not sure, use_container_width=True):
            ok, msg = js.restart_scheduler()
            (st.success if ok else st.error)(msg)

        if st.checkbox("Show the last lines of logs/scheduler.log", key="sched_show_log"):
            tail = js.log_tail()
            st.code("\n".join(tail) if tail else "(no log yet)", language="text")
