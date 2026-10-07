"""Settings page — manual scan, flow filters and Telegram push."""

from __future__ import annotations

from datetime import datetime
import glob
import json

from ui import settings_store, shell
from ui.archives import _latest_archive_stamp
from ui.services import _discover_tickers
from ui.services import _run_daily_scanner
from ui.services import _scan_archive_metadata
from ui.telegram_push import _format_scan_message
from ui.telegram_push import _load_telegram_config
from ui.telegram_push import _send_telegram
from ui.telegram_push import gamma_summary_for
from ui.telegram_push import plan_for_message
from ui.widgets import _choice_control
import streamlit as st
from ui.common import ET


def render(cfg: dict) -> None:
    """Settings page: manual scan, flow filters, Telegram push (formerly the sidebar)."""
    focus_ticker = cfg["ticker"]
    st.subheader("Settings")

    # ── Manual scan for focus ticker ──────────────────────────────────
    st.markdown(f"**Daily scan — {focus_ticker}**")
    run_scan = st.button(
        f"🚀 Run Scan for {focus_ticker}",
        use_container_width=True,
        type="primary",
        help=f"Runs dailyScaner.py {focus_ticker} and saves a new archive JSON",
    )

    if run_scan:
        with st.spinner(f"Scanning {focus_ticker}… (may take a few minutes)"):
            ok, output = _run_daily_scanner(focus_ticker)
        if ok:
            st.success(f"{focus_ticker} scan complete — archive updated.")
            _scan_archive_metadata.clear()
            stamp = _latest_archive_stamp(focus_ticker)
            if stamp:
                st.session_state[f"_last_seen_archive_{focus_ticker}"] = stamp
            st.rerun()
        else:
            st.error(f"{focus_ticker} scanner returned an error.")
        with st.expander("Scanner output", expanded=not ok):
            st.code(output[-4000:], language="text")

    st.divider()
    st.markdown("**Flow filters**")
    top_n = shell.render_flow_filters(st, _choice_control)["top_n"]

    st.divider()
    st.markdown("**Best Value**")
    shell.render_best_value_settings(st)

    st.divider()
    st.markdown("**Game plan**")
    shell.render_game_plan_settings(st)

    shell.save_if_changed(st, settings_store.save)
    st.caption("Settings are saved automatically and restored the next time the app starts.")

    st.divider()
    # ── Telegram push ─────────────────────────────────────────────────
    with st.expander("📨 Telegram", expanded=False):
        tg_token, tg_chat = _load_telegram_config()
        configured = bool(tg_token and tg_chat)
        if configured:
            st.success("@zeuseaibot connected ✓", icon="✅")
        else:
            st.warning("Not configured — add keys to `.env`")
            st.code(
                "TELEGRAM_BOT_TOKEN=...\nTELEGRAM_CHAT_ID=...",
                language="text",
            )

        # ── Ticker selector ───────────────────────────────────────────
        tg_tickers = _discover_tickers()
        tg_default = tg_tickers.index(focus_ticker) if focus_ticker in tg_tickers else 0
        tg_ticker  = st.selectbox(
            "Ticker to send",
            tg_tickers,
            index=tg_default,
            key="tg_ticker_sel",
        )

        # Load latest archive for selected ticker
        tg_files = sorted(glob.glob(f"archive/{tg_ticker}_*.json"), reverse=True)
        tg_payload: dict | None = None
        tg_prev:    dict | None = None
        if tg_files:
            try:
                with open(tg_files[0]) as _f:
                    tg_payload = json.load(_f)
            except Exception:
                pass
        if len(tg_files) >= 2:
            try:
                with open(tg_files[1]) as _f:
                    tg_prev = json.load(_f)
            except Exception:
                pass

        if tg_payload:
            try:
                ts_str = datetime.fromisoformat(tg_payload["timestamp"]).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
            except Exception:
                ts_str = "—"
            st.caption(f"Latest: {ts_str} · Spot ${tg_payload.get('spot','—')}")

        # ── Section toggles ───────────────────────────────────────────
        st.caption("Sections to include:")
        inc_session    = st.checkbox("Session (spot, Δ, open, prev close)", value=True,  key="tg_session")
        inc_mtf        = st.checkbox("Multi-Timeframe Detail",               value=True,  key="tg_mtf")
        inc_magnets    = st.checkbox(f"The Magnets — top {int(top_n)} calls/puts",  value=True,  key="tg_magnets")
        inc_vol_exp    = st.checkbox("Volume by Expiry",                     value=True,  key="tg_volexp")
        inc_orb        = st.checkbox("Opening Range Breakout",               value=True,  key="tg_orb")
        inc_deltas     = st.checkbox("CALL Δ / PUT Δ vs previous run",       value=True,  key="tg_deltas")
        inc_best_value = st.checkbox("⭐ Best Value Option",                  value=True,  key="tg_bestval")
        inc_gamma      = st.checkbox("🧱 Gamma exposure (net, walls)",        value=True,  key="tg_gamma")
        inc_plan       = st.checkbox("🎯 Game plan (side, day, levels)",      value=True,  key="tg_plan")

        # ── Expiry drill-down selector ────────────────────────────────
        tg_expiries: list[str] = []
        if tg_payload:
            vol_block = tg_payload.get("volume") or {}
            exp_set: set[str] = set()
            for c in (vol_block.get("top_calls") or []) + (vol_block.get("top_puts") or []):
                if c.get("expiry"):
                    exp_set.add(c["expiry"])
            tg_expiries = sorted(exp_set)

        selected_expiries: list[str] = []
        if tg_expiries:
            selected_expiries = st.multiselect(
                "Expiry drill-down (optional)",
                options=tg_expiries,
                default=[],
                help="Select one or more expiries to include top contracts in the message",
                key="tg_expiry_sel",
            )

        # ── Send button ───────────────────────────────────────────────
        send_tg = st.button(
            "📤 Send to Telegram",
            use_container_width=True,
            disabled=not configured or not tg_payload,
            type="primary",
            key="tg_send_btn",
        )
        if send_tg and configured and tg_payload:
            tg_gamma = (gamma_summary_for(tg_ticker, tg_payload.get("spot"))
                        if (inc_gamma or inc_plan) else None)
            msg = _format_scan_message(
                payload=tg_payload,
                prev_payload=tg_prev,
                ticker=tg_ticker,
                top_n=int(top_n),
                include={
                    "session":       inc_session,
                    "mtf":           inc_mtf,
                    "magnets":       inc_magnets,
                    "volume_expiry": inc_vol_exp,
                    "orb":           inc_orb,
                    "deltas":        inc_deltas,
                    "best_value":    inc_best_value,
                    "gamma":         inc_gamma,
                    "game_plan":     inc_plan,
                },
                expiry_drill=selected_expiries or None,
                gamma=tg_gamma if inc_gamma else None,
                plan=(plan_for_message(tg_payload, tg_prev, tg_ticker, gamma=tg_gamma,
                                       stop_buffer_pct=float(
                                           shell.setting(st, "stop_buffer_pct")),
                                       min_reward_to_risk=float(
                                           shell.setting(st, "min_reward_to_risk")))
                      if inc_plan else None),
            )
            ok, err = _send_telegram(tg_token, tg_chat, msg)
            if ok:
                st.success("Sent ✈️")
            else:
                st.error(f"Failed: {err}")
