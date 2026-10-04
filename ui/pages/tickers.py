"""Tickers page — add, exclude and schedule the tickers the scanner follows."""

from __future__ import annotations

from datetime import datetime
import glob
import json
import os
import time

from ui.services import _EXCLUDED_FILE
from ui.services import _SCANNER_DIR
from ui.services import _bg_state
from ui.services import _discover_tickers
from ui.services import _load_excluded
from ui.services import _run_daily_scanner
from ui.services import _scan_archive_metadata
import pandas as pd
import streamlit as st
from ui.common import ET

_SCHED_CFG_FILE = os.path.join(_SCANNER_DIR, "scheduler_config.json")
_SCHED_DEFAULTS: dict = {
    "market_open":          "09:30",
    "market_close":         "16:00",
    "post_close_buffer_min": 15,
    "default_interval_min": 5,
    "notify_telegram":      True,
    "tickers":              {},
}


def _save_excluded(excluded: set[str]) -> None:
    """Persist the excluded set. Never raises."""
    try:
        with open(_EXCLUDED_FILE, "w") as f:
            json.dump(sorted(excluded), f)
    except Exception:
        pass


def _load_sched_cfg() -> dict:
    cfg = dict(_SCHED_DEFAULTS)
    try:
        with open(_SCHED_CFG_FILE) as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def _save_sched_cfg(cfg: dict) -> None:
    try:
        with open(_SCHED_CFG_FILE, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


def _get_ticker_interval(ticker: str) -> int:
    """Return the scan interval in minutes for a ticker (from scheduler_config.json)."""
    cfg = _load_sched_cfg()
    return int((cfg.get("tickers") or {}).get(ticker, {}).get(
        "interval_min", cfg.get("default_interval_min", 5)
    ))


def _set_ticker_interval(ticker: str, minutes: int) -> None:
    """Persist a per-ticker scan interval to scheduler_config.json."""
    cfg = _load_sched_cfg()
    cfg.setdefault("tickers", {})[ticker] = {"interval_min": minutes}
    _save_sched_cfg(cfg)


def _ticker_summary(t: str) -> dict:
    """Build a one-row summary dict for ticker t from its latest archive."""
    files = sorted(glob.glob(f"archive/{t}_*.json"), reverse=True)
    last_ts, last_spot, last_dir = "—", "—", "—"
    if files:
        try:
            with open(files[0]) as f:
                p = json.load(f)
            ts_raw    = p.get("timestamp", "")
            last_ts   = datetime.fromisoformat(ts_raw).astimezone(ET).strftime("%Y-%m-%d %H:%M ET") if ts_raw else "—"
            last_spot = f"${float(p.get('spot') or 0):.2f}"
            last_dir  = p.get("direction", "—")
        except Exception:
            pass
    s = _bg_state(t)
    if s["running"]:
        auto = f"⏳ scanning ({int(time.time()-s['t0'])}s)…"
    elif s.get("last_ts"):
        auto = f"{'✅' if s['last_ok'] else '⚠'} last: {s['last_ts']}"
    else:
        auto = "—"
    return {
        "Ticker":       t,
        "Last scan":    last_ts,
        "Spot":         last_spot,
        "Direction":    last_dir,
        "Interval (min)": _get_ticker_interval(t),
        "Auto-scan":    auto,
        "# files":      len(files),
    }


def render() -> None:
    """
    Ticker Manager — add new tickers, rescan existing ones, or remove a ticker
    from auto-scan (files are never deleted; removal only stops future scans).
    """
    excluded = _load_excluded()

    # ── Add / scan a new ticker ───────────────────────────────────────────────
    st.markdown("### Add a new ticker")
    st.caption(
        "Enter any valid symbol (e.g. TSLA, NVDA, SPY). "
        "The scanner saves to `archive/{TICKER}_*.json` and the ticker "
        "appears in the sidebar selector immediately."
    )
    col_input, col_btn = st.columns([3, 1], gap="small")
    with col_input:
        new_ticker = st.text_input(
            "Ticker symbol", placeholder="e.g. TSLA",
            label_visibility="collapsed",
        ).strip().upper()
    with col_btn:
        run_new = st.button("🚀 Run scan", type="primary", use_container_width=True)

    if run_new:
        if not new_ticker or not new_ticker.isalpha():
            st.error("Enter a valid ticker symbol (letters only).")
        else:
            # If it was excluded, re-activate first
            if new_ticker in excluded:
                excluded.discard(new_ticker)
                _save_excluded(excluded)
            with st.spinner(f"Scanning {new_ticker}… (may take several minutes)"):
                ok, output = _run_daily_scanner(new_ticker)
            if ok:
                st.success(f"✅ {new_ticker} scan complete — now active in the sidebar selector.")
                _scan_archive_metadata.clear()
            else:
                st.error(f"❌ Scan failed for {new_ticker}.")
            with st.expander("Scanner output", expanded=not ok):
                st.code(output[-4000:], language="text")

    st.markdown("---")

    # ── Active tickers ────────────────────────────────────────────────────────
    st.markdown("### Active tickers")
    st.caption(
        "Tickers shown in the sidebar selector and included in the 5-minute "
        "auto-scan. Click **Delete** to stop scanning a ticker — "
        "its archive files are never removed."
    )

    active = _discover_tickers()
    # Remove placeholder 'AAPL' if it has no actual files
    active = [t for t in active if glob.glob(f"archive/{t}_*.json")]

    if not active:
        st.info("No active tickers. Run a scan above to get started.")
    else:
        # Build editable dataframe — "Select" checkbox + editable "Interval (min)"
        rows = [_ticker_summary(t) for t in active]
        orig_df = pd.DataFrame(rows)
        orig_df.insert(0, "☑", False)   # selection column

        st.caption(
            "✏️ Edit **Interval (min)** directly in the table — changes save on the fly.  "
            "Check **☑** to select rows for bulk actions."
        )
        edited_df = st.data_editor(
            orig_df,
            column_config={
                "☑":              st.column_config.CheckboxColumn("☑", default=False, width="small"),
                "Ticker":         st.column_config.TextColumn(disabled=True),
                "Last scan":      st.column_config.TextColumn(disabled=True),
                "Spot":           st.column_config.TextColumn(disabled=True),
                "Direction":      st.column_config.TextColumn(disabled=True),
                "Interval (min)": st.column_config.NumberColumn(
                    "Interval (min)", min_value=1, max_value=120, step=1,
                    help="Scan interval in minutes. Saved to scheduler_config.json immediately.",
                ),
                "Auto-scan":      st.column_config.TextColumn(disabled=True),
                "# files":        st.column_config.NumberColumn(disabled=True),
            },
            hide_index=True,
            use_container_width=True,
            key="tab4_editor",
        )

        # ── Auto-save interval changes ─────────────────────────────────────
        saved_tickers: list[str] = []
        for _, row in edited_df.iterrows():
            ticker  = row["Ticker"]
            new_val = int(row["Interval (min)"])
            old_val = _get_ticker_interval(ticker)
            if new_val != old_val:
                _set_ticker_interval(ticker, new_val)
                saved_tickers.append(f"✅ {ticker} → {new_val} min")
        if saved_tickers:
            for msg in saved_tickers:
                st.toast(msg)

        selected = edited_df[edited_df["☑"]]["Ticker"].tolist()

        btn_col1, btn_col2 = st.columns(2, gap="small")
        with btn_col1:
            if st.button(
                f"🗑 Remove from auto-scan ({len(selected)})" if selected else "🗑 Remove from auto-scan",
                key="tab4_del_btn", type="secondary",
                disabled=not selected, use_container_width=True,
            ):
                for t in selected:
                    excluded.add(t)
                _save_excluded(excluded)
                st.success(f"**{', '.join(selected)}** removed from auto-scan. Files kept.")
                st.rerun()

        with btn_col2:
            if st.button(
                f"🔄 Rescan ({len(selected)})" if selected else "🔄 Rescan",
                key="tab4_rescan_btn", type="primary",
                disabled=not selected, use_container_width=True,
            ):
                for t in selected:
                    with st.spinner(f"Rescanning {t}…"):
                        ok, output = _run_daily_scanner(t)
                    if ok:
                        st.success(f"✅ {t} rescanned.")
                        _scan_archive_metadata.clear()
                    else:
                        st.error(f"❌ Rescan failed for {t}.")
                    with st.expander(f"{t} output", expanded=not ok):
                        st.code(output[-4000:], language="text")

        # ── Global schedule settings ───────────────────────────────────────
        with st.expander("⚙️ Global schedule settings", expanded=False):
            cfg_now = _load_sched_cfg()
            gc1, gc2 = st.columns(2)
            with gc1:
                new_default = st.number_input(
                    "Default interval — all tickers (min)",
                    min_value=1, max_value=60,
                    value=int(cfg_now.get("default_interval_min", 5)), step=1,
                    key="tab4_default_interval",
                )
            with gc2:
                new_buffer = st.number_input(
                    "Post-close buffer (min)",
                    min_value=0, max_value=60,
                    value=int(cfg_now.get("post_close_buffer_min", 15)), step=5,
                    key="tab4_buffer",
                    help="Scans run this many minutes after 16:00 ET to capture delayed end-of-day data.",
                )
            if st.button("💾 Save global settings", key="tab4_save_globals"):
                cfg_now["default_interval_min"]  = int(new_default)
                cfg_now["post_close_buffer_min"] = int(new_buffer)
                _save_sched_cfg(cfg_now)
                st.success(f"Saved — default {new_default} min · post-close buffer {new_buffer} min")

    # ── Excluded / paused tickers ─────────────────────────────────────────────
    paused = sorted(t for t in excluded if glob.glob(f"archive/{t}_*.json"))
    if paused:
        st.markdown("---")
        st.markdown("### Paused tickers")
        st.caption("These tickers have archive data but auto-scan is stopped. Files are untouched.")

        rows_p = [_ticker_summary(t) for t in paused]
        st.caption("Select rows then click Re-add.")
        ev_p = st.dataframe(
            pd.DataFrame(rows_p),
            on_select="rerun",
            selection_mode="multi-row",
            use_container_width=True,
            hide_index=True,
        )
        readd_sel = [paused[i] for i in ev_p.selection.rows]

        if st.button(
            f"✅ Re-add to auto-scan ({len(readd_sel)})" if readd_sel else "✅ Re-add to auto-scan",
            key="tab4_readd_btn",
            disabled=not readd_sel,
            use_container_width=True,
        ):
            for t in readd_sel:
                excluded.discard(t)
            _save_excluded(excluded)
            st.success(f"**{', '.join(readd_sel)}** restored to auto-scan.")
            st.rerun()
