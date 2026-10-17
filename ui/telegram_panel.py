"""Settings → Telegram: where each ticker's messages go and what was sent lately."""

from __future__ import annotations

import os
from datetime import datetime

import pandas as pd
import streamlit as st

import jobs_status as js
import telegram_log as tl
from ui.telegram_push import _load_telegram_config, bot_username


def _when(ts: str) -> str:
    try:
        return f"{datetime.fromisoformat(ts):%a %b %d %H:%M} ET"
    except (TypeError, ValueError):
        return ts or ""


def _gamma_alert(cfg: dict, ticker: str) -> str:
    own = (cfg.get("tickers") or {}).get(ticker) or {}
    return "Off" if own.get("gamma_alert") is False else "On"


def render_status() -> None:
    entries = tl.recent(200)
    last = tl.last_per_ticker(entries)
    cfg = js._read_json(os.path.join(js.BASE_DIR, "scheduler_config.json"), {})

    st.markdown("**Where each ticker's messages go**")
    tickers = js.ticker_rows()
    if tickers:
        rows = []
        for t in tickers:
            e = last.get(t["ticker"])
            rows.append({
                "Ticker": t["ticker"], "Route": t["telegram"],
                "Gamma alert": _gamma_alert(cfg, t["ticker"]),
                "Last send": _when(e["ts"]) if e else "None recorded",
                "Status": tl.status_text(e) if e else "",
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    else:
        st.caption("No tickers to scan yet.")

    if st.button("🔎 Check which bot each ticker uses", key="tg_check_bots"):
        checked = []
        for t in tickers:
            token, chat = _load_telegram_config(t["ticker"])
            ok, name = bot_username(token)
            checked.append({"Ticker": t["ticker"], "Bot": name if ok else f"❌ {name}",
                            "Bot id": tl.bot_id(token), "Chat": tl.mask_chat(chat)})
        st.dataframe(pd.DataFrame(checked), hide_index=True, use_container_width=True)
        st.caption("Asks Telegram for each bot's name. Tokens never leave .env.")

    st.markdown("**Recent messages**")
    if not entries:
        st.caption("Nothing recorded yet. Sends are recorded from this version on.")
        return
    show = entries[:30]
    st.dataframe(pd.DataFrame([{
        "When": _when(e.get("ts", "")), "Ticker": e.get("ticker", ""),
        "What": tl.KIND_LABELS.get(e.get("kind", ""), e.get("kind", "")),
        "Status": tl.status_text(e), "Bot id": e.get("bot_id", ""),
        "Chat": e.get("chat", ""), "Chars": e.get("chars", 0),
    } for e in show]), hide_index=True, use_container_width=True)
    labels = [f"{_when(e.get('ts', ''))} · {e.get('ticker', '')} · "
              f"{tl.KIND_LABELS.get(e.get('kind', ''), e.get('kind', ''))}" for e in show]
    pick = st.selectbox("Show the text of", range(len(show)),
                        format_func=lambda i: labels[i], key="tg_log_pick")
    st.code(show[pick].get("preview", "") or "(empty)", language="text")
    st.caption(f"First {tl.PREVIEW} characters, without formatting. "
               "Kept in data/telegram_sent.jsonl (last 500–1000 sends).")
