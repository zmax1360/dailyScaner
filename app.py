"""
app.py — AAPL Options Scanner Dashboard
Display-only layer. All analytical numbers come from dailyScaner.py
functions or archive JSON files. No indicators recomputed here.
"""

import glob
import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytz
import streamlit as st

from logging_config import LOG_DIR, setup_logging

import data_adapter
import ema_stack
import gex_page
import volume_page
from ui import shell
from ui.common import ET
from ui.market import (
    _build_best_value_df,
    _cached_vwap_state,
    _market_is_closed,
    _now_et,
    _rsi_plain,
)
from ui.widgets import (
    _choice_control,
    _streamlit_ge,
)
from ui.option_math import (
    _bs_greeks,
    _norm_cdf,
    _norm_pdf,
)
from ui.services import (
    _BG,
    _EXCLUDED_FILE,
    _SCANNER_DIR,
    _bg_state,
    _discover_all_tickers,
    _discover_tickers,
    _load_excluded,
    _run_daily_scanner,
    _scan_archive_metadata,
)
from ui.pages import archive as archive_page
from ui.pages import flow as flow_page
from ui.pages import journal as journal_page
from ui.pages import news as news_page
from ui.pages import spread_gate as spread_gate_page
from ui.pages import tickers as tickers_page
from ui.components import REGISTRY as COMPONENTS
from ui.components.cost_distribution import cached_cost_distribution as _cached_cost_distribution
from ui.context import load_scan_context
import snapshot_store as ss
from spread_gate import evaluate_spread_gate
from dailyScaner import market_is_open, proximity_filter, MIN_OI_FOR_MAGNET
from news_service import get_news_sentiment, get_market_news
from best_value import calculate_best_value, build_best_value_df
from distribution import expiry_distribution, prob_beyond_strike
from best_value_archive import (
    ensure_archive_loaded,
    log_best_value_run,
    filter_today,
    add_times_flagged,
    most_persistent_today,
    clear_todays_log,
    archive_csv_bytes,
)
from best_value_ui import (
    CONTRACT_KEY_COL,
    STAR_COL,
    apply_display_keep,
    attach_contract_keys,
    best_value_star,
    contract_key,
    filter_ranked_display,
    greeks_display_columns,
    hidden_delta_band_caption,
    is_fully_extrinsic,
    pending_add_pos_payload,
    ranked_delta_band_mask,
)
from time_stop import format_exit_by_cell
from volume_analysis import (
    get_stock_volume_analysis,
    get_intraday_vwap_state,
    fetch_intraday_vwap_df,
    render_vwap_chart,
    CHART_TIMEFRAMES,
)
from cost_distribution import (
    calculate_cost_distribution,
    render_cost_distribution_chart,
    is_blue_sky_breakout,
    BLUE_SKY_TAG,
)
from strategy_engine import (
    recommend_strategy,
    resolve_has_catalyst,
    resolve_spot_below_support,
    ticker_expected_range,
    attach_optimal_strategy,
)
from zero_dte_gex import (
    calculate_0dte_gamma_flow,
    call_put_progress_bar_html,
    STATE_SQUEEZE,
    STATE_CASCADE,
)
from pov_leakage import (
    fetch_pov_leakage,
    render_pov_leakage_chart,
    URGENCY_TAG,
)
import portfolio_store as portfolio_store
import pre_trade_check as pre_trade_check
from scanner.journal_io import journal_path_for_day, list_journal_days, load_journal_day
from scanner.journal_view import (
    concat_all_fills,
    day_fill_counts,
    load_all_day_frames,
    metrics_from_match,
    positions_frame,
    today_et as journal_today_et,
    try_match,
    closed_on_day,
)


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

def _latest_archive_stamp(ticker: str) -> str | None:
    """Stable fingerprint of the newest archive for auto-refresh detection."""
    files = sorted(glob.glob(f"archive/{ticker}_*.json"), reverse=True)
    if not files:
        return None
    path = files[0]
    try:
        return f"{os.path.basename(path)}|{os.path.getmtime(path):.3f}"
    except OSError:
        return os.path.basename(path)


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


def _latest_weekly_archive(ticker: str) -> dict:
    files = sorted(glob.glob(f"archive_weekly/{ticker}_*.json"), reverse=True)
    if not files:
        return {}
    try:
        with open(files[0]) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _latest_archive(ticker: str = "AAPL") -> dict | None:
    files = sorted(glob.glob(f"archive/{ticker}_*.json"), reverse=True)
    if not files:
        return None
    try:
        with open(files[0]) as f:
            return json.load(f)
    except Exception:
        return None


def _fmt_dollars(v) -> str:
    if v is None:
        return "—"
    if abs(v) >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"${v/1_000:.1f}K"
    return f"${v:.0f}"


def _market_banner():
    """Persistent MARKET CLOSED banner — shown on every tab when session is not open."""
    if _market_is_closed():
        now = _now_et()
        st.error(
            f"🔴  MARKET CLOSED — DATA IS END-OF-DAY  "
            f"({now.strftime('%A %H:%M ET')})",
            icon="🔴",
        )


# ── Sidebar ───────────────────────────────────────────────────────────────────

_ENV_FILE    = os.path.join(_SCANNER_DIR, ".env")


# ── Telegram helpers ──────────────────────────────────────────────────────────

def _load_telegram_config() -> tuple[str | None, str | None]:
    """
    Read TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the .env file.
    Returns (token, chat_id) — either may be None if not configured.
    """
    token: str | None = None
    chat_id: str | None = None
    try:
        with open(_ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if key.strip() == "TELEGRAM_BOT_TOKEN":
                    token = val or None
                elif key.strip() == "TELEGRAM_CHAT_ID":
                    chat_id = val or None
    except FileNotFoundError:
        pass
    return token, chat_id


def _send_telegram(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """
    POST a message to Telegram via the Bot API.
    Uses only stdlib urllib — no extra dependencies.
    Returns (success, error_message).
    """
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200, ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        return False, f"HTTP {e.code}: {detail}"
    except Exception as exc:
        return False, str(exc)


def _tg_pc_bias(pc: float) -> str:
    if pc >= 1.5:  return "▼ BEARISH"
    if pc >= 1.1:  return "▼ MILD BEARISH"
    if pc >= 0.9:  return "─ NEUTRAL"
    if pc >= 0.7:  return "▲ MILD BULLISH"
    return "▲ BULLISH"


def _format_scan_message(
    payload: dict,
    prev_payload: dict | None,
    ticker: str,
    top_n: int,
    include: dict,
    expiry_drill: list[str] | None = None,
) -> str:
    """
    Build a Telegram HTML message from an archive payload.

    include keys:
      session, mtf, magnets, volume_expiry, orb, deltas

    expiry_drill: list of expiry strings (YYYY-MM-DD) to show drill-down details for.
    """
    L: list[str] = []
    vol       = payload.get("volume") or {}
    tfs       = payload.get("timeframes") or {}
    mags      = payload.get("signal_magnets") or {}
    session   = payload.get("session") or {}
    or_data   = payload.get("or_data") or {}
    spot      = float(payload.get("spot") or 0)
    direction = payload.get("direction", "—")
    pc_ratio  = float(vol.get("pc_ratio") or 0)
    all_calls = vol.get("top_calls") or []
    all_puts  = vol.get("top_puts")  or []

    try:
        ts_et = datetime.fromisoformat(payload.get("timestamp", "")).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        ts_et = "—"

    dir_icon = "▲" if "BULL" in direction else ("▼" if "BEAR" in direction else "─")

    # ── Header ────────────────────────────────────────────────────────────────
    L.append(f"<b>📊 {ticker} Options Scanner</b>")
    L.append(f"<i>{ts_et}</i>")
    L.append("")

    # ── Session summary ───────────────────────────────────────────────────────
    if include.get("session", True):
        prev_close = session.get("prev_close")
        open_p     = session.get("open")
        cv = int(vol.get("total_call_vol") or 0)
        pv = int(vol.get("total_put_vol")  or 0)
        pc_bias = _tg_pc_bias(pc_ratio)

        if prev_close:
            chg  = spot - prev_close
            sign = "+" if chg >= 0 else ""
            pct  = chg / prev_close * 100
            delta_str = f"{sign}${chg:.2f} ({sign}{pct:.2f}%)"
        else:
            delta_str = ""

        parts = [f"<b>${spot:.2f}</b>"]
        if delta_str:
            parts.append(delta_str)
        if open_p:
            parts.append(f"Open ${open_p:.2f}")
        if prev_close:
            parts.append(f"Prev close ${prev_close:.2f}")
        L.append(f"💰 <b>{ticker}</b> · " + " · ".join(parts))
        L.append(f"📈 Direction: <b>{dir_icon} {direction}</b>")
        L.append(f"⚖️ P/C <b>{pc_ratio:.2f}</b> {pc_bias} · Calls {cv:,} · Puts {pv:,}")
        L.append("")

    # ── Multi-Timeframe Detail ────────────────────────────────────────────────
    if include.get("mtf", True) and tfs:
        L.append("📊 <b>MULTI-TIMEFRAME</b>")
        rows = ["<pre>TF    RSI    MACD       Vol×"]
        tf_order = ["5M", "10M", "15M", "45M", "1H", "4H", "1D"]
        for tf in tf_order:
            d = tfs.get(tf)
            if not d:
                continue
            rsi  = float(d.get("rsi") or 0)
            hist = float(d.get("hist") or 0)
            vs   = float(d.get("vs") or 0)
            macd_s = f"{hist:+.2f}"
            rows.append(f"{tf:<5} {rsi:>5.1f}  {macd_s:>8}   {vs:.1f}x")
        rows.append("</pre>")
        L.extend(rows)

    # ── The Magnets ───────────────────────────────────────────────────────────
    if include.get("magnets", True):
        for label, emoji, contracts in [
            (f"TOP {top_n} CALLS", "🟢", all_calls[:top_n]),
            (f"TOP {top_n} PUTS",  "🔴", all_puts[:top_n]),
        ]:
            L.append(f"{emoji} <b>{label}</b>")
            rows = ["<pre>Strike  Expiry   Price    Vol      VOI"]
            for c in contracts:
                strike = float(c.get("strike") or 0)
                price  = float(c.get("lastPrice") or 0)
                exp    = c.get("expiry", "")
                exp_s  = exp[5:] if len(exp) >= 7 else exp  # MM-DD
                v      = int(c.get("volume") or 0)
                oi     = max(int(c.get("openInterest") or 0), 1)
                voi    = v / oi
                flag   = "🔥" if voi >= 5 else ("★" if voi >= 2 else " ")
                rows.append(
                    f"${strike:<6.1f} {exp_s:<8} ${price:<6.2f} {v:>7,}  {voi:>5.1f}x{flag}"
                )
            rows.append("</pre>")
            L.extend(rows)

    # ── Volume by Expiry ──────────────────────────────────────────────────────
    if include.get("volume_expiry", True):
        exp_agg: dict[str, dict] = {}
        for c in all_calls:
            exp = c.get("expiry", "?")
            d = exp_agg.setdefault(exp, {
                "cv": 0, "pv": 0, "dte": int(c.get("dte", 0)),
                "call_px": None, "call_top_vol": 0,
                "put_px": None, "put_top_vol": 0,
            })
            v = int(c.get("volume") or 0)
            d["cv"] += v
            if v > d["call_top_vol"]:
                d["call_top_vol"] = v
                d["call_px"] = float(c.get("lastPrice") or 0)
        for c in all_puts:
            exp = c.get("expiry", "?")
            d = exp_agg.setdefault(exp, {
                "cv": 0, "pv": 0, "dte": int(c.get("dte", 0)),
                "call_px": None, "call_top_vol": 0,
                "put_px": None, "put_top_vol": 0,
            })
            v = int(c.get("volume") or 0)
            d["pv"] += v
            if v > d["put_top_vol"]:
                d["put_top_vol"] = v
                d["put_px"] = float(c.get("lastPrice") or 0)

        if exp_agg:
            L.append("📅 <b>VOLUME BY EXPIRY</b>")
            rows = ["<pre>Expiry  DTE CallVol PutVol  P/C  C$    P$"]
            for exp in sorted(exp_agg):
                d   = exp_agg[exp]
                cv_ = d["cv"]; pv_ = d["pv"]; dte = d["dte"]
                if cv_ == 0 and pv_ == 0:
                    continue
                pc_ = pv_ / cv_ if cv_ else 0
                bias_s = _tg_pc_bias(pc_) if cv_ and pv_ else "n/a"
                exp_s  = exp[5:] if len(exp) >= 7 else exp
                cpx = f"${d['call_px']:.2f}" if d["call_px"] is not None else "—"
                ppx = f"${d['put_px']:.2f}"  if d["put_px"]  is not None else "—"
                rows.append(
                    f"{exp_s:<7} {dte:>3}d {cv_:>7,} {pv_:>6,} "
                    f"{pc_:.2f}{bias_s[:2]} {cpx:<6} {ppx}"
                )
            rows.append("</pre>")
            L.extend(rows)

    # ── Opening Range Breakout ────────────────────────────────────────────────
    if include.get("orb", True) and or_data:
        L.append("📍 <b>OPENING RANGE BREAKOUT</b>")
        for tf, d in or_data.items():
            if not isinstance(d, dict):
                continue
            bias   = d.get("bias", "—")
            hi     = d.get("high", 0)
            lo     = d.get("low", 0)
            rng    = d.get("range", 0)
            rng_p  = d.get("range_pct", 0)
            L.append(f"<b>{tf} OR:</b> H ${hi:.2f} · L ${lo:.2f} · Range ${rng:.2f} ({rng_p:.1f}%) → <b>{bias}</b>")

    # ── Deltas vs previous run ────────────────────────────────────────────────
    if include.get("deltas", True) and prev_payload:
        prev_vol  = prev_payload.get("volume") or {}
        prev_spot = float(prev_payload.get("spot") or 0)
        prev_pc   = float(prev_vol.get("pc_ratio") or 0)
        prev_dir  = prev_payload.get("direction", "")
        prev_mags = prev_payload.get("signal_magnets") or {}

        try:
            prev_ts = datetime.fromisoformat(prev_payload.get("timestamp","")).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
        except Exception:
            prev_ts = "prev run"

        delta_lines = []
        # Spot
        if prev_spot:
            sd = spot - prev_spot
            arrow = "↑" if sd > 0 else "↓"
            delta_lines.append(f"{arrow} Spot ${prev_spot:.2f} → ${spot:.2f} ({sd:+.2f})")
        # P/C
        pcd = pc_ratio - prev_pc
        arrow = "↑" if pcd > 0 else "↓"
        delta_lines.append(f"{arrow} P/C {prev_pc:.2f} → {pc_ratio:.2f} ({pcd:+.3f})")
        # Direction
        if prev_dir and prev_dir != direction:
            delta_lines.append(f"🔔 Direction: {prev_dir} → {direction}")
        # Magnet shifts
        for side in ("call", "put"):
            cm = mags.get(side) or {}
            pm = prev_mags.get(side) or {}
            if cm and pm and cm.get("strike") != pm.get("strike"):
                label = "CALL MAGNET" if side == "call" else "PUT MAGNET"
                delta_lines.append(f"🔄 {label}: ${pm.get('strike')} → ${cm.get('strike')} ← STRIKE CHANGE")

        if delta_lines:
            L.append(f"🔁 <b>CHANGES vs {prev_ts}</b>")
            L.extend(delta_lines)
            L.append("")

    # ── Best Value Option ─────────────────────────────────────────────────────
    if include.get("best_value"):
        prev_vol_bv = (prev_payload.get("volume") or {}) if prev_payload else None
        bv_df = _build_best_value_df(vol, spot, prev_vol_bv, min_volume=500)
        if not bv_df.empty and bv_df["Status"].astype(str).str.contains("BEST VALUE", na=False).any():
            from scoring_pool import POOL_0DTE, POOL_1DTE
            L.append("⭐ <b>BEST VALUE</b> <i>(per DTE pool)</i>")
            for pool_name in (POOL_1DTE, POOL_0DTE):
                best_p = bv_df[
                    bv_df["Status"].astype(str).str.contains("BEST VALUE", na=False)
                    & (bv_df.get("pool") == pool_name)
                ]
                if best_p.empty:
                    L.append(f"⭐ <b>{pool_name}</b> — not ranked")
                    continue
                best = best_p.iloc[0]
                voi_b = best["volume"] / max(int(best["openInterest"]), 1)
                dte_s = (
                    f"{int(best['dte'])}d"
                    if "dte" in best.index and pd.notna(best.get("dte"))
                    else "?"
                )
                L.append(
                    f"⭐ <b>{pool_name}</b>  "
                    f"{best['side']} ${best['strike']:.1f}  "
                    f"{best['expiry']} ({dte_s})  "
                    f"score {best['Value_Score']:.2f}  "
                    f"${best['last']:.2f}  vol/OI {voi_b:.1f}x"
                )
            L.append("")

    # ── Expiry drill-down ─────────────────────────────────────────────────────
    if expiry_drill:
        for exp in expiry_drill:
            exp_calls = [c for c in all_calls if c.get("expiry") == exp]
            exp_puts  = [c for c in all_puts  if c.get("expiry") == exp]
            if not exp_calls and not exp_puts:
                continue
            dte = (exp_calls or exp_puts)[0].get("dte", "?")
            pc_calls_vol = sum(int(c.get("volume") or 0) for c in exp_calls)
            pc_puts_vol  = sum(int(c.get("volume") or 0) for c in exp_puts)
            exp_pc = pc_puts_vol / pc_calls_vol if pc_calls_vol else 0
            L.append(f"🔍 <b>EXPIRY DRILL-DOWN: {exp} ({dte}d) · P/C {exp_pc:.2f}</b>")
            rows = ["<pre>Side   Strike   Vol      VOI"]
            for side_label, contracts in [("CALL", exp_calls), ("PUT", exp_puts)]:
                for c in contracts[:5]:
                    strike = float(c.get("strike") or 0)
                    v      = int(c.get("volume") or 0)
                    oi     = max(int(c.get("openInterest") or 0), 1)
                    voi    = v / oi
                    flag   = "🔥" if voi >= 5 else ("★" if voi >= 2 else " ")
                    rows.append(f"{side_label:<6} ${strike:<7.1f} {v:>7,}  {voi:.1f}x{flag}")
            rows.append("</pre>")
            L.extend(rows)

    return "\n".join(L)


# Module-level background-scan state keyed by ticker.
# {ticker: {"running": bool, "last_ok": bool|None, "last_ts": str|None, "t0": float}}

def _bg_scan_worker(ticker: str) -> None:
    """Runs in a daemon thread — never blocks the Streamlit event loop."""
    ok, _ = _run_daily_scanner(ticker)
    s = _bg_state(ticker)
    s["running"] = False
    s["last_ok"] = ok
    s["last_ts"] = _now_et().strftime("%H:%M ET")
    if ok:
        _scan_archive_metadata.clear()   # next fragment fire will show fresh data


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


def _render_settings(cfg: dict) -> None:
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
                },
                expiry_drill=selected_expiries or None,
            )
            ok, err = _send_telegram(tg_token, tg_chat, msg)
            if ok:
                st.success("Sent ✈️")
            else:
                st.error(f"Failed: {err}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Latest Run (full rich report from most recent archive JSON)
# ══════════════════════════════════════════════════════════════════════════════

def _load_archive_chain(ticker: str = "AAPL") -> tuple[pd.DataFrame, dict | None]:
    """Return per-contract DataFrame + raw payload for the latest daily archive."""
    files = sorted(glob.glob(f"archive/{ticker}_*.json"), reverse=True)
    if not files:
        return pd.DataFrame(), None
    try:
        with open(files[0]) as f:
            payload = json.load(f)
    except Exception:
        return pd.DataFrame(), None

    vol_block = payload.get("volume", {})
    rows: list[dict] = []
    for side, key in [("call", "top_calls"), ("put", "top_puts")]:
        for c in vol_block.get(key, []):
            vol  = c.get("volume")  or 0
            oi   = c.get("openInterest") or 0
            last = float(c.get("lastPrice") or 0.0)
            vol_int = int(vol) if not (isinstance(vol, float) and vol != vol) else 0
            oi_int  = int(oi)  if not (isinstance(oi,  float) and oi  != oi)  else 0
            rows.append({
                "side": side, "strike": float(c.get("strike", 0)),
                "expiry": c.get("expiry", ""), "dte": int(c.get("dte", 0)),
                "last": last, "volume": vol_int, "openInterest": oi_int,
                "premium": last * vol_int * 100,
            })
    if not rows:
        return pd.DataFrame(), payload
    return pd.DataFrame(rows), payload


# ══════════════════════════════════════════════════════════════════════════════
# Best Value Option — pure scoring logic + render helpers
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Scanner runs (archive)
# ══════════════════════════════════════════════════════════════════════════════

def _check_card(name: str, passed: bool | None, summary: str, detail: str):
    """
    Render one checklist card matching the screenshot style:
    name / PASS-FAIL badge / summary line / expandable detail.
    passed=None → "—" (field missing from this archive version).
    """
    if passed is None:
        icon, badge_color, arrow = "—", "#555", ""
    elif passed:
        icon, badge_color, arrow = "✅", "#00c853", "↑ "
    else:
        icon, badge_color, arrow = "❌", "#d50000", "↓ "

    badge_html = (
        f'<span style="font-size:2rem;font-weight:900;color:{badge_color}">'
        f'{"PASS" if passed else ("FAIL" if passed is not None else "N/A")}'
        f'</span>'
    )
    summary_color = "#00c853" if passed else ("#d50000" if passed is not None else "#888")

    st.markdown(f"**{name}**")
    st.markdown(badge_html, unsafe_allow_html=True)
    st.markdown(
        f'<span style="color:{summary_color};font-size:0.9rem">{arrow}{summary}</span>',
        unsafe_allow_html=True,
    )
    with st.popover("detail", use_container_width=True):
        st.markdown(detail, unsafe_allow_html=True)
    st.write("")


def _rsi_label(v) -> str:
    if v is None:
        return "—"
    v = float(v)
    if v <= 35:   return f"<span style='color:#00c853'>OVERSOLD ({v:.0f})</span>"
    if v >= 70:   return f"<span style='color:#d50000'>OVERBOUGHT ({v:.0f})</span>"
    if v >= 65:   return f"<span style='color:#ff6d00'>HIGH ({v:.0f})</span>"
    if v < 45:    return f"<span style='color:#ff5252'>BEARISH ({v:.0f})</span>"
    if v > 55:    return f"<span style='color:#69f0ae'>BULLISH ({v:.0f})</span>"
    return f"<span style='color:#9e9e9e'>NEUTRAL ({v:.0f})</span>"


def _render_daily_run(payload: dict, spot: float | str, run_time: str):
    """Display archived daily scanner data — no pass/fail verdicts computed here."""
    direction = payload.get("direction", "—")
    or_data   = payload.get("or_data") or {}
    tfs       = payload.get("timeframes") or {}
    vol       = payload.get("volume") or {}
    magnets   = payload.get("signal_magnets") or {}
    or_15m    = or_data.get("15M") or {}
    or_5m     = or_data.get("5M")  or {}
    pc_ratio  = vol.get("pc_ratio")
    tc        = int(vol.get("total_call_vol") or 0)
    tp        = int(vol.get("total_put_vol")  or 0)

    # ── Direction banner (from archive, not re-evaluated) ─────────────────────
    dir_color   = {"BULLISH": "#00c853", "BEARISH": "#d50000"}.get(direction, "#9e9e9e")
    dir_icon    = "▲" if direction == "BULLISH" else "▼" if direction == "BEARISH" else "─"
    hist_suffix = " (historical)" if _market_is_closed() else ""
    st.markdown(
        f'<div style="background:#1a1a2e;padding:0.75rem 1.5rem;border-radius:8px;margin-bottom:0.5rem">'
        f'<span style="font-size:1.4rem;font-weight:900;color:{dir_color}">'
        f'{dir_icon} {direction}{hist_suffix}</span>'
        f'&ensp;<span style="color:#eee">Spot ${spot}</span>'
        f'&ensp;<span style="color:#aaa">P/C {pc_ratio:.2f}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spot",        f"${spot}")
    m2.metric("Call Volume", f"{tc:,}")
    m3.metric("Put Volume",  f"{tp:,}")
    m4.metric("P/C Ratio",   f"{pc_ratio:.2f}" if pc_ratio is not None else "—")

    # ── Signal magnets ────────────────────────────────────────────────────────
    mc1, mc2 = st.columns(2)
    for col, side, color, label in [
        (mc1, "call", "#00c853", "🟢 CALL MAGNET"),
        (mc2, "put",  "#d50000", "🔴 PUT MAGNET"),
    ]:
        m = magnets.get(side) or {}
        with col:
            if m:
                v   = int(m.get("volume") or 0)
                oi  = max(int(m.get("openInterest") or 0), 1)
                iv  = float(m.get("impliedVolatility") or 0)
                st.markdown(
                    f'<div style="border-left:4px solid {color};padding:0.4rem 0.8rem">'
                    f'<b style="color:{color}">{label}</b><br>'
                    f'${m.get("strike","?")} &nbsp; exp {m.get("expiry","?")} &nbsp; DTE {m.get("dte","?")}<br>'
                    f'Vol {v:,} &nbsp; OI {oi:,} &nbsp; Vol/OI <b>{v/oi:.1f}x</b> &nbsp; IV {iv:.1%}'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── Multi-timeframe table ─────────────────────────────────────────────────
    st.markdown("**Multi-Timeframe**")
    tf_rows = []
    for tf in ["5M", "10M", "15M", "45M", "1H", "4H", "1D"]:
        d    = tfs.get(tf) or {}
        rsi  = d.get("rsi")
        hist = d.get("hist")
        vs   = d.get("vs")
        tf_rows.append({
            "TF":        tf,
            "RSI":       _rsi_plain(rsi),
            "MACD":      (f"{'BULLISH' if (hist or 0) > 0 else 'BEARISH'} [{hist:+.4f}]"
                          if hist is not None else "—"),
            "Vol Spike": f"{vs:.2f}×" if vs is not None else "—",
            "Support":   f"${d.get('support', '—')}",
            "Resist":    f"${d.get('resist', '—')}",
        })
    st.dataframe(pd.DataFrame(tf_rows), use_container_width=True, hide_index=True)

    # ── Opening Range ─────────────────────────────────────────────────────────
    st.markdown("**Opening Range**")
    or1, or2 = st.columns(2)
    for col, tf_key, or_tf in [(or1, "5M", or_5m), (or2, "15M", or_15m)]:
        with col:
            if or_tf:
                bias  = or_tf.get("bias", "—")
                bdir  = or_tf.get("bias_dir", "")
                bcolor = "#00c853" if bdir == "bull" else "#d50000" if bdir == "bear" else "#aaa"
                st.markdown(
                    f'<div style="border:1px solid #333;padding:0.5rem 0.75rem;border-radius:6px">'
                    f'<b>{tf_key} OR</b> ({or_tf.get("open_time","?")} ET)'
                    f'&ensp;<span style="color:{bcolor};font-weight:bold">{bias}</span><br>'
                    f'<span style="color:#888;font-size:0.8rem">'
                    f'O ${or_tf.get("open",0):.2f} '
                    f'H ${or_tf.get("high",0):.2f} '
                    f'L ${or_tf.get("low",0):.2f} '
                    f'Rng ${or_tf.get("range",0):.2f}</span>'
                    f'</div>',
                    unsafe_allow_html=True,
                )

    # ── No checklist in daily archive ─────────────────────────────────────────
    st.info(
        "No checklist in daily archive — "
        "run the **weekly scanner** for the 5-point gate evaluation.",
        icon="ℹ️",
    )


def _render_weekly_run(payload: dict, spot: float | str, run_time: str):
    """Display archived weekly scanner data — score from archive, no verdicts recomputed."""
    macro  = payload.get("macro") or {}
    oi     = payload.get("oi_structure") or {}
    daily  = payload.get("daily") or {}
    weekly = payload.get("weekly") or {}
    score  = payload.get("checklist_score")
    e_date = payload.get("earnings_date")
    e_days = payload.get("earnings_days")
    thesis = payload.get("thesis", "")

    spy   = macro.get("SPY")  or {}
    qqq   = macro.get("QQQ")  or {}
    vix_d = macro.get("^VIX") or {}

    # ── Score banner — verbatim from archive ──────────────────────────────────
    if score is not None:
        if score >= 4:
            bg, icon = "#1a3a1a", "✅"
        elif score >= 3:
            bg, icon = "#2a2a1a", "⚠️"
        else:
            bg, icon = "#3a1a1a", "🔴"
        st.markdown(
            f'<div style="background:{bg};padding:0.75rem 1.5rem;border-radius:8px;margin-bottom:0.5rem">'
            f'<span style="font-size:1.4rem;font-weight:900;color:#fff">'
            f'{icon} Checklist score: {score}/5</span><br>'
            f'<span style="color:#aaa;font-size:0.85rem">'
            f'Weekly scanner · {run_time} ET · Spot ${spot}</span>'
            f'</div>',
            unsafe_allow_html=True,
        )
    else:
        st.markdown(f"**Weekly scanner · {run_time} ET · Spot ${spot}**")

    st.caption("Score and all field values read verbatim from archive — no criteria recomputed here.")

    # ── Data table: values as recorded by the scanner ─────────────────────────
    rows = []

    # Macro
    for ticker, td in [("SPY", spy), ("QQQ", qqq)]:
        if td.get("spot"):
            above = td.get("above_ema20")
            rows.append({
                "Field":  f"{ticker} vs EMA20",
                "Value":  f"${td['spot']:.2f} / EMA20 ${td.get('ema20','—')}",
                "Detail": f"above_ema20={above}  5d={td.get('ret5d','—'):+.1f}%  RSI={td.get('rsi','—')}",
            })

    vix_spot = vix_d.get("spot")
    if vix_spot is not None:
        rows.append({
            "Field":  "VIX",
            "Value":  f"{vix_spot:.2f}",
            "Detail": f"EMA20 {vix_d.get('ema20','—')}  5d {vix_d.get('ret5d','—'):+.1f}%",
        })

    d_spot = daily.get("spot")
    if d_spot:
        rows.append({
            "Field":  "AAPL Daily",
            "Value":  f"${d_spot:.2f} / EMA14 ${daily.get('ema14','—')}",
            "Detail": f"RSI {daily.get('rsi','—')}  MACD hist {daily.get('macd_hist','—')}  "
                      f"EMA28 ${daily.get('ema28','—')}  EMA50 ${daily.get('ema50','—')}",
        })

    w_spot = weekly.get("spot")
    if w_spot:
        rows.append({
            "Field":  "AAPL Weekly",
            "Value":  f"${w_spot:.2f} / EMA14 ${weekly.get('ema14','—')}",
            "Detail": f"RSI {weekly.get('rsi','—')}  MACD hist {weekly.get('macd_hist','—')}",
        })

    pc_oi = oi.get("pc_oi")
    if pc_oi is not None:
        rows.append({
            "Field":  "P/C OI (near-term)",
            "Value":  f"{pc_oi:.3f}",
            "Detail": f"Call OI {int(oi.get('total_call_oi',0)):,}  "
                      f"Put OI {int(oi.get('total_put_oi',0)):,}  "
                      f"Max pain ${oi.get('max_pain','—')}  IV skew {oi.get('iv_skew','—')}%",
        })

    if e_date is not None:
        rows.append({
            "Field":  "Earnings",
            "Value":  f"{e_date}  ({e_days}d away)",
            "Detail": "",
        })

    if rows:
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if thesis:
        with st.popover("📄 Weekly thesis", use_container_width=True):
            st.markdown(thesis)


def _render_expiry_vol_table(vol_curr: dict, vol_prev: dict | None, overall_pc: float):
    """
    Volume-by-Expiry table aggregated from top_calls / top_puts.
    CALL VOL and PUT VOL cells are colored green (↑) / red (↓) vs prev run.
    """
    # ── Aggregate current run ─────────────────────────────────────────────────
    curr: dict[str, dict] = {}
    for side_key, vol_key in [("call_vol", "top_calls"), ("put_vol", "top_puts")]:
        for c in (vol_curr.get(vol_key) or []):
            exp = c.get("expiry", "?")
            dte = int(c.get("dte", 0))
            v   = int(c.get("volume") or 0)
            if exp not in curr:
                curr[exp] = {"dte": dte, "call_vol": 0, "put_vol": 0}
            curr[exp][side_key] += v

    # ── Aggregate previous run ────────────────────────────────────────────────
    prev: dict[str, dict] = {}
    if vol_prev:
        for side_key, vol_key in [("call_vol", "top_calls"), ("put_vol", "top_puts")]:
            for c in (vol_prev.get(vol_key) or []):
                exp = c.get("expiry", "?")
                v   = int(c.get("volume") or 0)
                prev.setdefault(exp, {"call_vol": 0, "put_vol": 0})[side_key] += v

    if not curr:
        st.caption("No volume data in this archive.")
        return

    # ── Build rows ────────────────────────────────────────────────────────────
    rows = []
    for exp in sorted(curr):
        d   = curr[exp]
        cv, pv = d["call_vol"], d["put_vol"]
        dte = d["dte"]

        # Guard: zero on either side → genuine data gap, not a valid P/C
        data_gap = (cv == 0 or pv == 0)
        if data_gap:
            pc   = None
            bias = ""
        else:
            pc = pv / cv
            if pc < 0.7:   bias = "▲ BULLISH"
            elif pc < 0.9: bias = "▲ MILD BULLISH"
            elif pc < 1.1: bias = "- NEUTRAL"
            elif pc < 1.5: bias = "▼ MILD BEARISH"
            else:          bias = "▼ BEARISH"

        notable = (
            abs(pc - overall_pc) > 0.25
            if (pc is not None and overall_pc > 0)
            else False
        )
        pd_     = prev.get(exp, {})
        cv_d    = cv - pd_.get("call_vol", 0) if vol_prev else None
        pv_d    = pv - pd_.get("put_vol",  0) if vol_prev else None

        def _ds(v):
            if v is None: return ""
            if v > 0: return f"+{v:,}"
            if v < 0: return f"{v:,}"
            return "·0"

        rows.append({
            "EXPIRY":   exp,
            "DTE":      f"{dte}d",
            "CALL VOL": f"{cv:,}",
            "PUT VOL":  f"{pv:,}",
            "P/C":      f"{pc:.2f}" if pc is not None else "n/a",
            "BIAS":     bias,
            "NOTABLE":  "⚠ possible data gap" if data_gap else ("◄ notable" if notable else ""),
            "CALL Δ":   _ds(cv_d),
            "PUT Δ":    _ds(pv_d),
            "_cv_d":    cv_d,
            "_pv_d":    pv_d,
        })

    df = pd.DataFrame(rows)
    display_cols = ["EXPIRY","DTE","CALL VOL","PUT VOL","P/C","BIAS","NOTABLE","CALL Δ","PUT Δ"]
    if not vol_prev:
        display_cols = [c for c in display_cols if c not in ("CALL Δ","PUT Δ")]
    disp = df[display_cols].copy()

    def _style_bias(val: str) -> str:
        if "BULL" in str(val): return "color:#00c853;font-weight:bold"
        if "BEAR" in str(val): return "color:#d50000;font-weight:bold"
        return "color:#9e9e9e"

    def _style_delta(val: str) -> str:
        s = str(val)
        if s.startswith("+"): return "color:#00c853;font-weight:bold"
        if s.startswith("-"): return "color:#d50000;font-weight:bold"
        return "color:#666"

    def _style_vol_cell(col_name):
        """Color CALL/PUT VOL cells green/red based on delta sign."""
        delta_col = "_cv_d" if col_name == "CALL VOL" else "_pv_d"
        def fn(val):
            # get the delta from the full df by row position
            return ""   # placeholder; handled by _style_cv / _style_pv below
        return fn

    styled = disp.style.map(_style_bias, subset=["BIAS"])
    if vol_prev:
        styled = styled.map(_style_delta, subset=["CALL Δ","PUT Δ"])
        # Color the actual volume cells by sign of their delta
        def _cv_style(val):
            idx = disp["CALL VOL"].tolist().index(val) if val in disp["CALL VOL"].tolist() else -1
            if idx >= 0:
                d = df["_cv_d"].iloc[idx]
                if d is not None and d > 0: return "color:#00c853;font-weight:bold"
                if d is not None and d < 0: return "color:#d50000;font-weight:bold"
            return ""
        def _pv_style(val):
            idx = disp["PUT VOL"].tolist().index(val) if val in disp["PUT VOL"].tolist() else -1
            if idx >= 0:
                d = df["_pv_d"].iloc[idx]
                if d is not None and d > 0: return "color:#00c853;font-weight:bold"
                if d is not None and d < 0: return "color:#d50000;font-weight:bold"
            return ""
        styled = styled.map(_cv_style, subset=["CALL VOL"]).map(_pv_style, subset=["PUT VOL"])

    st.dataframe(styled, use_container_width=True, hide_index=True)


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — Ticker Manager
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Spread gate
# ══════════════════════════════════════════════════════════════════════════════

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
        _market_banner()
        flow_page.render(cfg)
    elif page == "archive":
        _market_banner()
        archive_page.render(cfg)
    elif page == "spread_gate":
        _market_banner()
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
        _render_settings(cfg)
    elif page in COMPONENTS:
        ctx = load_scan_context(cfg["ticker"], top_n=cfg["top_n"])
        if ctx is None:
            st.info(f"No archive data found for {cfg['ticker']}. "
                    "Run the scanner first or pick a different ticker.")
        else:
            COMPONENTS[page].render(ctx)


if __name__ == "__main__" or True:
    main()
