"""
app.py — AAPL Options Scanner Dashboard
Display-only layer. All analytical numbers come from dailyScaner.py
functions or archive JSON files. No indicators recomputed here.
"""

import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta

import streamlit as st

from logging_config import LOG_DIR, setup_logging

import ema_stack
import gex_page
import volume_page
from ui import shell
from ui.common import ET
from ui.archives import (
    _latest_archive,
    _latest_archive_stamp,
    _latest_weekly_archive,
)
from ui.market import _cached_vwap_state, render_market_banner
from ui.widgets import _streamlit_ge
from ui.option_math import _bs_greeks
from ui.services import _SCANNER_DIR, _discover_tickers, _scan_archive_metadata
from ui.pages import archive as archive_page
from ui.pages import flow as flow_page
from ui.pages import journal as journal_page
from ui.pages import settings as settings_page
from ui.pages import news as news_page
from ui.pages import spread_gate as spread_gate_page
from ui.pages import tickers as tickers_page
from ui.components import REGISTRY as COMPONENTS
from ui.context import load_scan_context
import pre_trade_check as pre_trade_check


# Dashboard process log (rotating). Streamlit also writes to its own sinks.
setup_logging("app", console=False)
log = logging.getLogger("app")

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Options Scanner",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Global CSS — institutional dark dashboard ─────────────────────────────────
st.markdown(
    """
    <style>
    /* Hide Streamlit chrome */
    #MainMenu {visibility: hidden;}
    header[data-testid="stHeader"] {background: transparent;}
    footer {visibility: hidden;}
    footer:after {content: none;}

    /* Tight top padding — dashboard starts immediately */
    .block-container {
        padding-top: 1rem !important;
        padding-bottom: 2rem !important;
        max-width: 1400px;
    }

    /* KPI / metric polish */
    div[data-testid="stMetric"] {
        background: rgba(255,255,255,0.03);
        border: 1px solid rgba(255,255,255,0.08);
        border-radius: 10px;
        padding: 0.75rem 0.9rem;
    }
    div[data-testid="stMetric"] label {
        color: #9e9e9e !important;
        font-size: 0.78rem !important;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }
    div[data-testid="stMetricValue"] {
        font-size: 1.35rem !important;
        font-weight: 700 !important;
    }

    /* Tabs */
    .stTabs [data-baseweb="tab-list"] {
        gap: 0.25rem;
        border-bottom: 1px solid rgba(255,255,255,0.08);
    }
    .stTabs [data-baseweb="tab"] {
        padding: 0.55rem 1rem;
        font-weight: 600;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _watch_archive_auto_refresh(ticker: str) -> None:
    """
    Poll for a new scheduler/manual archive and full-rerun the page so Tab 1
    picks up fresh data without a manual browser refresh.
    """
    @st.fragment(run_every=timedelta(seconds=20))
    def _watcher():
        stamp = _latest_archive_stamp(ticker)
        key = f"_last_seen_archive_{ticker}"
        prev = st.session_state.get(key)
        if stamp is None:
            st.caption("Waiting for first archive…")
            return
        if prev is None:
            st.session_state[key] = stamp
            st.caption("Auto-refresh on · watching for new scans")
            return
        if stamp != prev:
            st.session_state[key] = stamp
            try:
                _scan_archive_metadata.clear()
            except Exception:
                pass
            st.toast(f"New {ticker} scan detected — refreshing…")
            st.rerun()
        st.caption("Auto-refresh on · watching for new scans")

    _watcher()


def _service_pid(pidfile: str, script_hint: str | None = None) -> int | None:
    """
    Return PID from a .pid file if that process is alive AND (when script_hint
    is set) its command line still looks like our service. Prevents stale
    post-reboot PIDs from looking "green".
    """
    try:
        with open(os.path.join(_SCANNER_DIR, pidfile)) as fh:
            pid = int(fh.read().strip())
        os.kill(pid, 0)   # raises if dead
        if script_hint:
            try:
                cmd = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "command="], text=True
                ).strip()
                if script_hint not in cmd:
                    return None
                # Suspended (Ctrl+Z) → treat as dead
                state = subprocess.check_output(
                    ["ps", "-p", str(pid), "-o", "state="], text=True
                ).strip()
                if state.startswith("T"):
                    return None
            except Exception:
                return None
        return pid
    except Exception:
        return None


def _ensure_services() -> None:
    """
    Called once per Streamlit session (tracked via st.session_state).
    Launches scheduler.py and telegram_bot.py as independent subprocesses
    if they are not already running.
    """
    if st.session_state.get("_services_started"):
        return
    st.session_state["_services_started"] = True

    python = sys.executable

    for script, pidfile in [
        ("scheduler.py",    "scheduler.pid"),
        ("telegram_bot.py", None),           # no PID file — check ps
    ]:
        script_path = os.path.join(_SCANNER_DIR, script)
        if not os.path.exists(script_path):
            continue

        if pidfile and _service_pid(pidfile, script_hint=script):
            continue   # already running

        # For telegram_bot check process list
        if pidfile is None:
            import subprocess as _sp
            out = _sp.run(["pgrep", "-f", script], capture_output=True, text=True)
            if out.stdout.strip():
                continue  # already running

        os.makedirs(LOG_DIR, exist_ok=True)
        log_name = script.replace(".py", ".log")
        log_path = os.path.join(LOG_DIR, log_name)
        subprocess.Popen(
            [python, script_path],
            cwd=_SCANNER_DIR,
            stdout=open(log_path, "a"),
            stderr=subprocess.STDOUT,
            env={**os.environ, "OPTIONTRADING_PROCESS": script.replace(".py", "")},
        )


def _services_status() -> list[tuple[str, bool, str]]:
    """Return [(name, running, detail)] for each service."""
    rows = []
    # Scheduler — prefer pidfile, fall back to pgrep
    import subprocess as _sp
    pid = _service_pid("scheduler.pid", script_hint="scheduler.py")
    if pid is None:
        out = _sp.run(["pgrep", "-f", "scheduler.py"], capture_output=True, text=True)
        pids = out.stdout.strip().split()
        # Exclude this Streamlit process matching falsely; keep python scheduler
        pid = int(pids[0]) if pids else None
    rows.append(("Scheduler", pid is not None,
                 f"pid {pid}" if pid else "not running"))
    # Telegram bot — check by process name
    out = _sp.run(["pgrep", "-f", "telegram_bot.py"], capture_output=True, text=True)
    bot_pids = out.stdout.strip().replace("\n", " ")
    rows.append(("Telegram bot", bool(bot_pids),
                 f"pid {bot_pids}" if bot_pids else "not running"))
    return rows


def _services_alert() -> None:
    """
    Top-of-page alert when Scheduler or Telegram bot is down.
    Silent when everything is running.
    """
    _ensure_services()
    down = [(name, detail) for name, running, detail in _services_status() if not running]
    if not down:
        return
    lines = "  ·  ".join(f"**{name}** ({detail})" for name, detail in down)
    st.error(
        f"🔴 Service down: {lines}  — scans / Telegram alerts may be paused. "
        f"Restart with `python3 scheduler.py` / `python3 telegram_bot.py`.",
        icon="🔴",
    )


def _ema_stack_banner(cfg: dict) -> None:
    """15-min EMA 9/21/50 trend rule, shown on every page. Display only."""
    info = ema_stack.banner_for_archive(cfg.get("latest_archive"), now=datetime.now(ET))
    ticker = str(cfg.get("ticker") or "").upper()
    when = f" (scan {info['as_of'].astimezone(ET):%H:%M} ET)" if info.get("as_of") else ""
    stale = " ⚠️ Scan is over 30 min old — the trend may have changed." if info.get("stale") else ""
    text = f"**{ticker} {info['headline']}**{when} — {info['reason']}{stale}"
    show = {
        ema_stack.BULL: (st.success, "🟢"),
        ema_stack.BEAR: (st.error, "🔴"),
        ema_stack.NO_TRADE: (st.warning, "⛔"),
    }.get(info["state"], (st.info, "ℹ️"))
    show[0](text, icon=show[1])


def _shell() -> dict:
    """Left-hand menu and ticker bar. Returns the cfg every page receives."""
    page = shell.render_menu(st, material_icons=_streamlit_ge(1, 40))

    known_tickers = _discover_tickers()
    picked = shell.stored_ticker(st, known_tickers)
    latest = _latest_archive(picked)
    status = None
    if latest:
        ts = datetime.fromisoformat(latest["timestamp"]).astimezone(ET)
        status = (f"Last scan {ts.strftime('%Y-%m-%d %H:%M ET')} · "
                  f"spot at run {latest.get('spot', '—')}")
    focus_ticker = shell.render_ticker_bar(st, known_tickers, status=status)
    if focus_ticker != picked:
        latest = _latest_archive(focus_ticker)

    with st.sidebar:
        st.divider()
        # Auto-refresh when scheduler (or another process) writes a new archive
        _watch_archive_auto_refresh(focus_ticker)

    # Auto-launch scheduler / telegram bot once per session (status alert is at page top)
    _ensure_services()

    return {
        "page":           page,
        "run":            False,
        "ticker":         focus_ticker,
        **shell.flow_settings(st),
        "latest_archive": latest,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def _open_pre_trade_if_requested() -> str | None:
    """A candidate link or an archive prefill jumps to the Pre-Trade Check page."""
    cand = pre_trade_check.read_candidate_query(st)
    if cand and st.session_state.get("ptc_nav_done_for") != cand:
        shell.go(st, "pretrade")
        st.session_state["ptc_nav_done_for"] = cand

    arch = st.session_state.get(pre_trade_check.ARCHIVE_PREFILL_KEY)
    arch_id = arch.get("id") if isinstance(arch, dict) else None
    if arch_id and st.session_state.get("ptc_nav_done_for") != arch_id:
        shell.go(st, "pretrade")
        st.session_state["ptc_nav_done_for"] = arch_id
    return cand


def _render_pre_trade(cfg: dict, cand: str | None) -> None:
    latest = cfg.get("latest_archive") or {}
    spot = latest.get("spot")
    try:
        spot_f = float(spot) if spot is not None else None
    except (TypeError, ValueError):
        spot_f = None
    # Spot is a chart field — do not pass it when a candidate
    # or archive prefill is loaded; archive sets underlying itself.
    arch_pending = st.session_state.get(pre_trade_check.ARCHIVE_PREFILL_KEY)
    ticker = str(cfg.get("ticker") or "")
    vwap_info = _cached_vwap_state(ticker) if ticker else {}
    weekly_payload = _latest_weekly_archive(ticker) if ticker else {}
    pre_trade_check.render_pre_trade_page(
        default_ticker=ticker,
        default_spot=None if (cand or arch_pending) else spot_f,
        or_data=(latest.get("or_data") or {}) if latest else {},
        vwap=(vwap_info or {}).get("VWAP"),
        emas={
            "daily": (weekly_payload.get("daily") or {}),
            "weekly": (weekly_payload.get("weekly") or {}),
        },
    )


def main():
    cand = _open_pre_trade_if_requested()      # before the menu, so it highlights correctly
    cfg = _shell()

    # Service-down alerts sit above the page so they're visible everywhere
    _services_alert()
    _ema_stack_banner(cfg)

    page = cfg["page"]
    latest = cfg.get("latest_archive") or {}
    if page == "flow":
        render_market_banner()
        flow_page.render(cfg)
    elif page == "archive":
        render_market_banner()
        archive_page.render(cfg)
    elif page == "spread_gate":
        render_market_banner()
        spread_gate_page.render(cfg)
    elif page == "tickers":
        tickers_page.render()
    elif page == "news":
        news_page.render(cfg)
    elif page == "volume":
        volume_page.render_volume_page(
            cfg["ticker"], tz=ET, spot=latest.get("spot"),
            scan_ts=latest.get("timestamp"), greeks_fn=_bs_greeks,
        )
    elif page == "gamma":
        gex_page.render_gex_page(cfg["ticker"], tz=ET, spot=latest.get("spot"))
    elif page == "journal":
        journal_page.render()
    elif page == "pretrade":
        _render_pre_trade(cfg, cand)
    elif page == "settings":
        settings_page.render(cfg)
    elif page in COMPONENTS:
        ctx = load_scan_context(cfg["ticker"], top_n=cfg["top_n"])
        if ctx is None:
            st.info(f"No archive data found for {cfg['ticker']}. "
                    "Run the scanner first or pick a different ticker.")
        else:
            COMPONENTS[page].render(ctx)


if __name__ == "__main__" or True:
    main()
