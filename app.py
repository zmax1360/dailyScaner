"""
app.py — AAPL Options Scanner Dashboard
Display-only layer. All analytical numbers come from dailyScaner.py
functions or archive JSON files. No indicators recomputed here.
"""

import glob
import json
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

import data_adapter
import snapshot_store as ss
from spread_gate import evaluate_spread_gate
from dailyScaner import market_is_open, proximity_filter, MIN_OI_FOR_MAGNET
from news_service import get_news_sentiment, get_market_news
from best_value import calculate_best_value, build_best_value_df
from best_value_archive import (
    ensure_archive_loaded,
    log_best_value_run,
    filter_today,
    add_times_flagged,
    most_persistent_today,
    clear_todays_log,
    archive_csv_bytes,
)
from best_value_ui import pending_add_pos_payload, style_best_value_rows
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

ET = ZoneInfo("America/New_York")

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


def _streamlit_ge(major: int, minor: int) -> bool:
    try:
        parts = [int(x) for x in st.__version__.split(".")[:2]]
        return tuple(parts) >= (major, minor)
    except Exception:
        return False


def _choice_control(
    label: str,
    options: list[str],
    *,
    default: str | None = None,
    key: str | None = None,
    help: str | None = None,
) -> str:
    """
    Prefer st.pills / st.segmented_control (Streamlit ≥1.40); fall back to
    horizontal radio on older versions.
    """
    default = default if default in options else (options[0] if options else "")
    if hasattr(st, "pills"):
        val = st.pills(label, options, default=default, key=key, help=help)
        return val if val is not None else default
    if hasattr(st, "segmented_control"):
        val = st.segmented_control(
            label, options, default=default, key=key, help=help,
        )
        return val if val is not None else default
    idx = options.index(default) if default in options else 0
    return st.radio(label, options, index=idx, horizontal=True, key=key, help=help)


def _main_tab_labels() -> list[str]:
    """Material icons when supported; emoji fallback otherwise."""
    if _streamlit_ge(1, 40):
        return [
            ":material/candlestick_chart: Options Flow",
            ":material/database: Scanner Archive",
            ":material/science: Spread Gate",
            ":material/list_alt: Tickers",
            ":material/newspaper: Market News",
            ":material/menu_book: Journal",
        ]
    return [
        "📈 Options Flow",
        "📋 Scanner Archive",
        "🔬 Spread Gate",
        "📁 Tickers",
        "📰 Market News",
        "📓 Journal",
    ]


# ── Shared helpers ─────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(ET)


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


def _market_is_closed() -> bool:
    """True when the regular session is not open right now."""
    return not market_is_open(_now_et())


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

_SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))
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
            has_dvol = "dVol" in bv_df.columns
            L.append("⭐ <b>BEST VALUE OPTION</b>")
            ranked = (
                bv_df[bv_df["Value_Score"].notna()]
                .sort_values("Value_Score", ascending=False)
                .head(3)
            )
            hdr = "Side  Strike   Exp    Score  Price  VOI"
            if has_dvol:
                hdr += "    ΔVol"
            rows = [f"<pre>{hdr}"]
            for _, r in ranked.iterrows():
                voi  = r["volume"] / max(int(r["openInterest"]), 1)
                star = " ⭐" if "BEST VALUE" in str(r.get("Status") or "") else ""
                exp_s = r["expiry"][5:] if len(r["expiry"]) >= 7 else r["expiry"]
                line = (
                    f"{r['side']:<5} ${r['strike']:<7.1f} {exp_s:<6} "
                    f"{r['Value_Score']:.4f} ${r['last']:.2f}  {voi:.1f}x"
                )
                if has_dvol and pd.notna(r.get("dVol")):
                    line += f"  {int(r['dVol']):+,}"
                line += star
                rows.append(line)
            rows.append("</pre>")
            L.extend(rows)
            best = bv_df[bv_df["Status"].astype(str).str.contains("BEST VALUE", na=False)].iloc[0]
            voi_b = best["volume"] / max(int(best["openInterest"]), 1)
            L.append(
                f"→ <b>{best['side']} ${best['strike']:.1f}</b> "
                f"exp {best['expiry']} · score {best['Value_Score']:.4f} · "
                f"${best['last']:.2f} · {voi_b:.1f}×"
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


def _run_daily_scanner(ticker: str = "AAPL") -> tuple[bool, str]:
    """
    Invoke dailyScaner.py <ticker> with the same Python that runs Streamlit.
    Returns (success, combined_output).
    """
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, "dailyScaner.py", ticker.upper()],
            cwd=_SCANNER_DIR,
            capture_output=True,
            text=True,
            timeout=1800,   # 30-min hard cap
        )
        elapsed = time.time() - t0
        out = result.stdout + ("\n" + result.stderr if result.stderr.strip() else "")
        return result.returncode == 0, f"[{elapsed:.0f}s]\n{out}"
    except subprocess.TimeoutExpired:
        return False, f"{ticker} scanner timed out after 30 minutes."
    except Exception as exc:
        return False, f"Could not launch scanner for {ticker}: {exc}"


_EXCLUDED_FILE = os.path.join(_SCANNER_DIR, "tickers_excluded.json")


def _load_excluded() -> set[str]:
    """Load the set of tickers excluded from auto-scan. Never raises."""
    try:
        with open(_EXCLUDED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _save_excluded(excluded: set[str]) -> None:
    """Persist the excluded set. Never raises."""
    try:
        with open(_EXCLUDED_FILE, "w") as f:
            json.dump(sorted(excluded), f)
    except Exception:
        pass


_SCHED_CFG_FILE = os.path.join(_SCANNER_DIR, "scheduler_config.json")
_SCHED_DEFAULTS: dict = {
    "market_open":          "09:30",
    "market_close":         "16:00",
    "post_close_buffer_min": 15,
    "default_interval_min": 5,
    "notify_telegram":      True,
    "tickers":              {},
}


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


def _discover_all_tickers() -> list[str]:
    """All tickers with at least one daily archive file (including excluded)."""
    seen: set[str] = set()
    for fpath in glob.glob("archive/*.json"):
        name  = os.path.basename(fpath)
        parts = name.split("_")
        if len(parts) >= 3:
            seen.add(parts[0].upper())
    return sorted(seen)


def _discover_tickers() -> list[str]:
    """
    Active tickers — have archive data AND are not in the excluded list.
    Used by the sidebar selector and auto-scan watcher.
    Falls back to ['AAPL'] if nothing is active.
    """
    excluded = _load_excluded()
    active   = [t for t in _discover_all_tickers() if t not in excluded]
    return active or ["AAPL"]


# Module-level background-scan state keyed by ticker.
# {ticker: {"running": bool, "last_ok": bool|None, "last_ts": str|None, "t0": float}}
_BG: dict[str, dict] = {}

def _bg_state(ticker: str) -> dict:
    """Return (creating if absent) the state dict for a given ticker."""
    if ticker not in _BG:
        _BG[ticker] = {"running": False, "last_ok": None, "last_ts": None, "t0": 0.0}
    return _BG[ticker]

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

        subprocess.Popen(
            [python, script_path],
            cwd=_SCANNER_DIR,
            stdout=open(os.path.join(_SCANNER_DIR, script.replace(".py", ".log")), "a"),
            stderr=subprocess.STDOUT,
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


def _sidebar() -> dict:
    with st.sidebar:
        st.title("📊 Options Scanner")
        st.caption("Display layer — results from archive JSONs")
        st.divider()

        # ── Ticker selector ───────────────────────────────────────────────
        known_tickers = _discover_tickers()
        default_idx   = known_tickers.index("AAPL") if "AAPL" in known_tickers else 0
        focus_ticker  = st.selectbox(
            "Focus ticker",
            known_tickers,
            index=default_idx,
            help="All tabs show data for this ticker. Add new tickers in the Tickers tab.",
        )

        st.divider()

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

        # Auto-refresh when scheduler (or another process) writes a new archive
        _watch_archive_auto_refresh(focus_ticker)

        # Auto-launch scheduler / telegram bot once per session (status alert is at page top)
        _ensure_services()

        st.divider()
        st.subheader("Flow filters")
        min_dte  = st.number_input("Min DTE", min_value=0, value=1, step=1)
        top_n    = st.number_input(
            "Top N results",
            min_value=1,
            max_value=30,
            value=5,
            step=1,
            help="How many rows to show in Best Value and how many calls/puts in The Magnets",
        )
        sort_by  = _choice_control(
            "Sort by",
            ["Volume", "Premium $", "Strike"],
            default="Volume",
            key="flow_sort_by",
        )

        st.divider()
        latest = _latest_archive(focus_ticker)
        if latest:
            ts = datetime.fromisoformat(latest["timestamp"]).astimezone(ET)
            st.caption(f"Last archive: {ts.strftime('%Y-%m-%d %H:%M ET')}")
            st.caption(f"Spot at run: ${latest.get('spot', '—')}")

        # ── Telegram push ─────────────────────────────────────────────────
        st.divider()
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

    return {
        "run":            run_scan,
        "ticker":         focus_ticker,
        "min_dte":        min_dte,
        "top_n":          int(top_n),
        "sort_by":        sort_by,
        "latest_archive": latest,
    }


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


def _render_pc_term_chart(chart_pc: dict[str, float | None]) -> None:
    """
    Altair bar chart — P/C ratio per expiry.
    - Bars blue when < 1 (call-heavy), red when ≥ 1 (put-heavy).
    - Dashed reference line at y = 1.0.
    - ⚠ text marker above any expiry whose P/C is None (data gap).
    - Expiries with either side's volume = 0 are rendered as gap markers only.
    Data comes exclusively from chart_pc (already aggregated from archive).
    All ET-aware dates are preserved as-is from the archive expiry strings.
    """
    import altair as alt

    valid = {exp: pc for exp, pc in chart_pc.items() if pc is not None}
    gaps  = [exp for exp, pc in chart_pc.items() if pc is None]

    layers = []

    if valid:
        bar_df = pd.DataFrame([
            {"expiry": exp, "pc": pc, "side": "Put-heavy" if pc >= 1 else "Call-heavy"}
            for exp, pc in sorted(valid.items())
        ])
        bars = (
            alt.Chart(bar_df)
            .mark_bar(cornerRadiusTopLeft=3, cornerRadiusTopRight=3)
            .encode(
                x=alt.X("expiry:O",
                         sort=sorted(valid.keys()),
                         axis=alt.Axis(labelAngle=-40, title=None)),
                y=alt.Y("pc:Q",
                         title="P/C ratio",
                         scale=alt.Scale(domainMin=0)),
                color=alt.Color(
                    "side:N",
                    scale=alt.Scale(
                        domain=["Call-heavy", "Put-heavy"],
                        range=["#1565c0", "#c62828"],
                    ),
                    legend=alt.Legend(title=None, orient="top-right"),
                ),
                tooltip=[
                    alt.Tooltip("expiry:O", title="Expiry"),
                    alt.Tooltip("pc:Q", format=".3f", title="P/C"),
                    alt.Tooltip("side:N", title="Bias"),
                ],
            )
        )
        layers.append(bars)

    # Dashed reference line at y = 1.0
    rule = (
        alt.Chart(pd.DataFrame({"y": [1.0]}))
        .mark_rule(strokeDash=[6, 3], color="#888", strokeWidth=1.5)
        .encode(y="y:Q")
    )
    layers.append(rule)

    # ⚠ gap markers
    if gaps:
        gap_df = pd.DataFrame({"expiry": sorted(gaps), "label": ["⚠"] * len(gaps), "y": [0.05] * len(gaps)})
        gap_marks = (
            alt.Chart(gap_df)
            .mark_text(fontSize=14, color="#ff6d00", dy=-6)
            .encode(
                x=alt.X("expiry:O", sort=sorted(chart_pc.keys())),
                y=alt.Y("y:Q"),
                text=alt.Text("label:N"),
                tooltip=[alt.Tooltip("expiry:O", title="Data gap — zero volume on one side")],
            )
        )
        layers.append(gap_marks)

    if layers:
        chart = alt.layer(*layers).properties(height=200)
        st.altair_chart(chart, use_container_width=True)
        gap_note = f"  ·  ⚠ = data gap: {', '.join(sorted(gaps))}" if gaps else ""
        st.caption(f"< 1 call-heavy · > 1 put-heavy{gap_note}")


def _rsi_plain(rsi: float | None) -> str:
    """Plain text RSI label (no HTML) for use in DataFrames."""
    if rsi is None:
        return "—"
    if rsi >= 80:  return f"OVERBOUGHT ({rsi:.1f})"
    if rsi >= 60:  return f"BULLISH ({rsi:.1f})"
    if rsi >= 45:  return f"NEUTRAL ({rsi:.1f})"
    if rsi >= 30:  return f"BEARISH ({rsi:.1f})"
    return f"OVERSOLD ({rsi:.1f})"


def _voi_style(val: str) -> str:
    """Pandas Styler cell function — heat-gradient background for VOL/OI column."""
    try:
        v = float(str(val).replace("x", "").replace("🔥", "").strip())
    except (ValueError, AttributeError):
        return ""
    if v >= 100: return "background-color:#7f0000;color:#fff;font-weight:bold"
    if v >= 50:  return "background-color:#b71c1c;color:#fff;font-weight:bold"
    if v >= 20:  return "background-color:#d50000;color:#fff;font-weight:bold"
    if v >= 10:  return "background-color:#e65100;color:#fff;font-weight:bold"
    if v >= 5:   return "background-color:#ff6d00;color:#fff;font-weight:bold"
    if v >= 2:   return "background-color:#ffa726;color:#000;font-weight:bold"
    return ""


def _contracts_table(contracts: list, n: int = 5) -> pd.DataFrame | None:
    """Build a styled DataFrame from a top_calls / top_puts list."""
    rows = []
    for c in contracts[:n]:
        v   = int(c.get("volume") or 0)
        oi  = max(int(c.get("openInterest") or 0), 1)
        voi = v / oi
        rows.append({
            "EXPIRY":  c.get("expiry", ""),
            "STRIKE":  f"${float(c.get('strike', 0)):.1f}",
            "PRICE":   f"${float(c.get('lastPrice') or 0):.2f}",
            "VOLUME":  f"{v:,}",
            "OI":      f"{oi:,}",
            "VOL/OI":  f"{voi:.2f}x 🔥" if voi >= 2 else f"{voi:.2f}x",
            "_voi":    voi,
        })
    if not rows:
        return None
    df = pd.DataFrame(rows)
    styled = df.drop(columns=["_voi"]).style.map(_voi_style, subset=["VOL/OI"])
    return styled


# ══════════════════════════════════════════════════════════════════════════════
# Best Value Option — pure scoring logic + render helpers
# ══════════════════════════════════════════════════════════════════════════════

def compute_daily_bias(
    open_px: float,
    high_px: float,
    low_px: float,
    close_px: float,
) -> dict:
    """
    Pure candlestick-structure bias from daily OHLC.
    Returns {candle_body, body_ratio, daily_bias}.
    """
    body = close_px - open_px
    rng  = high_px - low_px
    if rng == 0 or abs(rng) < 1e-12:
        ratio = 0.0
    else:
        ratio = body / rng

    if ratio <= -0.60:
        bias = "HEAVY BEARISH"
    elif ratio >= 0.60:
        bias = "HEAVY BULLISH"
    else:
        bias = "NEUTRAL"

    return {
        "candle_body": round(body, 4),
        "body_ratio":  round(ratio, 4),
        "daily_bias":  bias,
        "open":  open_px,
        "high":  high_px,
        "low":   low_px,
        "close": close_px,
    }


def _resolve_daily_bias(ticker: str, session: dict, spot: float) -> dict | None:
    """
    Prefer live daily OHLC via data_adapter; fall back to archive session + spot.
    Returns compute_daily_bias(...) result, or None if insufficient data.
    """
    ohlc = data_adapter.fetch_daily_ohlc(ticker)
    if ohlc:
        return compute_daily_bias(
            ohlc["open"], ohlc["high"], ohlc["low"], ohlc["close"]
        )

    # Archive fallback — session block from dailyScaner
    open_px = session.get("open")
    high_px = session.get("day_high")
    low_px  = session.get("day_low")
    if open_px is None or high_px is None or low_px is None or not spot:
        return None
    return compute_daily_bias(
        float(open_px), float(high_px), float(low_px), float(spot)
    )


def compute_market_state(macro: dict) -> dict:
    """
    Pure macro gravity assessment from SPY / QQQ / VIX daily bars.

    BEARISH DRAG:     SPY or QQQ body_ratio <= -0.60  OR  VIX day-change > +5%
    BULLISH TAILWIND: SPY and QQQ body_ratio >= +0.60 AND VIX day-change < -2%
    NEUTRAL:          otherwise
    """
    spy_b = compute_daily_bias(
        macro["SPY"]["open"], macro["SPY"]["high"],
        macro["SPY"]["low"],  macro["SPY"]["close"],
    )
    qqq_b = compute_daily_bias(
        macro["QQQ"]["open"], macro["QQQ"]["high"],
        macro["QQQ"]["low"],  macro["QQQ"]["close"],
    )

    vix = macro["VIX"]
    vix_close = float(vix["close"])
    vix_prev  = vix.get("prev_close")
    if vix_prev and float(vix_prev) > 0:
        vix_chg_pct = (vix_close - float(vix_prev)) / float(vix_prev) * 100.0
    else:
        vix_chg_pct = 0.0

    spy_r = spy_b["body_ratio"]
    qqq_r = qqq_b["body_ratio"]

    if spy_r <= -0.60 or qqq_r <= -0.60 or vix_chg_pct > 5.0:
        state = "BEARISH DRAG"
    elif spy_r >= 0.60 and qqq_r >= 0.60 and vix_chg_pct < -2.0:
        state = "BULLISH TAILWIND"
    else:
        state = "NEUTRAL"

    def _day_chg(bar: dict) -> float | None:
        prev = bar.get("prev_close")
        close = float(bar.get("close") or 0)
        if prev and float(prev) > 0 and close > 0:
            return (close - float(prev)) / float(prev) * 100.0
        return None

    return {
        "market_state": state,
        "spy_close":    float(macro["SPY"]["close"]),
        "qqq_close":    float(macro["QQQ"]["close"]),
        "spy_ratio":    spy_r,
        "qqq_ratio":    qqq_r,
        "spy_chg_pct":  _day_chg(macro["SPY"]),
        "qqq_chg_pct":  _day_chg(macro["QQQ"]),
        "vix_close":    vix_close,
        "vix_chg_pct":  round(vix_chg_pct, 2),
    }


def _resolve_market_state() -> dict | None:
    """Fetch SPY/QQQ/VIX via data_adapter and compute Market_State. None on failure."""
    macro = data_adapter.fetch_macro_snapshot()
    if not macro:
        return None
    return compute_market_state(macro)


@st.cache_data(ttl=300)
def _cached_news_sentiment(ticker: str) -> dict:
    """
    Cached news fetch (5 min TTL) so UI refreshes do not spam the API.
    Always returns a dict — news_service never raises.
    """
    return get_news_sentiment(ticker)


@st.cache_data(ttl=300)
def _cached_market_news(tickers: tuple[str, ...], limit: int = 15) -> list[dict]:
    """Cached multi-ticker headline timeline for the Market News tab."""
    return get_market_news(list(tickers), limit=limit)


@st.cache_data(ttl=120)
def _cached_volume_analysis(ticker: str) -> dict:
    """Cached intraday buy/sell/neutral volume breakdown (2 min TTL)."""
    return get_stock_volume_analysis(ticker)


@st.cache_data(ttl=60)
def _cached_vwap_state(ticker: str) -> dict:
    """Cached 5m VWAP reclaim state (1 min TTL)."""
    return get_intraday_vwap_state(ticker)


@st.cache_data(ttl=60)
def _cached_vwap_chart_df(ticker: str, timeframe: str = "5M") -> pd.DataFrame:
    """Cached OHLC + VWAP for the candlestick chart at the selected timeframe."""
    return fetch_intraday_vwap_df(ticker, timeframe=timeframe)


@st.cache_data(ttl=60)
def _cached_pov_leakage(ticker: str) -> tuple[pd.DataFrame, dict]:
    """Cached 5m POV participation metrics + urgency flag (1 min TTL)."""
    return fetch_pov_leakage(ticker, last_session_only=True)


@st.cache_data(ttl=3600)
def _cached_cost_distribution(ticker: str, spot: float | None = None) -> dict:
    """Cached 6-month cost distribution / overhead supply profile (1 hr TTL)."""
    return calculate_cost_distribution(ticker, days=180, spot=spot)


def _fmt_compact_shares(n: float | int) -> str:
    """Format share counts like 31.93M / 695.71K."""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return "—"
    abs_v = abs(v)
    if abs_v >= 1_000_000:
        return f"{v / 1_000_000:.2f}M"
    if abs_v >= 1_000:
        return f"{v / 1_000:.2f}K"
    return f"{v:.0f}"


def _render_volume_analysis(
    ticker: str,
    *,
    compact: bool = False,
    vol_curr: dict | None = None,
) -> None:
    """Broker-style Buy / Sell / Neutral volume doughnut + header metrics."""
    import plotly.graph_objects as go
    from volume_analysis import _classify_tick_rule

    with st.container():
        st.markdown("#### 📊 Volume Analysis" if compact else "### 📊 Volume Analysis")
        data = _cached_volume_analysis(ticker)
        total = int(data.get("Total_Volume") or 0)
        mode = "stock"  # stock tick-rule vs options call/put fallback

        # If the cached fetch was empty (rate-limit / 1m gap), rebuild from the
        # same 5M bars the VWAP chart already uses — usually already warm.
        if total <= 0:
            try:
                chart_df = _cached_vwap_chart_df(ticker, "5M")
                if chart_df is not None and not chart_df.empty:
                    data = _classify_tick_rule(chart_df)
                    data["ticker"] = ticker
                    data["source"] = "vwap_chart_5m_fallback"
                    total = int(data.get("Total_Volume") or 0)
            except Exception:
                pass

        # Archive options volume — always available after a scan, no extra Yahoo hit
        if total <= 0 and isinstance(vol_curr, dict):
            cv = int(vol_curr.get("total_call_vol") or 0)
            pv = int(vol_curr.get("total_put_vol") or 0)
            if cv + pv > 0:
                mode = "options"
                total = cv + pv
                data = {
                    "Average_Price": 0.0,
                    "Total_Count": (
                        len(vol_curr.get("top_calls") or [])
                        + len(vol_curr.get("top_puts") or [])
                    ),
                    "Total_Volume": total,
                    "Buy_Volume": cv,       # Call flow proxy
                    "Sell_Volume": pv,      # Put flow proxy
                    "Neutral_Volume": 0,
                    "source": "archive_options_volume",
                }

        if total <= 0:
            try:
                _cached_volume_analysis.clear()
            except Exception:
                pass
            err = (data or {}).get("error")
            msg = "No intraday volume data available for this ticker right now."
            if err:
                msg += f" ({err})"
            st.caption(msg)
            return

        avg_px = float(data.get("Average_Price") or 0)
        count  = int(data.get("Total_Count") or 0)
        buy    = int(data.get("Buy_Volume") or 0)
        sell   = int(data.get("Sell_Volume") or 0)
        neut   = int(data.get("Neutral_Volume") or 0)

        if mode == "options":
            labels = ["Call Volume", "Put Volume"]
            values = [buy, sell]
            colors = ["#00C853", "#FF1744"]
            title = "Call vs Put Volume"
            if compact:
                st.caption(
                    f"Options flow · Vol {_fmt_compact_shares(total)} "
                    f"(Yahoo stock bars unavailable)"
                )
            else:
                st.caption("Showing options call/put volume from latest scan (stock bars unavailable).")
        else:
            labels = ["Buy Volume", "Sell Volume", "Neutral Volume"]
            values = [buy, sell, neut]
            colors = ["#00C853", "#FF1744", "#B0BEC5"]
            title = "Buy vs Sell Volume"
            if compact:
                st.caption(
                    f"Avg ${avg_px:,.2f} · Count {_fmt_compact_shares(count)} · "
                    f"Vol {_fmt_compact_shares(total)}"
                )
            else:
                h1, h2, h3 = st.columns(3)
                h1.metric("Average Price", f"${avg_px:,.2f}")
                h2.metric("Total Count", _fmt_compact_shares(count))
                h3.metric("Total Volume (Shares)", _fmt_compact_shares(total))

        plot_labels, plot_values, plot_colors = [], [], []
        for lab, val, col in zip(labels, values, colors):
            if val > 0:
                plot_labels.append(lab)
                plot_values.append(val)
                plot_colors.append(col)

        if not plot_values:
            st.caption("Volume bars present but buy/sell split is empty.")
            return

        try:
            base = (st.get_option("theme.base") or "light").lower()
        except Exception:
            base = "light"
        center_color = "#eeeeee" if base == "dark" else "#212121"
        title_color = "#e0e0e0" if base == "dark" else "#424242"

        fig = go.Figure(
            data=[
                go.Pie(
                    labels=plot_labels,
                    values=plot_values,
                    hole=0.7,
                    marker=dict(colors=plot_colors, line=dict(width=0)),
                    textinfo="none",
                    hovertemplate="%{label}<br>%{value:,.0f}"
                                  "<br>%{percent}<extra></extra>",
                    sort=False,
                )
            ]
        )
        fig.update_layout(
            title=dict(
                text=title,
                font=dict(size=13 if compact else 14, color=title_color),
                x=0.5,
                xanchor="center",
            ),
            showlegend=False,
            paper_bgcolor="rgba(0,0,0,0)",
            plot_bgcolor="rgba(0,0,0,0)",
            margin=dict(t=36, b=8, l=8, r=8),
            height=220 if compact else 280,
            annotations=[
                dict(
                    text=f"<b>{_fmt_compact_shares(total)}</b><br>"
                         f"<span style='font-size:11px;opacity:0.7'>"
                         f"{'contracts' if mode == 'options' else 'shares'}</span>",
                    x=0.5, y=0.5, showarrow=False,
                    font=dict(size=14 if compact else 16, color=center_color),
                )
            ],
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

        if mode == "options":
            buy_pct = buy / total * 100 if total else 0
            sell_pct = sell / total * 100 if total else 0
            st.markdown(
                f"🟢 **Calls** `{_fmt_compact_shares(buy)}` ({buy_pct:.0f}%) · "
                f"🔴 **Puts** `{_fmt_compact_shares(sell)}` ({sell_pct:.0f}%)"
            )
        else:
            buy_pct  = buy / total * 100 if total else 0
            sell_pct = sell / total * 100 if total else 0
            neut_pct = neut / total * 100 if total else 0
            st.markdown(
                f"🟢 **Buy** `{_fmt_compact_shares(buy)}` ({buy_pct:.0f}%) · "
                f"🔴 **Sell** `{_fmt_compact_shares(sell)}` ({sell_pct:.0f}%) · "
                f"⚪ **Neutral** `{_fmt_compact_shares(neut)}` ({neut_pct:.0f}%)"
            )


def _build_best_value_df(
    vol_curr: dict,
    spot: float,
    vol_prev: dict | None,
    min_volume: int = 500,
    daily_bias: str | None = None,
    market_state: str | None = None,
    news_bias: str | None = None,
    vwap_state: str | None = None,
    profited_shares_pct: float | None = None,
    *,
    upper_1sd: float | None = None,
    lower_1sd: float | None = None,
    optimal_strategy: str | None = None,
    has_catalyst: bool = False,
    spot_below_support: bool = False,
    odte_info: dict | None = None,
    pov_info: dict | None = None,
) -> pd.DataFrame:
    """Build + score via shared best_value engine (expiry/0DTE + phantom-ΔVol safe)."""
    return build_best_value_df(
        vol_curr, spot, vol_prev,
        min_volume=min_volume,
        daily_bias=daily_bias,
        market_state=market_state,
        news_bias=news_bias,
        vwap_state=vwap_state,
        profited_shares_pct=profited_shares_pct,
        upper_1sd=upper_1sd,
        lower_1sd=lower_1sd,
        optimal_strategy=optimal_strategy,
        has_catalyst=has_catalyst,
        spot_below_support=spot_below_support,
        odte_info=odte_info,
        pov_info=pov_info,
    )


_SURGE_THRESH = 0.15
_EXIT_THRESH  = -0.15
_EXTENDED_MOVE_PCT = 0.035   # ≥3.5% off intraday low → caution buying Calls
_RUNNER_VEL_THRESH = 0.20    # strong velocity → hold runner (with daily bias)
_SCALE_PREMIUM_PCT = 0.25    # +25% off entry → scale 50%
_STOP_LOSS_PCT = -0.15       # portfolio personal stop

_PORTFOLIO_COLS = list(portfolio_store.EDITOR_COLS)
_PORTFOLIO_LEDGER_COLS = list(portfolio_store.LEDGER_COLS)


def _ensure_portfolio_df() -> None:
    """Initialize / hydrate the portfolio ledger from disk into session_state."""
    if "portfolio_df" not in st.session_state:
        st.session_state["portfolio_df"] = portfolio_store.load_portfolio()
    else:
        df = st.session_state["portfolio_df"]
        if not isinstance(df, pd.DataFrame):
            st.session_state["portfolio_df"] = portfolio_store.load_portfolio()
            return
        for col in _PORTFOLIO_LEDGER_COLS:
            if col not in df.columns:
                df[col] = pd.NA
        st.session_state["portfolio_df"] = df[
            [c for c in _PORTFOLIO_LEDGER_COLS if c in df.columns]
        ].copy()
        for col in _PORTFOLIO_LEDGER_COLS:
            if col not in st.session_state["portfolio_df"].columns:
                st.session_state["portfolio_df"][col] = pd.NA
        st.session_state["portfolio_df"] = st.session_state["portfolio_df"][
            _PORTFOLIO_LEDGER_COLS
        ]


def _persist_portfolio_editor(edited: pd.DataFrame) -> None:
    """Merge editor columns into the ledger and save to disk."""
    _ensure_portfolio_df()
    prev = st.session_state["portfolio_df"]
    if not isinstance(edited, pd.DataFrame):
        return
    out = edited.copy()
    for col in _PORTFOLIO_COLS:
        if col not in out.columns:
            out[col] = pd.NA
    # Preserve marks + entry timestamps for rows that still match
    meta_map: dict[tuple, tuple] = {}
    if isinstance(prev, pd.DataFrame) and not prev.empty:
        for _, r in prev.iterrows():
            k = (
                str(r.get("Ticker") or "").upper(),
                str(r.get("Side") or "").upper(),
                round(float(r["Strike"]), 4) if pd.notna(r.get("Strike")) else None,
                str(r.get("Expiry") or ""),
            )
            meta_map[k] = (
                r.get("Mark_Price"),
                r.get("Mark_Updated_At"),
                r.get("Entry_At"),
            )
    marks, marked_at, entry_ats = [], [], []
    for _, r in out.iterrows():
        k = (
            str(r.get("Ticker") or "").upper(),
            str(r.get("Side") or "").upper(),
            round(float(r["Strike"]), 4) if pd.notna(r.get("Strike")) else None,
            str(r.get("Expiry") or ""),
        )
        mp, ma, ea = meta_map.get(k, (pd.NA, pd.NA, pd.NA))
        marks.append(mp)
        marked_at.append(ma)
        entry_ats.append(ea)
    out["Mark_Price"] = marks
    out["Mark_Updated_At"] = marked_at
    out["Entry_At"] = entry_ats
    for col in _PORTFOLIO_LEDGER_COLS:
        if col not in out.columns:
            out[col] = pd.NA
    out = out[_PORTFOLIO_LEDGER_COLS]
    st.session_state["portfolio_df"] = out
    portfolio_store.save_portfolio(out)


def evaluate_portfolio(
    portfolio_df: pd.DataFrame,
    live_scanner_df: pd.DataFrame,
) -> pd.DataFrame:
    """
    Merge open positions with live scanner quotes and attach personal exit signals.

    live_scanner_df expected columns (case-insensitive / aliases accepted):
      Ticker, Side, Strike, Expiry, Current_Price (or last), Score_Velocity
    """
    if portfolio_df is None or portfolio_df.empty:
        return pd.DataFrame()

    port = portfolio_df.copy()
    for col in _PORTFOLIO_COLS:
        if col not in port.columns:
            port[col] = pd.NA
    port = port[_PORTFOLIO_COLS].copy()

    # Drop blank ticker rows from the editor
    port["Ticker"] = port["Ticker"].astype(str).str.strip().str.upper()
    port = port[port["Ticker"].notna() & (port["Ticker"] != "") & (port["Ticker"] != "NAN")]
    if port.empty:
        return pd.DataFrame()

    port["Side"] = port["Side"].astype(str).str.strip().str.upper()
    port["Side"] = port["Side"].replace({"C": "CALL", "P": "PUT"})
    port["Strike"] = pd.to_numeric(port["Strike"], errors="coerce")
    port["Expiry"] = port["Expiry"].astype(str).str.strip()
    port["Quantity"] = pd.to_numeric(port["Quantity"], errors="coerce")
    port["Entry_Price"] = pd.to_numeric(port["Entry_Price"], errors="coerce")

    live = pd.DataFrame() if live_scanner_df is None else live_scanner_df.copy()
    if not live.empty:
        # Normalize live column names
        rename = {}
        cols_lower = {c.lower(): c for c in live.columns}
        if "current_price" not in cols_lower and "last" in cols_lower:
            rename[cols_lower["last"]] = "Current_Price"
        if "side" in cols_lower and cols_lower["side"] != "Side":
            rename[cols_lower["side"]] = "Side"
        if "strike" in cols_lower and cols_lower["strike"] != "Strike":
            rename[cols_lower["strike"]] = "Strike"
        if "expiry" in cols_lower and cols_lower["expiry"] != "Expiry":
            rename[cols_lower["expiry"]] = "Expiry"
        if "ticker" in cols_lower and cols_lower["ticker"] != "Ticker":
            rename[cols_lower["ticker"]] = "Ticker"
        if "score_velocity" in cols_lower and cols_lower["score_velocity"] != "Score_Velocity":
            rename[cols_lower["score_velocity"]] = "Score_Velocity"
        live = live.rename(columns=rename)

        for col, default in [
            ("Ticker", ""), ("Side", ""), ("Strike", float("nan")),
            ("Expiry", ""), ("Current_Price", float("nan")),
            ("Score_Velocity", float("nan")),
        ]:
            if col not in live.columns:
                live[col] = default

        live["Ticker"] = live["Ticker"].astype(str).str.strip().str.upper()
        live["Side"] = live["Side"].astype(str).str.strip().str.upper()
        live["Strike"] = pd.to_numeric(live["Strike"], errors="coerce")
        live["Expiry"] = live["Expiry"].astype(str).str.strip()
        live["Current_Price"] = pd.to_numeric(live["Current_Price"], errors="coerce")
        live["Score_Velocity"] = pd.to_numeric(live["Score_Velocity"], errors="coerce")

        live = live[
            ["Ticker", "Side", "Strike", "Expiry", "Current_Price", "Score_Velocity"]
        ].drop_duplicates(
            subset=["Ticker", "Side", "Strike", "Expiry"], keep="first"
        )

        merged = port.merge(
            live,
            on=["Ticker", "Side", "Strike", "Expiry"],
            how="left",
        )
    else:
        merged = port.copy()
        merged["Current_Price"] = float("nan")
        merged["Score_Velocity"] = float("nan")

    def _pnl(row) -> float:
        entry = row.get("Entry_Price")
        cur = row.get("Current_Price")
        if pd.isna(entry) or pd.isna(cur) or float(entry) == 0:
            return float("nan")
        return (float(cur) - float(entry)) / float(entry)

    merged["PnL_Percentage"] = merged.apply(_pnl, axis=1)

    def _personal_signal(row) -> str:
        pnl = row.get("PnL_Percentage")
        if pd.notna(pnl):
            if float(pnl) >= _SCALE_PREMIUM_PCT:
                return "💰 SCALE 50% (LOCK PROFIT)"
            if float(pnl) <= _STOP_LOSS_PCT:
                return "🚨 STOP-LOSS TRIGGERED"
        vel = row.get("Score_Velocity")
        if pd.notna(vel):
            if float(vel) <= _EXIT_THRESH:
                return "⚠️ MOMENTUM DYING"
            if float(vel) >= _SURGE_THRESH:
                return "🚀 HOLD"
        if pd.isna(row.get("Current_Price")):
            return "— (no live quote)"
        return "WATCH"

    merged["Personal_Signal"] = merged.apply(_personal_signal, axis=1)
    return merged


def _build_live_scanner_df_for_portfolio(
    ticker: str,
    vol_curr: dict,
    spot: float,
    vol_prev: dict | None,
    daily_bias: str | None,
    market_state: str | None,
    news_bias: str | None,
) -> pd.DataFrame:
    """
    Prefer the scored Best Value snapshot stashed this refresh; otherwise
    rebuild scores using the velocity cache (without mutating it).
    """
    stash_key = f"bv_live_scanner_{ticker}"
    stashed = st.session_state.get(stash_key)
    if isinstance(stashed, pd.DataFrame) and not stashed.empty:
        return stashed.copy()

    df = build_best_value_df(
        vol_curr, spot, vol_prev,
        min_volume=500,
        daily_bias=daily_bias,
        market_state=market_state,
        news_bias=news_bias,
    )
    if df.empty:
        return pd.DataFrame()

    df = df[df["Value_Score"].notna()].copy()
    state_key = f"bv_prev_scores_{ticker}"
    # Use pre-refresh scores if Best Value already overwrote cache this run
    vel_key = f"bv_velocity_snapshot_{ticker}"
    vel_map: dict = st.session_state.get(vel_key) or {}
    prev_scores: dict = st.session_state.get(state_key, {})

    def _ck(row) -> tuple:
        return (str(row["side"]), float(row["strike"]), str(row["expiry"]))

    def _vel(row) -> float:
        k = _ck(row)
        if k in vel_map:
            return float(vel_map[k])
        if pd.isna(row["Value_Score"]):
            return float("nan")
        prev = prev_scores.get(k)
        if prev is None:
            return 0.0
        return round(float(row["Value_Score"]) - float(prev), 4)

    df["Score_Velocity"] = df.apply(_vel, axis=1)
    df["Ticker"] = ticker.upper()
    df["Current_Price"] = df["last"]
    return df[
        ["Ticker", "side", "strike", "expiry", "Current_Price", "Score_Velocity"]
    ].rename(columns={"side": "Side", "strike": "Strike", "expiry": "Expiry"})


def _render_portfolio_manager(
    ticker: str,
    vol_curr: dict,
    spot: float,
    vol_prev: dict | None,
    daily_bias: str | None = None,
    market_state: str | None = None,
    news_bias: str | None = None,
    *,
    compact: bool = False,
) -> None:
    """Interactive open-positions ledger + personalized exit signals."""
    _ensure_portfolio_df()

    st.markdown("#### 💼 My Open Positions" if compact else "### 💼 My Open Positions")
    if not compact:
        st.caption(
            "Add from Best Value with **＋**, close with **−** (enter exit price). "
            "Signals use **your Entry_Price** vs live scanner "
            "(+25% scale / −15% stop). Marks refresh every scan."
        )
    else:
        st.caption("＋ adds · − closes (exit price) · marks refresh each scan")

    editor_src = st.session_state["portfolio_df"][_PORTFOLIO_COLS].copy()
    _editor_kw: dict = dict(
        num_rows="dynamic",
        use_container_width=True,
        key="portfolio_editor",
        column_config={
            "Ticker": st.column_config.TextColumn("Ticker", width="small"),
            "Side": st.column_config.SelectboxColumn(
                "Side", options=["CALL", "PUT"], required=False, width="small",
            ),
            "Strike": st.column_config.NumberColumn(
                "Strike", min_value=0.0, format="%.1f", width="small",
            ),
            "Expiry": st.column_config.TextColumn(
                "Expiry", help="YYYY-MM-DD", width="small",
            ),
            "Quantity": st.column_config.NumberColumn(
                "Qty", min_value=0, step=1, format="%d", width="small",
            ),
            "Entry_Price": st.column_config.NumberColumn(
                "Entry $", min_value=0.0, format="%.2f", width="small",
            ),
        },
    )
    if compact:
        _editor_kw["height"] = 220
    edited = st.data_editor(editor_src, **_editor_kw)
    # Persist edits across refreshes + disk
    if isinstance(edited, pd.DataFrame):
        _persist_portfolio_editor(edited)

    live = _build_live_scanner_df_for_portfolio(
        ticker, vol_curr, spot, vol_prev,
        daily_bias=daily_bias,
        market_state=market_state,
        news_bias=news_bias,
    )
    # Refresh Mark_Price from live quotes every run; EOD force when closed
    marked = portfolio_store.apply_live_marks(
        st.session_state["portfolio_df"],
        live,
        force_eod=_market_is_closed(),
    )
    st.session_state["portfolio_df"] = marked

    scored = evaluate_portfolio(st.session_state["portfolio_df"], live)
    if scored.empty:
        st.caption("No open positions — use ＋ on Best Value, or add a row above.")
        if (
            isinstance(st.session_state.get("portfolio_df"), pd.DataFrame)
            and not st.session_state["portfolio_df"].empty
        ):
            _render_close_position_controls(
                st.session_state["portfolio_df"],
                scored,
                compact=compact,
            )
        _render_closed_positions_summary(compact=compact)
        return

    # Prefer live Current_Price; fall back to persisted Mark_Price
    if (
        not scored.empty
        and "Mark_Price" in st.session_state["portfolio_df"].columns
    ):
        scored = scored.merge(
            st.session_state["portfolio_df"][
                ["Ticker", "Side", "Strike", "Expiry", "Mark_Price"]
            ],
            on=["Ticker", "Side", "Strike", "Expiry"],
            how="left",
            suffixes=("", "_dup"),
        )
        if "Mark_Price_dup" in scored.columns:
            scored["Mark_Price"] = scored["Mark_Price"].fillna(scored["Mark_Price_dup"])
            scored = scored.drop(columns=["Mark_Price_dup"], errors="ignore")
        scored["Current_Price"] = scored["Current_Price"].fillna(scored["Mark_Price"])
        scored["PnL_Percentage"] = scored.apply(
            lambda r: (
                (float(r["Current_Price"]) - float(r["Entry_Price"]))
                / float(r["Entry_Price"])
                if pd.notna(r.get("Current_Price"))
                and pd.notna(r.get("Entry_Price"))
                and float(r["Entry_Price"]) != 0
                else float("nan")
            ),
            axis=1,
        )

    # Net $ PnL across positions with live quotes
    net_pnl = 0.0
    net_ok = False
    for _, r in scored.iterrows():
        entry = r.get("Entry_Price")
        cur = r.get("Current_Price")
        qty = r.get("Quantity")
        if pd.notna(entry) and pd.notna(cur) and pd.notna(qty):
            net_pnl += (float(cur) - float(entry)) * float(qty) * 100.0
            net_ok = True
    if net_ok:
        st.metric("Net P&L (est.)", f"${net_pnl:+,.0f}")
    if _market_is_closed():
        st.caption("Market closed — position marks snapshotted for EOD.")

    # Active exit signals summary
    hot = scored[
        scored["Personal_Signal"].astype(str).str.contains(
            "SCALE|STOP-LOSS|MOMENTUM|HOLD", regex=True, na=False
        )
    ]
    if not hot.empty:
        for _, r in hot.head(5).iterrows():
            st.markdown(
                f"**{r.get('Ticker')} {r.get('Side')} ${float(r.get('Strike') or 0):.0f}** — "
                f"{r.get('Personal_Signal')}"
            )

    disp = scored.copy()
    disp["PnL %"] = disp["PnL_Percentage"].apply(
        lambda x: f"{x:+.1%}" if pd.notna(x) else "—"
    )
    disp["Current $"] = disp["Current_Price"].apply(
        lambda x: f"${x:.2f}" if pd.notna(x) else "—"
    )
    disp["Entry $"] = disp["Entry_Price"].apply(
        lambda x: f"${x:.2f}" if pd.notna(x) else "—"
    )
    show_cols = ["Ticker", "Side", "Strike", "PnL %", "Personal_Signal"]
    if not compact:
        show_cols = [
            "Ticker", "Side", "Strike", "Expiry", "Quantity",
            "Entry $", "Current $", "PnL %", "Personal_Signal",
        ]
    show = disp[show_cols].rename(columns={"Personal_Signal": "Signal"})

    def _pnl_bg(val: str) -> str:
        s = str(val)
        if s.startswith("+"):
            return "background-color:#1b5e20;color:#ffffff;font-weight:bold"
        if s.startswith("-"):
            return "background-color:#b71c1c;color:#ffffff;font-weight:bold"
        return ""

    def _signal_fg(val: str) -> str:
        s = str(val)
        if "SCALE" in s:
            return "color:#ffd600;font-weight:bold"
        if "STOP-LOSS" in s:
            return "color:#ff1744;font-weight:bold"
        if "MOMENTUM DYING" in s:
            return "color:#ffab00;font-weight:bold"
        if "HOLD" in s:
            return "color:#00e676;font-weight:bold"
        return "color:#9e9e9e"

    styled = (
        show.style
        .map(_pnl_bg, subset=["PnL %"])
        .map(_signal_fg, subset=["Signal"])
    )
    _df_kw: dict = dict(use_container_width=True, hide_index=True)
    if compact:
        _df_kw["height"] = 180
    st.dataframe(styled, **_df_kw)

    # − close controls (exit price required)
    _render_close_position_controls(
        st.session_state["portfolio_df"],
        scored,
        compact=compact,
    )
    _render_closed_positions_summary(compact=compact)


def _render_close_position_controls(
    portfolio_df: pd.DataFrame,
    scored: pd.DataFrame,
    *,
    compact: bool = False,
) -> None:
    """− button per open row → ask exit price → move to closed ledger."""
    if portfolio_df is None or portfolio_df.empty:
        return

    pdf = portfolio_df.reset_index(drop=True)
    # Prefer live/current mark as default exit
    mark_by_key: dict[tuple, float] = {}
    if scored is not None and not scored.empty:
        for _, r in scored.iterrows():
            k = (
                str(r.get("Ticker") or "").upper(),
                str(r.get("Side") or "").upper(),
                round(float(r["Strike"]), 4) if pd.notna(r.get("Strike")) else None,
                str(r.get("Expiry") or ""),
            )
            px = r.get("Current_Price")
            if pd.isna(px):
                px = r.get("Mark_Price")
            if pd.notna(px) and float(px) > 0:
                mark_by_key[k] = float(px)

    st.markdown("**Close position**" if not compact else "**− Close**")
    for i, r in pdf.iterrows():
        ticker = str(r.get("Ticker") or "").upper()
        if not ticker or ticker == "NAN":
            continue
        side = str(r.get("Side") or "").upper()
        strike = float(r["Strike"]) if pd.notna(r.get("Strike")) else 0.0
        expiry = str(r.get("Expiry") or "")
        entry = float(r["Entry_Price"]) if pd.notna(r.get("Entry_Price")) else 0.0
        qty = int(float(r["Quantity"])) if pd.notna(r.get("Quantity")) else 1
        k = (ticker, side, round(strike, 4), expiry)
        default_px = mark_by_key.get(k)
        if default_px is None and pd.notna(r.get("Mark_Price")):
            default_px = float(r["Mark_Price"])
        if default_px is None or default_px <= 0:
            default_px = entry if entry > 0 else 0.01

        c1, c2 = st.columns([0.35, 3.65] if compact else [0.25, 4.75])
        with c1:
            if st.button(
                "−",
                key=f"close_pos_{i}_{ticker}_{side}_{strike}_{expiry}",
                help=f"Close {side} ${strike:.1f} — enter exit price",
            ):
                st.session_state["_pending_close_pos"] = {
                    "index": int(i),
                    "Ticker": ticker,
                    "Side": side,
                    "Strike": strike,
                    "Expiry": expiry,
                    "Quantity": qty,
                    "Entry_Price": entry,
                    "default_price": float(default_px),
                }
                st.rerun()
        with c2:
            st.caption(
                f"{ticker} {side} ${strike:.1f} · {expiry} · "
                f"qty {qty} · entry ${entry:.2f}"
            )

    pending = st.session_state.get("_pending_close_pos")
    if not pending:
        return

    with st.form(key="close_pos_form"):
        st.markdown(
            f"Close **{pending['Side']} ${float(pending['Strike']):.1f}** "
            f"exp `{pending['Expiry']}` · entry "
            f"**${float(pending['Entry_Price']):.2f}**"
        )
        exit_px = st.number_input(
            "Exit price ($)",
            min_value=0.01,
            value=max(0.01, float(pending.get("default_price") or 0.01)),
            step=0.05,
            format="%.2f",
            help="The premium you came out at",
        )
        c1, c2 = st.columns(2)
        with c1:
            ok = st.form_submit_button("Confirm close", type="primary")
        with c2:
            cancel = st.form_submit_button("Cancel")

    if cancel:
        st.session_state.pop("_pending_close_pos", None)
        st.rerun()
    if ok:
        try:
            open_df, closed = portfolio_store.close_position(
                int(pending["index"]),
                float(exit_px),
                portfolio_df=st.session_state["portfolio_df"],
            )
            st.session_state["portfolio_df"] = open_df
            st.session_state.pop("_pending_close_pos", None)
            pnl_d = closed.get("PnL_Dollars")
            pnl_p = closed.get("PnL_Pct")
            pnl_txt = ""
            if pnl_d is not None and pnl_p is not None:
                pnl_txt = f" · realized ${pnl_d:+,.0f} ({pnl_p:+.1%})"
            st.success(
                f"Closed {closed['Side']} ${float(closed['Strike']):.1f} "
                f"@ ${float(exit_px):.2f}{pnl_txt}"
            )
            st.rerun()
        except Exception as exc:
            st.error(f"Could not close position: {exc}")


def _render_closed_positions_summary(*, compact: bool = False) -> None:
    """Recent closed trades with exit price."""
    closed = portfolio_store.load_closed()
    if closed is None or closed.empty:
        return
    with st.expander(
        f"Closed trades ({len(closed)})",
        expanded=False,
    ):
        show = closed.tail(15).iloc[::-1].copy()
        show["Entry $"] = show["Entry_Price"].apply(
            lambda x: f"${float(x):.2f}" if pd.notna(x) else "—"
        )
        show["Exit $"] = show["Exit_Price"].apply(
            lambda x: f"${float(x):.2f}" if pd.notna(x) else "—"
        )
        show["PnL %"] = show["PnL_Pct"].apply(
            lambda x: f"{float(x):+.1%}" if pd.notna(x) else "—"
        )
        show["PnL $"] = show["PnL_Dollars"].apply(
            lambda x: f"${float(x):+,.0f}" if pd.notna(x) else "—"
        )
        cols = ["Ticker", "Side", "Strike", "Expiry", "Entry $", "Exit $", "PnL %", "PnL $"]
        if compact:
            cols = ["Ticker", "Side", "Strike", "Exit $", "PnL $"]
        st.dataframe(
            show[cols],
            use_container_width=True,
            hide_index=True,
            height=160 if compact else 220,
        )


def _render_mtf_matrix(tfs: dict, prev_tfs: dict | None = None) -> None:
    """Compact multi-timeframe RSI / MACD matrix for the workspace grid."""
    st.markdown("#### 📊 Multi-Timeframe")
    prev_tfs = prev_tfs or {}
    tf_rows = []
    for tf in ["5M", "10M", "15M", "45M", "1H", "4H", "1D"]:
        d = tfs.get(tf) or {}
        pd_ = prev_tfs.get(tf) or {}
        rsi = d.get("rsi")
        hist = d.get("hist")
        vs = d.get("vs")
        p_rsi = pd_.get("rsi")
        p_hist = pd_.get("hist")
        d_rsi = (rsi - p_rsi) if (rsi is not None and p_rsi is not None) else None
        d_hist = (hist - p_hist) if (hist is not None and p_hist is not None) else None
        tf_rows.append({
            "TF": tf,
            "RSI": _rsi_plain(rsi),
            "ΔRSI": f"{d_rsi:+.1f}" if d_rsi is not None else "—",
            "MACD": f"{hist:+.4f}" if hist is not None else "—",
            "ΔMACD": f"{d_hist:+.4f}" if d_hist is not None else "—",
            "Vol×": f"{vs:.2f}×" if vs is not None else "—",
        })

    df_tf = pd.DataFrame(tf_rows)

    def _delta_style(val: str) -> str:
        s = str(val)
        if s.startswith("+"):
            return "color:#00c853;font-weight:bold"
        if s.startswith("-"):
            return "color:#d50000;font-weight:bold"
        return "color:#666"

    dcols = ["TF", "RSI", "ΔRSI", "MACD", "ΔMACD", "Vol×"]
    if not prev_tfs:
        dcols = ["TF", "RSI", "MACD", "Vol×"]
    styled_tf = df_tf[dcols].style
    if prev_tfs:
        styled_tf = styled_tf.map(_delta_style, subset=["ΔRSI", "ΔMACD"])
    st.dataframe(styled_tf, use_container_width=True, hide_index=True, height=280)


def _render_best_value_panel(
    vol_curr: dict,
    spot: float,
    vol_prev: dict | None,
    ticker: str = "AAPL",
    daily_bias_info: dict | None = None,
    market_state_info: dict | None = None,
    news_bias: str | None = None,
    session_low: float | None = None,
    vwap_info: dict | None = None,
    run_timestamp: str | None = None,
    cost_info: dict | None = None,
    has_catalyst: bool = False,
    spot_below_support: bool = False,
    optimal_strategy: str | None = None,
    upper_1sd: float | None = None,
    lower_1sd: float | None = None,
    odte_info: dict | None = None,
    pov_info: dict | None = None,
    top_n: int = 5,
) -> None:
    """
    Best Value Option Scanner — composite rank of all archive contracts.

    Each refresh:
      1. Scores contracts via calculate_best_value (40% leverage / 60% flow).
      2. Applies daily + macro (SPY/QQQ/VIX) counter-trend penalties.
      3. Applies news_bias ±20% CALL/PUT adjustment when BULLISH/BEARISH.
      4. Computes Score_Velocity = current_score - previous_score (session cache).
      5. Assigns Action_Signal based on velocity thresholds (±0.15).
      6. Extension check: if spot is ≥3.5% off session low, CALL surges become
         "SURGE BUT EXTENDED" and a UI warning is shown.
      7. Overwrites the session-state cache so the next refresh sees fresh prev scores.

    No live fetches in this panel — bias/state are computed upstream and passed in.
    """
    daily_bias   = (daily_bias_info or {}).get("daily_bias")
    market_state = (market_state_info or {}).get("market_state")
    vwap_state   = (vwap_info or {}).get("VWAP_State")
    vwap_px      = (vwap_info or {}).get("VWAP")
    profited_pct = (cost_info or {}).get("Profited_Shares_Pct")
    blue_sky = is_blue_sky_breakout(profited_pct, daily_bias)

    # Extension check: (spot - session_low) / session_low
    extended = False
    intraday_move_pct = None
    if session_low is not None and float(session_low) > 0 and spot > 0:
        intraday_move_pct = (float(spot) - float(session_low)) / float(session_low)
        extended = intraday_move_pct >= _EXTENDED_MOVE_PCT

    c1, c2 = st.columns([3, 1])
    with c2:
        min_vol_input = st.number_input(
            "Min Volume", min_value=0, value=500, step=100,
            key="bv_min_vol",
            help="Contracts below this volume threshold are excluded from scoring.",
        )
    with c1:
        notes = []
        if daily_bias in ("HEAVY BEARISH", "HEAVY BULLISH"):
            notes.append(f"Daily **{daily_bias}** → counter-trend −50%")
        if market_state in ("BEARISH DRAG", "BULLISH TAILWIND"):
            notes.append(f"Macro **{market_state}** → counter-trend −70%")
        if news_bias == "BEARISH":
            notes.append("News **BEARISH** → CALL ×0.8 · PUT ×1.2")
        elif news_bias == "BULLISH":
            notes.append("News **BULLISH** → CALL ×1.2 · PUT ×0.8")
        if vwap_state == "RECLAIMED UP" and daily_bias == "HEAVY BULLISH":
            notes.append("VWAP **RECLAIMED UP** → CALL ×1.5 sniper")
        elif vwap_state == "RECLAIMED DOWN" and daily_bias == "HEAVY BEARISH":
            notes.append("VWAP **RECLAIMED DOWN** → PUT ×1.5 sniper")
        elif vwap_state and vwap_state != "UNKNOWN" and vwap_px is not None:
            notes.append(f"VWAP **{vwap_state}** @ \\${float(vwap_px):.2f}")
        if blue_sky:
            notes.append(
                f"**{BLUE_SKY_TAG}** "
                f"(profited shares {float(profited_pct):.1f}%)"
            )
        note_s = ("  ·  " + "  ·  ".join(notes)) if notes else ""
        show_n = int(max(1, min(30, top_n)))
        st.caption(
            "Ranks every contract by a composite score: "
            "**40% leverage efficiency** (delta × spot ÷ premium)  ·  "
            "**60% flow intensity** (VOL/OI × |ΔVol|).  "
            f"Filters: Volume ≥ {min_vol_input:,} · Price > $0.01  ·  "
            f"Showing top **{show_n}** (sidebar Flow filters)  ·  "
            f"Velocity threshold ±{_SURGE_THRESH:.2f}"
            f"{note_s}"
        )

    if extended and intraday_move_pct is not None:
        st.warning(
            f"⚠️ **EXTENDED MOVE:** Ticker is **+{intraday_move_pct * 100:.1f}%** "
            f"off intraday lows (L \\${float(session_low):.2f} → "
            f"\\${float(spot):.2f}). Exercise caution buying Calls."
        )

    if blue_sky:
        st.success(
            f"**{BLUE_SKY_TAG}** — "
            f"{float(profited_pct):.1f}% of 6-month volume sits below spot "
            f"with Daily Bias HEAVY BULLISH (near-zero overhead supply)."
        )

    if (pov_info or {}).get("urgency"):
        ratio = (pov_info or {}).get("ratio")
        st.error(
            f"**{URGENCY_TAG}** — Magenta over-participation "
            f"({ratio:.2f}× vs 15-bar avg) with price above VWAP. "
            f"Best Value **CALLS** boosted ×1.25."
        )

    # ── Score contracts ───────────────────────────────────────────────────────
    df = _build_best_value_df(
        vol_curr, spot, vol_prev,
        min_volume=int(min_vol_input),
        daily_bias=daily_bias,
        market_state=market_state,
        news_bias=news_bias,
        vwap_state=vwap_state,
        profited_shares_pct=profited_pct,
        upper_1sd=upper_1sd,
        lower_1sd=lower_1sd,
        optimal_strategy=optimal_strategy,
        has_catalyst=has_catalyst,
        spot_below_support=spot_below_support,
        odte_info=odte_info,
        pov_info=pov_info,
    )
    if df.empty:
        st.info(f"No contracts pass the min-volume filter ({min_vol_input:,}). Lower the threshold.")
        return

    # Hide expired / after-hours 0DTE / below-threshold rows (no Value_Score)
    df = df[df["Value_Score"].notna()].copy()
    if df.empty:
        st.info(
            "No eligible contracts to score right now "
            "(expired and after-hours 0DTE are excluded)."
        )
        return

    has_dvol = "dVol" in df.columns

    # ── Velocity tracking via session_state ───────────────────────────────────
    # Cache key is per-ticker so switching tickers doesn't bleed scores across.
    state_key = f"bv_prev_scores_{ticker}"
    prev_scores: dict[tuple, float] = st.session_state.get(state_key, {})

    def _contract_key(row) -> tuple:
        """Unique key: (side, strike_float, expiry_str)."""
        return (str(row["side"]), float(row["strike"]), str(row["expiry"]))

    def _velocity(row) -> float:
        if pd.isna(row["Value_Score"]):
            return float("nan")
        prev = prev_scores.get(_contract_key(row))
        # New contract this session → velocity = 0.0 (neutral, not a signal)
        if prev is None:
            return 0.0
        return round(float(row["Value_Score"]) - prev, 4)

    def _action_signal(row) -> str:
        side = str(row.get("side") or "").upper()
        tag = str(row.get("Strategy_Tag") or "").strip()

        def _with_tag(base: str) -> str:
            if not tag:
                return base
            if not base or base == "HOLD":
                return tag
            return f"{base} · {tag}"

        # VWAP sniper overrides — highest priority entry timing signal
        if (
            vwap_state == "RECLAIMED UP"
            and daily_bias == "HEAVY BULLISH"
            and side == "CALL"
        ):
            return _with_tag("🚀 SNIPER ENTRY: VWAP RECLAIM")
        if (
            vwap_state == "RECLAIMED DOWN"
            and daily_bias == "HEAVY BEARISH"
            and side == "PUT"
        ):
            return _with_tag("🩸 SNIPER ENTRY: VWAP LOSS")

        vel = row["Score_Velocity"]
        if pd.isna(vel):
            return _with_tag("")
        if vel >= _SURGE_THRESH:
            if extended and side == "CALL":
                return _with_tag("⚠️ SURGE BUT EXTENDED")
            return _with_tag("🔥 BUYING SURGE")
        if vel <= _EXIT_THRESH:
            return _with_tag("🚨 EXIT / STOP-LOSS")
        return _with_tag("HOLD")

    df["Score_Velocity"] = df.apply(_velocity, axis=1)
    df["Action_Signal"]  = df.apply(_action_signal, axis=1)

    # Stash for Portfolio Manager (before score-cache overwrite)
    st.session_state[f"bv_velocity_snapshot_{ticker}"] = {
        _contract_key(row): float(row["Score_Velocity"])
        for _, row in df.iterrows()
        if pd.notna(row["Score_Velocity"])
    }
    _live_stash = df.copy()
    _live_stash["Ticker"] = ticker.upper()
    _live_stash["Current_Price"] = _live_stash["last"]
    st.session_state[f"bv_live_scanner_{ticker}"] = _live_stash[
        ["Ticker", "side", "strike", "expiry", "Current_Price", "Score_Velocity"]
    ].rename(columns={"side": "Side", "strike": "Strike", "expiry": "Expiry"})

    # ── Take Profit & Runner (Target_Status) ───────────────────────────────────
    # Entry premium: prefer previous-archive mid/last for the same contract;
    # otherwise lock first-seen price this Streamlit session (position tracking).
    entry_state_key = f"bv_entry_px_{ticker}"
    entry_px: dict[tuple, float] = st.session_state.setdefault(entry_state_key, {})

    prev_px: dict[tuple, float] = {}
    if vol_prev:
        for side, key in [("CALL", "top_calls"), ("PUT", "top_puts")]:
            for c in (vol_prev.get(key) or []):
                bid = float(c.get("bid") or 0)
                ask = float(c.get("ask") or 0)
                last = float(c.get("lastPrice") or 0)
                px = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
                if px > 0:
                    k = (side, float(c.get("strike") or 0), str(c.get("expiry") or ""))
                    prev_px[k] = px

    def _entry_price(row) -> float | None:
        k = _contract_key(row)
        if k in entry_px and entry_px[k] > 0:
            return float(entry_px[k])
        if k in prev_px:
            entry_px[k] = float(prev_px[k])
            return float(prev_px[k])
        cur = float(row.get("last") or 0)
        if cur > 0:
            entry_px[k] = cur  # seed — gain shows on next refresh
        return None

    def _target_status(row) -> str:
        vel = row["Score_Velocity"]
        # 1) Velocity flip → full exit (risk first)
        if pd.notna(vel) and float(vel) <= _EXIT_THRESH:
            return "🚨 CLOSE ENTIRE POSITION"
        # 2) Premium +25% from tracked entry → scale out half
        entry = _entry_price(row)
        cur = float(row.get("last") or 0)
        if entry and entry > 0 and cur > 0:
            gain = (cur - entry) / entry
            if gain >= _SCALE_PREMIUM_PCT:
                return "💰 SCALE 50% (LOCK PROFIT)"
        # 3) Strong velocity + heavy bullish day → hold runner
        if (
            pd.notna(vel)
            and float(vel) > _RUNNER_VEL_THRESH
            and daily_bias == "HEAVY BULLISH"
        ):
            return "🚀 HOLD FOR RUNNER"
        return ""

    df["Target_Status"] = df.apply(_target_status, axis=1)

    # Overwrite cache immediately — next refresh sees today's scores as "prev"
    st.session_state[state_key] = {
        _contract_key(row): float(row["Value_Score"])
        for _, row in df.iterrows()
        if pd.notna(row["Value_Score"])
    }
    st.session_state[entry_state_key] = entry_px

    # ── Build display DataFrame (top N by Value_Score) ────────────────────────
    keep = ["side", "strike", "expiry", "dte", "last", "volume", "openInterest"]
    if has_dvol:
        keep.append("dVol")
    keep += [
        "iv", "Value_Score", "Score_Velocity",
        "Action_Signal", "Target_Status", "Status",
    ]
    show_n = int(max(1, min(30, top_n)))
    top5 = (
        df[keep]
        .sort_values("Value_Score", ascending=False)
        .head(show_n)
        .copy()
    )

    # Persist top contracts for this scanner refresh (deduped by archive timestamp)
    log_best_value_run(top5, ticker=ticker, run_timestamp=run_timestamp)

    # Enrich with today's Times_Flagged persistence counter
    ensure_archive_loaded()
    today_hits = filter_today(st.session_state.get("best_value_archive"))
    flag_map: dict[tuple, int] = {}
    if today_hits is not None and not today_hits.empty:
        flagged = add_times_flagged(today_hits)
        for _, r in flagged.drop_duplicates(
            subset=["Ticker", "Side", "Strike", "Expiry"]
        ).iterrows():
            flag_map[
                (
                    str(r["Ticker"]).upper(),
                    str(r["Side"]).upper(),
                    round(float(r["Strike"]), 2),
                    str(r["Expiry"]),
                )
            ] = int(r["Times_Flagged"])

    def _times_flagged_row(row) -> int:
        k = (
            ticker.upper(),
            str(row["side"]).upper(),
            round(float(row["strike"]), 2),
            str(row["expiry"]),
        )
        return flag_map.get(k, 1)

    top5 = top5.copy()
    top5["Times_Flagged"] = top5.apply(_times_flagged_row, axis=1)

    # 5 Directions strategy is already baked into Value_Score / Strategy_Tag;
    # keep Optimal Strategy column aligned with the engine output.
    if "Optimal_Strategy" in top5.columns and top5["Optimal_Strategy"].astype(str).str.len().gt(0).any():
        top5["Optimal Strategy"] = top5["Optimal_Strategy"]
    else:
        top5 = attach_optimal_strategy(
            top5,
            daily_bias=daily_bias,
            profited_shares_pct=profited_pct,
            has_catalyst=bool(has_catalyst),
            spot_below_support=bool(spot_below_support),
            spot=spot,
        )

    disp = top5.rename(columns={
        "side":           "Side",
        "strike":         "Strike",
        "expiry":         "Expiry",
        "dte":            "DTE",
        "last":           "Price",
        "volume":         "Volume",
        "openInterest":   "OI",
        "iv":             "IV",
        "Score_Velocity": "Velocity",
        "Action_Signal":  "Signal",
        "Target_Status":  "Target",
        **( {"dVol": "ΔVol"} if has_dvol else {} ),
    })
    # Keep EM math internal — only show Optimal Strategy in the scanner table
    disp = disp.drop(
        columns=[c for c in ("Expected_Move", "Upper_1SD", "Lower_1SD") if c in disp.columns],
        errors="ignore",
    )

    # Format numeric columns for display
    disp["Strike"]      = disp["Strike"].apply(lambda x: f"${x:.1f}")
    disp["DTE"]         = disp["DTE"].apply(lambda x: f"{x}d")
    disp["Price"]       = disp["Price"].apply(lambda x: f"${x:.2f}")
    disp["Volume"]      = disp["Volume"].apply(lambda x: f"{int(x):,}")
    disp["OI"]          = disp["OI"].apply(lambda x: f"{int(x):,}")
    disp["IV"]          = disp["IV"].apply(lambda x: f"{x:.1%}" if x > 0 else "—")
    if has_dvol:
        disp["ΔVol"]    = disp["ΔVol"].apply(
            lambda x: f"{int(x):+,}" if pd.notna(x) else "—"
        )
    disp["Value_Score"] = disp["Value_Score"].apply(
        lambda x: f"{x:.4f}" if pd.notna(x) else "—"
    )
    disp["Velocity"]    = disp["Velocity"].apply(
        lambda x: f"{x:+.4f}" if pd.notna(x) else "—"
    )
    disp["Target"]      = disp["Target"].apply(lambda x: x if x else "—")
    disp["Times_Flagged"] = disp["Times_Flagged"].astype(int)
    if "Optimal Strategy" not in disp.columns:
        disp["Optimal Strategy"] = "—"

    # Interactive table: ＋ column in-row (st.dataframe cannot host buttons)
    _render_best_value_table_with_plus(ticker, top5, disp, has_dvol=has_dvol)
    # Caption: Velocity/Target columns removed from the table. Score_Velocity is
    # computed as 0.0 for first-seen contracts this session (by design, not a
    # formatting bug); later refreshes can produce non-zero velocity that still
    # feeds Action_Signal. Dropped the Velocity-named exit clauses so the caption
    # does not document a column that is no longer shown.
    st.caption(
        "**SCALE 50%** if premium ≥ +25% vs tracked entry "
        "(prior archive / first-seen).  "
        "Select a row, then use **＋ Add … to Open Positions** below the table."
    )
    _render_add_position_form(ticker)

    # ── Summary callout ───────────────────────────────────────────────────────
    best = df[df["Status"].astype(str).str.contains("BEST VALUE", na=False)]
    if not best.empty:
        b    = best.iloc[0]
        voi  = b["volume"] / max(int(b["openInterest"]), 1)
        dvol_part  = f" | **ΔVol:** {int(b['dVol']):+,}" if has_dvol and pd.notna(b.get("dVol")) else ""
        vel        = b["Score_Velocity"]
        vel_part   = f" | **Velocity:** {vel:+.4f}" if pd.notna(vel) else ""
        sig        = b["Action_Signal"]
        sig_part   = f" | {sig}" if sig and sig != "HOLD" else ""
        tgt        = b.get("Target_Status") or ""
        tgt_part   = f" | {tgt}" if tgt else ""
        sky_part   = f" | {BLUE_SKY_TAG}" if BLUE_SKY_TAG in str(b.get("Status") or "") else ""
        st.success(
            f"⭐ **{b['side']} ${b['strike']:.1f}**"
            f" | **Exp:** {b['expiry']}"
            f" | **Score:** {b['Value_Score']:.4f}"
            f"{vel_part}"
            f" | **Price:** ${b['last']:.2f}"
            f" | **Vol/OI:** {voi:.1f}x"
            f"{dvol_part}"
            f"{sig_part}"
            f"{tgt_part}"
            f"{sky_part}",
            icon="⭐",
        )


def _render_best_value_table_with_plus(
    ticker: str,
    top5: pd.DataFrame,
    disp: pd.DataFrame,
    *,
    has_dvol: bool,
) -> None:
    """Best Value table with native single-row selection + Add button below."""
    if top5 is None or top5.empty or disp is None or disp.empty:
        return

    top5_r = top5.reset_index(drop=True)
    disp_r = disp.reset_index(drop=True)

    show_cols = [
        "Side", "Strike", "Expiry", "DTE", "Price", "Volume", "OI",
    ]
    if has_dvol and "ΔVol" in disp_r.columns:
        show_cols.append("ΔVol")
    # Velocity / Target dropped — typically uniform zeros/"—" and steal width
    # from Optimal Strategy. Score_Velocity still drives Action_Signal upstream.
    show_cols += [
        "IV", "Value_Score", "Signal", "Optimal Strategy",
    ]
    show_cols = [c for c in show_cols if c in disp_r.columns]
    view = disp_r[show_cols].copy()

    col_cfg: dict = {
        "Side": st.column_config.TextColumn("Side", width="small"),
        "Strike": st.column_config.TextColumn("Strike", width="small"),
        "Expiry": st.column_config.TextColumn("Expiry", width="medium"),
        "DTE": st.column_config.TextColumn("DTE", width="small"),
        "Price": st.column_config.TextColumn("Price", width="small"),
        "Volume": st.column_config.TextColumn("Volume", width="small"),
        "OI": st.column_config.TextColumn("OI", width="small"),
        "ΔVol": st.column_config.TextColumn("ΔVol", width="small"),
        "IV": st.column_config.TextColumn("IV", width="small"),
        "Value_Score": st.column_config.TextColumn("Value_Score", width="small"),
        "Signal": st.column_config.TextColumn("Signal", width="medium"),
        "Optimal Strategy": st.column_config.TextColumn(
            "Optimal Strategy", width="large",
        ),
    }
    col_cfg = {k: v for k, v in col_cfg.items() if k in view.columns}

    styled = style_best_value_rows(view, top5_r)
    table_key = f"bv_select_{str(ticker).upper()}"

    sel: list[int] = []
    try:
        event = st.dataframe(
            styled,
            on_select="rerun",
            selection_mode="single-row",
            use_container_width=True,
            hide_index=True,
            column_config=col_cfg,
            key=table_key,
        )
        if event is not None and getattr(event, "selection", None) is not None:
            sel = list(event.selection.rows or [])
    except TypeError:
        # Older Streamlit: no on_select / column_config combo — fallback picker
        st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
            key=f"{table_key}_fallback_df",
        )
        labels = [
            f"{r['side']} ${float(r['strike']):.1f} · {r['expiry']}"
            for _, r in top5_r.iterrows()
        ]
        pick = st.selectbox(
            "Contract to add",
            options=["—"] + labels,
            key=f"{table_key}_fallback_pick",
        )
        if pick and pick != "—":
            sel = [labels.index(pick)]

    payload = pending_add_pos_payload(ticker, top5_r, sel)
    if payload is None:
        st.caption("Select a row to add it to Open Positions.")
    else:
        label = (
            f"＋  Add {payload['Side']} ${float(payload['Strike']):.1f} · "
            f"{payload['Expiry']} to Open Positions"
        )
        if st.button(label, type="primary", key=f"bv_add_selected_{ticker}"):
            st.session_state["_pending_add_pos"] = payload
            st.rerun()


def _render_add_position_form(ticker: str) -> None:
    """Price/qty form after clicking ＋ on a Best Value row."""
    pending = st.session_state.get("_pending_add_pos")
    if not pending or str(pending.get("Ticker") or "").upper() != ticker.upper():
        return

    with st.form(key=f"add_pos_form_{ticker}"):
        st.markdown(
            f"Add **{pending['Side']} ${float(pending['Strike']):.1f}** "
            f"exp `{pending['Expiry']}` to **My Open Positions**"
        )
        price = st.number_input(
            "Entry price ($)",
            min_value=0.01,
            value=max(0.01, float(pending.get("default_price") or 0.01)),
            step=0.05,
            format="%.2f",
        )
        qty = st.number_input(
            "Quantity (contracts)",
            min_value=1,
            value=1,
            step=1,
        )
        c1, c2 = st.columns(2)
        with c1:
            ok = st.form_submit_button("Add to Open Positions", type="primary")
        with c2:
            cancel = st.form_submit_button("Cancel")

    if cancel:
        st.session_state.pop("_pending_add_pos", None)
        st.rerun()
    if ok:
        df = portfolio_store.append_position(
            ticker=pending["Ticker"],
            side=pending["Side"],
            strike=float(pending["Strike"]),
            expiry=str(pending["Expiry"]),
            quantity=int(qty),
            entry_price=float(price),
            mark_price=float(price),
        )
        st.session_state["portfolio_df"] = df
        st.session_state.pop("_pending_add_pos", None)
        st.success(
            f"Added {pending['Side']} ${float(pending['Strike']):.1f} "
            f"×{int(qty)} @ ${float(price):.2f} to Open Positions"
        )
        st.rerun()


def _render_cost_distribution_panel(
    ticker: str,
    spot: float,
    cost_info: dict | None = None,
) -> None:
    """Zone 5 — Macro Cost Distribution & Overhead Supply metrics + chart."""
    st.markdown("### 📊 Macro Cost Distribution & Overhead Supply")
    st.caption(
        "6-month daily volume profile by Typical Price (H+L+C)/3 · "
        "teal = cost below spot (in profit) · orange = overhead supply"
    )

    info = cost_info or _cached_cost_distribution(ticker, spot if spot > 0 else None)
    poc = info.get("Average_Cost_POC")
    prof = info.get("Profited_Shares_Pct")
    r90 = info.get("Cost_Range_90") or (None, None)
    r70 = info.get("Cost_Range_70") or (None, None)
    prices = info.get("price_bins") or []
    vols = info.get("volume_bins") or []

    if not prices or poc is None:
        st.info("Cost distribution unavailable — daily history fetch failed.")
        return

    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Average Cost (POC)", f"${float(poc):.2f}")
    with m2:
        delta = None
        if prof is not None and float(prof) >= 95.0:
            delta = "near zero overhead"
        st.metric(
            "Profited Shares %",
            f"{float(prof):.1f}%" if prof is not None else "—",
            delta=delta,
        )
    with m3:
        if r90[0] is not None and r90[1] is not None:
            st.metric(
                "90% Cost Range",
                f"${float(r90[0]):.2f} – ${float(r90[1]):.2f}",
            )
        else:
            st.metric("90% Cost Range", "—")

    if r70[0] is not None and r70[1] is not None:
        st.caption(
            f"70% Cost Range: **${float(r70[0]):.2f} – ${float(r70[1]):.2f}** · "
            f"{info.get('days', '—')} sessions · "
            f"total vol {int(info.get('total_volume') or 0):,}"
        )

    spot_px = float(info.get("spot") or spot or 0)
    fig = render_cost_distribution_chart(
        prices,
        vols,
        spot_price=spot_px,
        avg_cost=float(poc),
        range_70=r70 if r70[0] is not None else None,
        range_90=r90 if r90[0] is not None else None,
        ticker=ticker,
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def _build_expiry_table(
    vol: dict, prev_vol: dict | None, overall_pc: float
) -> tuple[list[dict], dict[str, float]]:
    """
    Group top_calls + top_puts by expiry, compute P/C and Δ vs previous run.
    Returns (rows_for_table, {expiry: pc_ratio}) for the bar chart.
    """
    curr_exp: dict[str, dict] = {}
    for side_key, vol_key in [("call_vol", "top_calls"), ("put_vol", "top_puts")]:
        for c in (vol.get(vol_key) or []):
            exp = c.get("expiry", "?")
            dte = int(c.get("dte", 0))
            v   = int(c.get("volume") or 0)
            if exp not in curr_exp:
                curr_exp[exp] = {"dte": dte, "call_vol": 0, "put_vol": 0}
            curr_exp[exp][side_key] += v

    prev_exp: dict[str, dict] = {}
    if prev_vol:
        for side_key, vol_key in [("call_vol", "top_calls"), ("put_vol", "top_puts")]:
            for c in (prev_vol.get(vol_key) or []):
                exp = c.get("expiry", "?")
                v   = int(c.get("volume") or 0)
                prev_exp.setdefault(exp, {"call_vol": 0, "put_vol": 0})[side_key] += v

    rows, chart_pc = [], {}
    for exp in sorted(curr_exp):
        d  = curr_exp[exp]
        cv, pv  = d["call_vol"], d["put_vol"]
        dte     = d["dte"]
        data_gap = (cv == 0 or pv == 0)
        pc       = (pv / cv) if not data_gap else None
        chart_pc[exp] = pc   # None means data gap — caller must handle

        if pc is None:
            bias = ""
        elif pc < 0.7:   bias = "▲ BULLISH"
        elif pc < 0.9: bias = "▲ MILD BULLISH"
        elif pc < 1.1: bias = "— NEUTRAL"
        elif pc < 1.5: bias = "▼ MILD BEARISH"
        else:          bias = "▼ BEARISH"

        notable = (
            abs(pc - overall_pc) > 0.25
            if (pc is not None and overall_pc > 0)
            else False
        )

        pd_   = prev_exp.get(exp, {})
        cv_d  = cv - pd_.get("call_vol", 0) if prev_vol else None
        pv_d  = pv - pd_.get("put_vol",  0) if prev_vol else None

        def _ds(v):
            if v is None: return "·0"
            if v > 0: return f"▲+{v:,}"
            if v < 0: return f"▼{v:,}"
            return "·0"

        rows.append({
            "EXPIRY":   exp,
            "DTE":      f"{dte}d",
            "CALL VOL": f"{cv:,}",
            "PUT VOL":  f"{pv:,}",
            "P/C":      f"{pc:.2f}" if pc is not None else "n/a",
            "BIAS":     bias,
            "NOTABLE":  "⚠ data gap" if data_gap else ("◄ notable" if notable else ""),
            "CALL Δ":   _ds(cv_d),
            "PUT Δ":    _ds(pv_d),
        })

    return rows, chart_pc


def _render_0dte_gamma_kpi(odte_info: dict | None) -> None:
    """Hero KPI card: 0DTE Gamma Flow state + Call/Put dominance bar."""
    info = odte_info or {}
    state = info.get("0DTE_State") or "—"
    ratio = info.get("0DTE_Call_Ratio")
    net_g = info.get("Net_0DTE_Gamma")
    cv = int(info.get("0DTE_Call_Volume") or 0)
    pv = int(info.get("0DTE_Put_Volume") or 0)
    afternoon = bool(info.get("afternoon_phase"))

    if state == STATE_SQUEEZE:
        border = "#00e676"
        bg = "rgba(0,230,118,0.08)"
    elif state == STATE_CASCADE:
        border = "#ff1744"
        bg = "rgba(255,23,68,0.08)"
    else:
        border = "#90a4ae"
        bg = "rgba(144,164,174,0.06)"

    phase = " · Afternoon Acceleration" if afternoon else ""
    gex_s = f"{float(net_g):+.2e}" if net_g is not None else "—"
    ratio_s = f"{float(ratio)*100:.0f}% Call" if ratio is not None else "—"

    st.markdown(
        f'<div style="background:{bg};border:1px solid {border};border-radius:8px;'
        f'padding:0.7rem 1rem;margin:0.4rem 0 0.6rem 0">'
        f'<div style="display:flex;flex-wrap:wrap;justify-content:space-between;'
        f'align-items:center;gap:0.5rem">'
        f'<div><div style="font-size:0.75rem;color:#9e9e9e;letter-spacing:0.04em">'
        f'0DTE GAMMA FLOW{phase}</div>'
        f'<div style="font-size:1.05rem;font-weight:800;color:#eee;margin-top:2px">'
        f'{state}</div></div>'
        f'<div style="text-align:right;font-size:0.85rem;color:#b0bec5">'
        f'C {cv:,} · P {pv:,}<br>'
        f'Net GEX {gex_s} · {ratio_s}</div>'
        f'</div>'
        f'<div style="margin-top:0.55rem">{call_put_progress_bar_html(ratio, width_px=220)}</div>'
        f'</div>',
        unsafe_allow_html=True,
    )


def _render_0dte_top_strikes_expander(odte_info: dict | None) -> None:
    """Options Flow expander — Top 5 most active 0DTE strikes (MM exposure)."""
    info = odte_info or {}
    top = info.get("top_strikes") or []
    with st.expander("⚡ 0DTE Gamma Exposure — Top 5 Active Strikes", expanded=bool(top)):
        if not info.get("has_0dte") or not top:
            st.caption("No 0DTE contracts in the current archive snapshot.")
            return
        st.caption(
            f"{info.get('0DTE_State')} · "
            f"Call ratio {(info.get('0DTE_Call_Ratio') or 0)*100:.0f}% · "
            f"Net GEX {info.get('Net_0DTE_Gamma')}"
        )
        rows = []
        for r in top:
            rows.append({
                "Strike": f"${float(r['strike']):.1f}",
                "Call Vol": int(r["call_vol"]),
                "Put Vol": int(r["put_vol"]),
                "Total Vol": int(r["total_vol"]),
                "ATM": "✓" if r.get("atm") else "",
                "Bias": (
                    "🟢 Call" if r["call_vol"] > r["put_vol"] * 1.15
                    else ("🔴 Put" if r["put_vol"] > r["call_vol"] * 1.15 else "⚪ Mixed")
                ),
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        st.markdown(
            call_put_progress_bar_html(info.get("0DTE_Call_Ratio"), width_px=240),
            unsafe_allow_html=True,
        )


def _render_tab1(cfg: dict):
    """Options Flow — Magnets heatmap + Volume-by-Expiry term structure."""

    ticker = cfg.get("ticker", "AAPL")
    files  = sorted(glob.glob(f"archive/{ticker}_*.json"), reverse=True)
    if not files:
        st.info(f"No archive data found for {ticker}. Run the scanner first or pick a different ticker.")
        return

    try:
        with open(files[0]) as f:
            curr = json.load(f)
    except Exception as e:
        st.error(f"Could not read archive: {e}"); return

    prev = None
    if len(files) > 1:
        try:
            with open(files[1]) as f:
                prev = json.load(f)
        except Exception:
            pass

    spot      = float(curr.get("spot") or 0)
    direction = curr.get("direction", "—")
    ts_str    = curr.get("timestamp", "")
    vol       = curr.get("volume") or {}
    tfs       = curr.get("timeframes") or {}
    mags      = curr.get("signal_magnets") or {}
    or_data   = curr.get("or_data") or {}
    pc_ratio  = float(vol.get("pc_ratio") or 0)
    prev_vol  = (prev.get("volume") or {}) if prev else None

    # ── Shared context for all zones ──────────────────────────────────────────
    session     = curr.get("session") or {}
    prev_close  = session.get("prev_close")
    open_today  = session.get("open")
    day_high    = session.get("day_high")
    day_low     = session.get("day_low")
    spot_label  = "Close" if _market_is_closed() else "Spot"
    prev_tfs    = (prev.get("timeframes") or {}) if prev else {}
    top_n       = cfg.get("top_n", 5)

    daily_bias_info   = _resolve_daily_bias(ticker, session, spot)
    market_state_info = _resolve_market_state()
    vwap_info         = _cached_vwap_state(ticker)
    vwap_px           = vwap_info.get("VWAP")
    vwap_state        = vwap_info.get("VWAP_State") or "UNKNOWN"
    news_info         = _cached_news_sentiment(ticker)
    news_bias         = news_info.get("news_bias") or "NEUTRAL"
    catalyst          = news_info.get("catalyst_score", 0.0)
    headlines         = news_info.get("top_headlines") or []
    cost_info         = _cached_cost_distribution(ticker, spot if spot > 0 else None)
    has_catalyst      = resolve_has_catalyst(news_bias, catalyst)
    spot_below_sup    = resolve_spot_below_support(
        spot,
        vwap=float(vwap_px) if vwap_px is not None else None,
        vwap_state=vwap_state,
        cost_info=cost_info,
    )
    em_range          = ticker_expected_range(spot, vol)
    optimal_strat     = recommend_strategy(
        (daily_bias_info or {}).get("daily_bias"),
        em_range.get("IV"),
        (cost_info or {}).get("Profited_Shares_Pct"),
        has_catalyst,
        spot_below_support=spot_below_sup,
    )
    odte_info         = calculate_0dte_gamma_flow(vol, spot, ticker=ticker)
    pov_df, pov_info  = _cached_pov_leakage(ticker)

    call_vol = int(vol.get("total_call_vol") or 0)
    put_vol  = int(vol.get("total_put_vol")  or 0)
    pc_bias  = "BULLISH SKEW" if pc_ratio < 0.7 else ("BEARISH SKEW" if pc_ratio > 1.0 else "NEUTRAL")

    spot_chg = spot_chg_pct = None
    if prev_close is not None and prev_close:
        spot_chg = spot - prev_close
        spot_chg_pct = spot_chg / prev_close * 100

    if ts_str:
        ts_et = datetime.fromisoformat(ts_str).astimezone(ET)
        st.caption(f"Last run: **{ts_et.strftime('%Y-%m-%d %H:%M ET')}**")

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE 1 — System Header & Macro KPIs
    # ══════════════════════════════════════════════════════════════════════════
    dir_color   = "#00c853" if "BULL" in direction else "#d50000" if "BEAR" in direction else "#9e9e9e"
    dir_icon    = "▲" if "BULL" in direction else "▼" if "BEAR" in direction else "─"
    hist_suffix = " (historical)" if _market_is_closed() else ""
    ms_label    = (market_state_info or {}).get("market_state") or "—"

    banner_meta = [
        f'<span style="color:#aaa">{ticker} · {spot_label} '
        f'<b style="color:#eee">${spot:.2f}</b></span>',
        f'<span style="color:#00c853">Calls {call_vol:,}</span>',
        f'<span style="color:#d50000">Puts {put_vol:,}</span>',
        f'<span style="color:#aaa">P/C {pc_ratio:.2f} ({pc_bias})</span>',
        f'<span style="color:#90caf9">Macro {ms_label}</span>',
    ]
    if open_today is not None:
        banner_meta.insert(1, f'<span style="color:#aaa">Open ${open_today:.2f}</span>')
    if day_high is not None and day_low is not None:
        banner_meta.append(
            f'<span style="color:#888">H ${day_high:.2f} · L ${day_low:.2f}</span>'
        )
    if em_range.get("Lower_1SD") is not None and em_range.get("Upper_1SD") is not None:
        banner_meta.append(
            f'<span style="color:#ce93d8;font-weight:600">'
            f'1SD Expected Range: '
            f'${float(em_range["Lower_1SD"]):.2f} – '
            f'${float(em_range["Upper_1SD"]):.2f}</span>'
        )
    banner_meta.append(
        f'<span style="color:#80cbc4">{optimal_strat}</span>'
    )

    st.markdown(
        f'<div style="background:#1a1a2e;padding:0.85rem 1.4rem;border-radius:8px;'
        f'margin-bottom:0.75rem;border-left:4px solid {dir_color}">'
        f'<div style="font-size:1.45rem;font-weight:900;color:{dir_color};margin-bottom:0.35rem">'
        f'{dir_icon} {direction}{hist_suffix}</div>'
        f'<div style="display:flex;flex-wrap:wrap;gap:0.15rem 0;align-items:center">'
        + "&ensp;·&ensp;".join(banner_meta)
        + "</div></div>",
        unsafe_allow_html=True,
    )

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    if spot_chg is not None and spot_chg_pct is not None:
        k1.metric(
            f"{spot_label} Price",
            f"${spot:.2f}",
            delta=f"{spot_chg:+.2f} ({spot_chg_pct:+.2f}%)",
        )
    else:
        k1.metric(f"{spot_label} Price", f"${spot:.2f}")

    if market_state_info:
        spy = market_state_info["spy_close"]
        qqq = market_state_info["qqq_close"]
        vix = market_state_info["vix_close"]
        spy_chg = market_state_info.get("spy_chg_pct")
        qqq_chg = market_state_info.get("qqq_chg_pct")
        vchg = market_state_info.get("vix_chg_pct")
        k2.metric("SPY", f"${spy:.2f}", delta=f"{spy_chg:+.2f}%" if spy_chg is not None else None)
        k3.metric("QQQ", f"${qqq:.2f}", delta=f"{qqq_chg:+.2f}%" if qqq_chg is not None else None)
        k4.metric("VIX", f"{vix:.2f}", delta=f"{vchg:+.2f}%" if vchg is not None else None)
    else:
        k2.metric("SPY", "—")
        k3.metric("QQQ", "—")
        k4.metric("VIX", "—")

    if daily_bias_info:
        k5.metric(
            "Daily Bias",
            daily_bias_info["daily_bias"],
            delta=f"body {daily_bias_info['body_ratio']:+.2f}",
        )
    else:
        k5.metric("Daily Bias", "—")

    if vwap_px is not None:
        k6.metric("Live VWAP", f"${float(vwap_px):.2f}", delta=vwap_state)
    else:
        k6.metric("Live VWAP", "—")

    # ── 0DTE Gamma Flow KPI ───────────────────────────────────────────────────
    _render_0dte_gamma_kpi(odte_info)

    # 1SD expected range strip (68% probability band)
    if em_range.get("Lower_1SD") is not None and em_range.get("Upper_1SD") is not None:
        dte_s = em_range.get("DTE")
        iv_s = em_range.get("IV")
        em_s = em_range.get("Expected_Move")
        detail = []
        if em_s is not None:
            detail.append(f"EM ±${float(em_s):.2f}")
        if iv_s is not None:
            detail.append(f"IV {float(iv_s):.1%}")
        if dte_s is not None:
            detail.append(f"DTE {float(dte_s):.0f}d")
        st.caption(
            f"**1SD Expected Range:** "
            f"${float(em_range['Lower_1SD']):.2f} – ${float(em_range['Upper_1SD']):.2f}"
            + (f"  ·  {' · '.join(detail)}" if detail else "")
            + f"  ·  **Strategy:** {optimal_strat}"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE 2 — Main Workspace Grid
    # ══════════════════════════════════════════════════════════════════════════
    with st.container():
        chart_tf = _choice_control(
            "Chart timeframe",
            list(CHART_TIMEFRAMES),
            default="5M",
            key=f"chart_tf_{ticker}",
            help="Candlestick + VWAP interval (10M/45M resampled from 5m; 4H from 1h)",
        )
        chart_df = _cached_vwap_chart_df(ticker, chart_tf)
        if chart_df is not None and not chart_df.empty:
            fig = render_vwap_chart(chart_df, ticker=ticker, timeframe=chart_tf)
            st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        else:
            st.caption(f"{chart_tf} VWAP chart unavailable right now.")

        # Institutional POV leakage (always 5m participation math)
        if pov_df is not None and not pov_df.empty:
            pov_fig = render_pov_leakage_chart(pov_df, ticker=ticker)
            st.plotly_chart(pov_fig, use_container_width=True, config={"displayModeBar": False})
            if pov_info.get("urgency"):
                st.caption(
                    f"**{URGENCY_TAG}** · last bar POV "
                    f"**{pov_info.get('ratio')}×** · price above VWAP"
                )
            elif pov_info.get("ratio") is not None:
                st.caption(
                    f"POV last bar: **{pov_info.get('ratio')}×** "
                    f"(leakage threshold {3.0:.1f}×) · "
                    f"{'above' if pov_info.get('above_vwap') else 'below/at'} VWAP"
                )
        else:
            st.caption("POV leakage chart unavailable (no 5m volume).")

    # Same row: Volume Analysis | Multi-Timeframe | My Open Positions
    sub_c1, sub_c2, sub_c3 = st.columns(3)
    with sub_c1:
        _render_volume_analysis(ticker, compact=True, vol_curr=vol)
    with sub_c2:
        _render_mtf_matrix(tfs, prev_tfs)
    with sub_c3:
        _render_portfolio_manager(
            ticker, vol, spot, prev_vol,
            daily_bias=(daily_bias_info or {}).get("daily_bias"),
            market_state=(market_state_info or {}).get("market_state"),
            news_bias=news_bias,
            compact=True,
        )

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE 3 — Catalyst (collapsed by default)
    # ══════════════════════════════════════════════════════════════════════════
    _bias_colors = {
        "BULLISH": "#00c853",
        "BEARISH": "#d50000",
        "NEUTRAL": "#9e9e9e",
    }
    bias_color = _bias_colors.get(news_bias, "#9e9e9e")

    with st.expander("📰 Live Catalyst Sentiment & News", expanded=False):
        st.markdown(
            f'<span style="font-size:1.15rem;font-weight:700;color:{bias_color}">'
            f'{news_bias}</span>'
            f'  ·  catalyst score '
            f'<span style="font-weight:700;color:{bias_color}">{catalyst:+.2f}</span>',
            unsafe_allow_html=True,
        )
        if headlines:
            bullets = []
            for h in headlines:
                src  = h.get("source") or "Unknown"
                text = h.get("headline") or ""
                url  = h.get("url") or ""
                if url and text:
                    bullets.append(f"- **{src}**: [{text}]({url})")
                elif text:
                    bullets.append(f"- **{src}**: {text}")
            if bullets:
                st.markdown("\n".join(bullets))
        else:
            st.caption("No recent headlines from Finnhub or Yahoo Finance.")

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE 4 — Execution Engine (Best Value)
    # ══════════════════════════════════════════════════════════════════════════
    st.subheader("⭐ Best Value Option Scanner")
    _render_best_value_panel(
        vol, spot, prev_vol, ticker=ticker,
        daily_bias_info=daily_bias_info,
        market_state_info=market_state_info,
        news_bias=news_bias,
        session_low=session.get("day_low"),
        vwap_info=vwap_info,
        run_timestamp=ts_str,
        cost_info=cost_info,
        has_catalyst=has_catalyst,
        spot_below_support=spot_below_sup,
        optimal_strategy=optimal_strat,
        upper_1sd=em_range.get("Upper_1SD"),
        lower_1sd=em_range.get("Lower_1SD"),
        odte_info=odte_info,
        pov_info=pov_info,
        top_n=top_n,
    )

    # 0DTE reflexivity — top strikes MM exposure
    _render_0dte_top_strikes_expander(odte_info)

    # ══════════════════════════════════════════════════════════════════════════
    # ZONE 5 — Analytics (Magnets + Expiration + Cost Distribution)
    # ══════════════════════════════════════════════════════════════════════════
    tab_magnets, tab_expiry, tab_cost = st.tabs([
        "🧲 Flow Magnets",
        "📅 Expiration Breakdown",
        "📊 Cost Distribution",
    ])

    with tab_magnets:
        st.markdown(f"### The Magnets — Top {top_n} Calls / Top {top_n} Puts")
        st.caption("🔥 Vol/OI heatmap — values ≥ 2.0x glow hot (unusual vs open interest)")
        call_col, put_col = st.columns(2, gap="small")
        for col, key, label in [
            (call_col, "top_calls", f"🟢 Top {top_n} CALLS"),
            (put_col,  "top_puts",  f"🔴 Top {top_n} PUTS"),
        ]:
            contracts = vol.get(key) or []
            with col:
                st.markdown(f"**{label}**")
                styled = _contracts_table(contracts, n=top_n)
                if styled is not None:
                    st.dataframe(styled, use_container_width=True, hide_index=True)
                else:
                    st.caption("No data")

    with tab_expiry:
        st.markdown("### Volume by Expiry — P/C term structure")
        st.caption("Institutional-style skew across the expiry curve")
        exp_rows, chart_pc = _build_expiry_table(vol, prev_vol, pc_ratio)
        if exp_rows:
            exp_df = pd.DataFrame(exp_rows)

            def _bias_style(val: str) -> str:
                if "BULL" in str(val):
                    return "color:#00c853;font-weight:bold"
                if "BEAR" in str(val):
                    return "color:#d50000;font-weight:bold"
                return "color:#9e9e9e"

            def _delta_style(val: str) -> str:
                s = str(val)
                if s.startswith("▲"):
                    return "color:#00c853"
                if s.startswith("▼"):
                    return "color:#d50000"
                return "color:#666"

            styled_exp = (
                exp_df.style
                .map(_bias_style, subset=["BIAS"])
                .map(_delta_style, subset=["CALL Δ", "PUT Δ"])
            )
            st.dataframe(styled_exp, use_container_width=True, hide_index=True)
            _render_pc_term_chart(chart_pc)
        else:
            st.caption("No expiry data available")

    with tab_cost:
        _render_cost_distribution_panel(ticker, spot, cost_info)

    # ══ Collapsible detail sections ═══════════════════════════════════════════
    if prev:
        prev_spot = float(prev.get("spot") or 0)
        prev_pc   = float((prev.get("volume") or {}).get("pc_ratio") or 0)
        try:
            prev_ts_str = datetime.fromisoformat(prev.get("timestamp", "")).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
        except Exception:
            prev_ts_str = "previous run"
        vs_spot = spot - prev_spot
        pc_chg  = pc_ratio - prev_pc
        pct_chg = (vs_spot / prev_spot * 100) if prev_spot else 0

        with st.expander(f"📈 Changes vs last run  (since {prev_ts_str})", expanded=False):
            ca, cb = st.columns(2)
            with ca:
                st.metric("Spot", f"${spot:.2f}", delta=f"{vs_spot:+.2f} ({pct_chg:+.1f}%)")
                st.metric("P/C Ratio", f"{pc_ratio:.3f}", delta=f"{pc_chg:+.3f}")
            with cb:
                rsi_lines = []
                for tf in ["5M", "10M", "15M", "45M", "1H", "4H", "1D"]:
                    cr = (tfs.get(tf) or {}).get("rsi")
                    pr = ((prev.get("timeframes") or {}).get(tf) or {}).get("rsi")
                    if cr is not None and pr is not None:
                        rsi_lines.append(f"**{tf}:** {pr:.1f}→{cr:.1f} ({cr-pr:+.1f})")
                if rsi_lines:
                    st.markdown("**RSI shifts**  \n" + "  \n".join(rsi_lines))
            prev_mags = prev.get("signal_magnets") or {}
            for side in ("call", "put"):
                cm = mags.get(side) or {}
                pm = prev_mags.get(side) or {}
                if cm and pm and cm.get("strike") != pm.get("strike"):
                    icon = "▲ CALL" if side == "call" else "▼ PUT"
                    st.info(
                        f"**{icon} MAGNET shifted:** "
                        f"${pm.get('strike')} ({pm.get('expiry')}) → "
                        f"${cm.get('strike')} ({cm.get('expiry')})  ← STRIKE CHANGE"
                    )

    with st.expander("⏰ Opening Range Breakout", expanded=False):
        or_rows = []
        for tf_key in ["5M", "15M"]:
            or_tf = or_data.get(tf_key) or {}
            if or_tf:
                or_rows.append({
                    "TF":        tf_key,
                    "Open time": or_tf.get("open_time", "—"),
                    "Open":      f"${or_tf.get('open', 0):.2f}",
                    "High":      f"${or_tf.get('high', 0):.2f}",
                    "Low":       f"${or_tf.get('low', 0):.2f}",
                    "Range":     f"${or_tf.get('range', 0):.2f} ({or_tf.get('range_pct', 0):.2f}%)",
                    "Current":   f"${float(or_tf.get('current') or spot):.2f}",
                    "Bias":      or_tf.get("bias", "—"),
                })
        if or_rows:
            def _or_bias_style(val: str) -> str:
                if "BULL" in str(val):
                    return "color:#00c853;font-weight:bold"
                if "BEAR" in str(val):
                    return "color:#d50000;font-weight:bold"
                return "color:#9e9e9e"
            or_df = pd.DataFrame(or_rows)
            st.dataframe(
                or_df.style.map(_or_bias_style, subset=["Bias"]),
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.caption("No Opening Range data in this archive.")


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


def _volume_split_gauge_html(call_vol: int, put_vol: int, width_px: int = 140) -> str:
    """Green/red horizontal split bar (HTML) for Call% vs Put%."""
    total = max(int(call_vol) + int(put_vol), 0)
    if total <= 0:
        return (
            '<div style="color:#666;font-size:0.8rem">—</div>'
        )
    call_pct = 100.0 * int(call_vol) / total
    put_pct = 100.0 - call_pct
    return (
        f'<div style="display:flex;align-items:center;gap:6px">'
        f'<div style="flex:0 0 {width_px}px;height:12px;border-radius:6px;'
        f'overflow:hidden;background:#333;display:flex">'
        f'<div style="width:{call_pct:.1f}%;background:#00c853"></div>'
        f'<div style="width:{put_pct:.1f}%;background:#d50000"></div>'
        f'</div>'
        f'<span style="font-size:0.75rem;color:#9e9e9e;white-space:nowrap">'
        f'{call_pct:.0f}% / {put_pct:.0f}%</span></div>'
    )


def _volume_split_gauge_text(call_vol: int, put_vol: int, width: int = 16) -> str:
    """Unicode fallback gauge for st.dataframe cells."""
    total = max(int(call_vol) + int(put_vol), 0)
    if total <= 0:
        return "—"
    n_call = int(round(width * int(call_vol) / total))
    n_call = max(0, min(width, n_call))
    n_put = width - n_call
    return f"🟢{'█' * n_call}{'░' * n_put}🔴"


def _build_expiry_strike_chain(vol_curr: dict, expiry: str) -> pd.DataFrame:
    """
    Merge calls + puts for one expiry into a strike-level option chain.
    Columns: strike, call_vol, put_vol, call_oi, put_oi, total_vol, ...
    """
    calls: dict[float, dict] = {}
    puts: dict[float, dict] = {}
    for c in (vol_curr.get("top_calls") or []):
        if c.get("expiry") != expiry:
            continue
        k = float(c.get("strike") or 0)
        calls[k] = c
    for c in (vol_curr.get("top_puts") or []):
        if c.get("expiry") != expiry:
            continue
        k = float(c.get("strike") or 0)
        puts[k] = c

    strikes = sorted(set(calls) | set(puts))
    rows = []
    for k in strikes:
        cv = int((calls.get(k) or {}).get("volume") or 0)
        pv = int((puts.get(k) or {}).get("volume") or 0)
        coi = int((calls.get(k) or {}).get("openInterest") or 0)
        poi = int((puts.get(k) or {}).get("openInterest") or 0)
        total = cv + pv
        if cv > pv * 1.15:
            bias = "🟢 Call Dominated"
        elif pv > cv * 1.15:
            bias = "🔴 Put Dominated"
        else:
            bias = "⚪ Balanced"
        rows.append({
            "strike": k,
            "call_vol": cv,
            "put_vol": pv,
            "call_oi": coi,
            "put_oi": poi,
            "total_vol": total,
            "bias": bias,
            "call_share": (cv / total) if total else 0.0,
        })
    return pd.DataFrame(rows)


def _render_top_strikes_volume_chart(chain: pd.DataFrame, expiry: str) -> None:
    """Horizontal stacked Call/Put volume for top-10 strikes by total volume."""
    import plotly.graph_objects as go

    if chain is None or chain.empty:
        return
    top = (
        chain.sort_values("total_vol", ascending=False)
        .head(10)
        .sort_values("strike", ascending=True)
    )
    if top.empty or int(top["total_vol"].sum()) <= 0:
        st.caption("No strike volume to chart for this expiry.")
        return

    labels = [f"${float(s):.1f}" for s in top["strike"]]
    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            name="Call Vol",
            y=labels,
            x=top["call_vol"],
            orientation="h",
            marker_color="#00c853",
            hovertemplate="Call %{x:,}<extra></extra>",
        )
    )
    fig.add_trace(
        go.Bar(
            name="Put Vol",
            y=labels,
            x=top["put_vol"],
            orientation="h",
            marker_color="#d50000",
            hovertemplate="Put %{x:,}<extra></extra>",
        )
    )
    fig.update_layout(
        barmode="stack",
        title=dict(
            text=f"Top 10 Strikes by Total Volume — {expiry}",
            font=dict(size=14, color="#e0e0e0"),
            x=0.01,
            xanchor="left",
        ),
        template="plotly_dark",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        height=320,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", y=1.08, x=1, xanchor="right"),
        xaxis=dict(title="Contracts", showgrid=True, gridcolor="rgba(255,255,255,0.06)"),
        yaxis=dict(title="Strike", showgrid=False),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})


def _render_expiry_drill_down(
    vol_curr: dict, expiry: str, vol_prev: dict | None = None
) -> None:
    """
    Master-detail strike chain for one expiry:
      • metrics row + Call/Put volume gauges
      • Top-10 stacked volume chart
      • Full strike table with ratio gauges + MAX VOLUME MAGNET badge
    """
    with st.container():
        st.markdown(f"### 📋 Option Chain — `{expiry}`")
        chain = _build_expiry_strike_chain(vol_curr, expiry)
        if chain.empty:
            st.caption("No contracts for this expiry in the archive top-30 snapshot.")
            return

        total_c = int(chain["call_vol"].sum())
        total_p = int(chain["put_vol"].sum())
        total_v = total_c + total_p
        max_idx = int(chain["total_vol"].idxmax()) if total_v > 0 else None
        magnet_strike = float(chain.loc[max_idx, "strike"]) if max_idx is not None else None

        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Call Volume", f"{total_c:,}")
        m2.metric("Put Volume", f"{total_p:,}")
        m3.metric("Total Volume", f"{total_v:,}")
        if total_c > 0:
            m4.metric("Expiry P/C", f"{total_p / total_c:.2f}")
        else:
            m4.metric("Expiry P/C", "n/a")

        st.markdown(
            "**Expiry Call / Put Split**&nbsp;&nbsp;"
            + _volume_split_gauge_html(total_c, total_p, width_px=220),
            unsafe_allow_html=True,
        )
        if magnet_strike is not None:
            st.success(
                f"🧲 **MAX VOLUME MAGNET** — Strike **${magnet_strike:.1f}** "
                f"({int(chain.loc[max_idx, 'total_vol']):,} contracts)"
            )

        _render_top_strikes_volume_chart(chain, expiry)

        # ── Strike-level detail table ─────────────────────────────────────────
        disp_rows = []
        for _, r in chain.sort_values("strike").iterrows():
            cv, pv = int(r["call_vol"]), int(r["put_vol"])
            badge = ""
            if magnet_strike is not None and abs(float(r["strike"]) - magnet_strike) < 1e-9:
                badge = "🧲 MAX VOLUME MAGNET"
            disp_rows.append({
                "Strike Price": f"${float(r['strike']):.1f}",
                "Call Volume": cv,
                "Put Volume": pv,
                "Call/Put Ratio Gauge": _volume_split_gauge_text(cv, pv),
                "Total Volume": int(r["total_vol"]),
                "Call OI": int(r["call_oi"]),
                "Put OI": int(r["put_oi"]),
                "Dominant Bias": r["bias"],
                "Badge": badge,
                "_call_share": float(r["call_share"]),
                "_is_magnet": bool(badge),
            })

        disp = pd.DataFrame(disp_rows)
        show_cols = [
            "Strike Price", "Call Volume", "Put Volume",
            "Call/Put Ratio Gauge", "Total Volume",
            "Call OI", "Put OI", "Dominant Bias", "Badge",
        ]

        def _bias_fg(val: str) -> str:
            s = str(val)
            if "Call Dominated" in s:
                return "color:#00c853;font-weight:bold"
            if "Put Dominated" in s:
                return "color:#d50000;font-weight:bold"
            return "color:#9e9e9e"

        def _magnet_row(row):
            if row.get("Badge"):
                return ["background-color:#1a237e;color:#e8eaf6;font-weight:bold"] * len(row)
            return [""] * len(row)

        styled = (
            disp[show_cols].style
            .apply(_magnet_row, axis=1)
            .map(_bias_fg, subset=["Dominant Bias"])
        )
        st.dataframe(
            styled,
            use_container_width=True,
            hide_index=True,
            height=min(420, 48 + 28 * max(len(disp), 1)),
        )

        # HTML gauge strip for the top strikes (richer than unicode blocks)
        with st.expander("🔬 Visual Call/Put gauges (HTML)", expanded=True):
            html_rows = [
                '<div style="font-family:monospace;font-size:0.85rem">'
            ]
            for _, r in chain.sort_values("total_vol", ascending=False).head(12).iterrows():
                mag = (
                    ' <span style="color:#82b1ff;font-weight:700">🧲 MAX VOLUME MAGNET</span>'
                    if magnet_strike is not None
                    and abs(float(r["strike"]) - magnet_strike) < 1e-9
                    else ""
                )
                html_rows.append(
                    f'<div style="display:flex;align-items:center;gap:10px;'
                    f'margin:4px 0;padding:4px 0;border-bottom:1px solid #222">'
                    f'<span style="width:72px;color:#eee;font-weight:600">'
                    f'${float(r["strike"]):.1f}</span>'
                    f'{_volume_split_gauge_html(int(r["call_vol"]), int(r["put_vol"]))}'
                    f'<span style="color:#888;width:90px;text-align:right">'
                    f'{int(r["total_vol"]):,}</span>{mag}</div>'
                )
            html_rows.append("</div>")
            st.markdown("\n".join(html_rows), unsafe_allow_html=True)

        # Keep legacy side-by-side Δ view when previous archive exists
        if vol_prev:
            with st.expander("Δ vs previous run (Calls | Puts)", expanded=False):
                _render_expiry_side_by_side_delta(vol_curr, expiry, vol_prev)


def _render_expiry_side_by_side_delta(
    vol_curr: dict, expiry: str, vol_prev: dict
) -> None:
    """Original calls|puts side-by-side with ΔPrice / ΔVol."""
    def _filter(vol, key):
        return [c for c in (vol.get(key) or []) if c.get("expiry") == expiry] if vol else []

    def _signed(n: int | float, fmt_int: bool = True) -> str:
        if n > 0:
            return f"+{int(n):,}" if fmt_int else f"+{n:.2f}"
        if n < 0:
            return f"{int(n):,}" if fmt_int else f"{n:.2f}"
        return "·0"

    def _delta_style(val: str) -> str:
        s = str(val)
        if s.startswith("+"):
            return "color:#00c853;font-weight:bold"
        if s.startswith("-"):
            return "color:#d50000;font-weight:bold"
        return "color:#666"

    cc, pc_ = st.columns(2, gap="small")
    for col, curr_key, prev_key, label in [
        (cc,  "top_calls", "top_calls", "🟢 CALLS"),
        (pc_, "top_puts",  "top_puts",  "🔴 PUTS"),
    ]:
        curr_contracts = _filter(vol_curr, curr_key)
        prev_by_strike = {
            float(c.get("strike", 0)): c
            for c in _filter(vol_prev, prev_key)
        }
        with col:
            st.markdown(f"**{label}**")
            if not curr_contracts:
                st.caption("No contracts for this expiry.")
                continue
            rows = []
            for c in curr_contracts:
                strike = float(c.get("strike") or 0)
                vol = int(c.get("volume") or 0)
                oi = int(c.get("openInterest") or 0)
                price = float(c.get("lastPrice") or 0)
                iv = float(c.get("impliedVolatility") or 0)
                voi = vol / max(oi, 1)
                p = prev_by_strike.get(strike)
                d_price = price - float(p.get("lastPrice") or 0) if p else None
                d_vol = vol - int(p.get("volume") or 0) if p else None
                rows.append({
                    "Strike": f"${strike:.1f}",
                    "Price": f"${price:.2f}",
                    "ΔPrice": _signed(d_price, fmt_int=False) if d_price is not None else "new",
                    "Volume": f"{vol:,}",
                    "ΔVol": _signed(d_vol) if d_vol is not None else "new",
                    "OI": f"{oi:,}",
                    "VOL/OI": f"{voi:.2f}x 🔥" if voi >= 2 else f"{voi:.2f}x",
                    "IV": f"{iv:.1%}" if iv > 0 else "—",
                })
            df = pd.DataFrame(rows)
            styled = df.style.map(_delta_style, subset=["ΔPrice", "ΔVol"])
            st.dataframe(styled, use_container_width=True, hide_index=True)


def _render_expiry_vol_interactive(
    vol_curr: dict, vol_prev: dict | None, pc_ratio: float
) -> None:
    """
    Volume by Expiry table with clickable row selection (Tab 2).
    Clicking an expiry row shows a drill-down of its individual contracts.
    """
    curr: dict[str, dict] = {}
    for side_key, vol_key in [("call_vol", "top_calls"), ("put_vol", "top_puts")]:
        for c in (vol_curr.get(vol_key) or []):
            exp = c.get("expiry", "?")
            dte = int(c.get("dte", 0))
            v   = int(c.get("volume") or 0)
            if exp not in curr:
                curr[exp] = {"dte": dte, "call_vol": 0, "put_vol": 0}
            curr[exp][side_key] += v

    prev_agg: dict[str, dict] = {}
    if vol_prev:
        for side_key, vol_key in [("call_vol", "top_calls"), ("put_vol", "top_puts")]:
            for c in (vol_prev.get(vol_key) or []):
                exp = c.get("expiry", "?")
                v   = int(c.get("volume") or 0)
                prev_agg.setdefault(exp, {"call_vol": 0, "put_vol": 0})[side_key] += v

    if not curr:
        st.caption("No volume data in this archive.")
        return

    def _ds(v):
        if v is None: return ""
        if v > 0:     return f"+{v:,}"
        if v < 0:     return f"{v:,}"
        return "·0"

    rows, expiry_list = [], []
    for exp in sorted(curr):
        d  = curr[exp]
        cv, pv = d["call_vol"], d["put_vol"]
        dte = d["dte"]
        data_gap = (cv == 0 or pv == 0)
        pc = (pv / cv) if not data_gap else None

        if pc is None:   bias = ""
        elif pc < 0.7:   bias = "▲ BULLISH"
        elif pc < 0.9:   bias = "▲ MILD BULLISH"
        elif pc < 1.1:   bias = "─ NEUTRAL"
        elif pc < 1.5:   bias = "▼ MILD BEARISH"
        else:            bias = "▼ BEARISH"

        notable = (abs(pc - pc_ratio) > 0.25 if (pc is not None and pc_ratio > 0) else False)
        pd_     = prev_agg.get(exp, {})
        cv_d    = cv - pd_.get("call_vol", 0) if vol_prev else None
        pv_d    = pv - pd_.get("put_vol",  0) if vol_prev else None

        rows.append({
            "EXPIRY":   exp,
            "DTE":      f"{dte}d",
            "CALL VOL": f"{cv:,}",
            "PUT VOL":  f"{pv:,}",
            "P/C":      f"{pc:.2f}" if pc is not None else "n/a",
            "BIAS":     bias,
            "NOTABLE":  "⚠ data gap" if data_gap else ("◄ notable" if notable else ""),
            "CALL Δ":   _ds(cv_d),
            "PUT Δ":    _ds(pv_d),
            "_cv_d":    cv_d,
            "_pv_d":    pv_d,
        })
        expiry_list.append(exp)

    df   = pd.DataFrame(rows)
    dcols = ["EXPIRY","DTE","CALL VOL","PUT VOL","P/C","BIAS","NOTABLE","CALL Δ","PUT Δ"]
    if not vol_prev:
        dcols = [c for c in dcols if c not in ("CALL Δ","PUT Δ")]
    disp = df[dcols].copy()

    def _style_bias(val: str) -> str:
        if "BULL" in str(val): return "color:#00c853;font-weight:bold"
        if "BEAR" in str(val): return "color:#d50000;font-weight:bold"
        return "color:#9e9e9e"

    def _style_delta(val: str) -> str:
        s = str(val)
        if s.startswith("+"): return "color:#00c853;font-weight:bold"
        if s.startswith("-"): return "color:#d50000;font-weight:bold"
        return "color:#666"

    styled = disp.style.map(_style_bias, subset=["BIAS"])
    if vol_prev:
        styled = styled.map(_style_delta, subset=["CALL Δ", "PUT Δ"])

    st.markdown("### 📅 Expiration Summary")
    st.caption("Select a single expiry row to load the full strike-level option chain below.")

    # Prefer unstyled DF for reliable selection events on older Streamlit;
    # fall back gracefully if on_select is unavailable.
    try:
        event = st.dataframe(
            disp,
            on_select="rerun",
            selection_mode="single-row",
            use_container_width=True,
            hide_index=True,
            key="expiry_summary_select",
        )
        sel = []
        if event is not None and getattr(event, "selection", None) is not None:
            sel = list(event.selection.rows or [])
    except TypeError:
        st.dataframe(styled, use_container_width=True, hide_index=True)
        sel = []
        pick = st.selectbox(
            "Expiry detail",
            options=["—"] + expiry_list,
            key="expiry_summary_fallback",
        )
        if pick and pick != "—":
            sel = [expiry_list.index(pick)]

    if sel:
        idx = int(sel[0])
        if 0 <= idx < len(expiry_list):
            sel_exp = expiry_list[idx]
            st.markdown("---")
            _render_expiry_drill_down(vol_curr, sel_exp, vol_prev)
    else:
        st.info("👆 Click an expiry row in the summary table to open its option chain.")


@st.cache_data(ttl=120)
def _scan_archive_metadata() -> list[dict]:
    """
    Read minimal metadata from every archive file. Cached for 2 minutes.
    Only parses top-level JSON fields — no heavy rendering.
    """
    rows: list[dict] = []
    for pattern, atype in [
        ("archive/*.json",        "Daily"),
        ("archive_weekly/*.json", "Weekly"),
    ]:
        for fpath in sorted(glob.glob(pattern), reverse=True):
            fname = os.path.basename(fpath)
            try:
                parts    = fname.replace(".json", "").split("_")
                # parts[0]=TICKER, parts[1]=YYYYMMDD, parts[2]=HHMM
                run_date = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:]}"
                run_time = f"{parts[2][:2]}:{parts[2][2:4]}"
            except Exception:
                run_date, run_time = "?", "?"
            try:
                with open(fpath) as f:
                    p = json.load(f)
                spot      = p.get("spot")
                direction = p.get("direction", "—")
                pc_ratio  = (p.get("volume") or {}).get("pc_ratio")
                score     = p.get("checklist_score")
            except Exception:
                spot = direction = pc_ratio = score = None
            rows.append({
                "fpath":     fpath,
                "type":      atype,
                "date":      run_date,
                "time":      run_time,
                "spot":      spot,
                "direction": direction or "—",
                "pc_ratio":  pc_ratio,
                "score":     score,
            })
    return rows


def _render_best_value_archive_section(ticker: str | None = None) -> None:
    """Today's Best Value hit ledger — persistence counters + export controls."""
    ensure_archive_loaded()
    st.markdown("### ⭐ Best Value Hits — Today")
    st.caption(
        "Logged automatically each scanner refresh · "
        "`Times_Flagged` = how often the same contract appeared in today's top list · "
        "≥ 3 highlighted green (persistent institutional flow)"
    )

    arch = st.session_state.get("best_value_archive")
    today = filter_today(arch if isinstance(arch, pd.DataFrame) else None)

    persistent = most_persistent_today(today)
    m1, m2, m3 = st.columns(3)
    with m1:
        st.metric("Hits logged today", f"{len(today):,}" if today is not None else "0")
    with m2:
        uniq_n = 0
        if today is not None and not today.empty:
            uniq_n = today.drop_duplicates(
                subset=["Ticker", "Side", "Strike", "Expiry"]
            ).shape[0]
        st.metric("Unique contracts", f"{uniq_n:,}")
    with m3:
        if persistent:
            label, n = persistent
            st.metric("🔥 Most Persistent Contract Today", label, delta=f"{n}× flagged")
        else:
            st.metric("🔥 Most Persistent Contract Today", "—")

    b1, b2, _ = st.columns([1, 1, 2])
    with b1:
        if st.button("Clear Today's Log", key="bv_clear_today"):
            removed = clear_todays_log()
            st.success(f"Cleared {removed} row(s) from today's log.")
            st.rerun()
    with b2:
        st.download_button(
            "Export Archive to CSV",
            data=archive_csv_bytes(),
            file_name="best_value_archive.csv",
            mime="text/csv",
            key="bv_export_csv",
        )

    if today is None or today.empty:
        st.info("No Best Value hits recorded today yet. Open Options Flow after a scan to start logging.")
        return

    view = add_times_flagged(today)
    # Newest runs first
    view = view.sort_values("Run_Timestamp", ascending=False).reset_index(drop=True)
    if ticker:
        # Soft filter hint — still show all tickers; caption notes focus
        focus = ticker.upper()
        focus_n = int((view["Ticker"].astype(str).str.upper() == focus).sum())
        st.caption(f"Showing all tickers · {focus}: {focus_n} hit(s) today")

    show = view.copy()
    show["Strike"] = show["Strike"].apply(
        lambda x: f"${float(x):.1f}" if pd.notna(x) else "—"
    )
    show["Price"] = show["Price"].apply(
        lambda x: f"${float(x):.2f}" if pd.notna(x) else "—"
    )
    show["Value_Score"] = show["Value_Score"].apply(
        lambda x: f"{float(x):.4f}" if pd.notna(x) else "—"
    )
    show["Velocity"] = show["Velocity"].apply(
        lambda x: f"{float(x):+.4f}" if pd.notna(x) else "—"
    )

    cols = [
        "Run_Timestamp", "Ticker", "Side", "Strike", "Expiry",
        "Price", "Value_Score", "Velocity", "Signal", "Times_Flagged",
    ]
    show = show[cols]

    def _persist_row_style(row):
        try:
            if int(row.get("Times_Flagged") or 0) >= 3:
                return ["background-color:#1b5e20;color:#e8f5e9;font-weight:bold"] * len(row)
        except Exception:
            pass
        return [""] * len(row)

    styled = show.style.apply(_persist_row_style, axis=1)
    st.dataframe(styled, use_container_width=True, hide_index=True, height=360)


def _render_tab2(cfg: dict):
    ticker = cfg.get("ticker", "AAPL")

    # ── Best Value historical ledger (all tickers, focus caption for selected) ─
    _render_best_value_archive_section(ticker)
    st.markdown("---")

    # Load most recent daily archive for the selected ticker
    files = sorted(glob.glob(f"archive/{ticker}_*.json"), reverse=True)
    if not files:
        st.info(f"No archive data found for {ticker}. Run the scanner or pick a different ticker.")
        return

    try:
        with open(files[0]) as f:
            payload = json.load(f)
    except Exception as e:
        st.error(f"Could not read archive: {e}")
        return

    prev_payload = None
    if len(files) > 1:
        try:
            with open(files[1]) as f:
                prev_payload = json.load(f)
        except Exception:
            pass

    vol_curr = payload.get("volume") or {}
    vol_prev = (prev_payload.get("volume") or {}) if prev_payload else None
    pc_ratio = float(vol_curr.get("pc_ratio") or 0)

    try:
        ts_et = datetime.fromisoformat(payload.get("timestamp", "")).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        ts_et = "—"
    st.markdown("### 📅 Expiration Summary & Option Chain")
    st.caption(f"Most recent daily run · {ts_et}")

    if prev_payload:
        try:
            prev_et = datetime.fromisoformat(prev_payload.get("timestamp", "")).astimezone(ET).strftime("%H:%M ET")
        except Exception:
            prev_et = "previous run"
        st.caption(f"CALL Δ / PUT Δ vs {prev_et}  ·  green = higher  ·  red = lower")

    _render_expiry_vol_interactive(vol_curr, vol_prev, pc_ratio)

    st.markdown("---")
    st.markdown("### Theta & Gamma by Strike")
    st.caption("Black-Scholes greeks computed from archived IV · calls green · puts red · Δ vs previous run")
    _render_greeks_panel(
        vol_curr,
        float(payload.get("spot") or 0),
        vol_prev,
        float(prev_payload.get("spot") or 0) if prev_payload else 0.0,
    )


def _norm_cdf(x: float) -> float:
    import math
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

def _norm_pdf(x: float) -> float:
    import math
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)

def _bs_greeks(
    S: float, K: float, iv: float, dte_days: int,
    r: float = 0.05, is_call: bool = True,
) -> tuple[float | None, float | None]:
    """
    Black-Scholes gamma and theta.
    Returns (gamma, theta_per_calendar_day).
    Theta is in dollars (negative = time decay cost per day).
    Returns (None, None) when inputs are invalid (e.g. 0DTE, zero IV).
    """
    import math
    if dte_days <= 0 or iv <= 0.001 or S <= 0 or K <= 0:
        return None, None
    try:
        T    = dte_days / 365.0
        sqT  = math.sqrt(T)
        d1   = (math.log(S / K) + (r + 0.5 * iv ** 2) * T) / (iv * sqT)
        d2   = d1 - iv * sqT
        nd1  = _norm_pdf(d1)
        disc = math.exp(-r * T)

        gamma = nd1 / (S * iv * sqT)

        theta_annual = -(S * nd1 * iv) / (2 * sqT)
        if is_call:
            theta_annual -= r * K * disc * _norm_cdf(d2)
        else:
            theta_annual += r * K * disc * _norm_cdf(-d2)

        return gamma, theta_annual / 365.0
    except Exception:
        return None, None


def _render_greeks_panel(
    vol_curr: dict, spot: float,
    vol_prev: dict | None = None, prev_spot: float = 0.0,
) -> None:
    """
    Theta & Gamma by strike — computed from Black-Scholes using archive IV.
    When vol_prev/prev_spot are provided, adds ΔGamma and ΔTheta columns:
    green = increased (gamma up / theta less negative), red = decreased.
    No live fetches. 0DTE contracts show '—' for greeks.
    """
    import altair as alt

    # Build prev lookup: (side, strike, expiry) → contract
    prev_lookup: dict[tuple, dict] = {}
    if vol_prev:
        for side_key, vol_key in [("CALL","top_calls"), ("PUT","top_puts")]:
            for c in (vol_prev.get(vol_key) or []):
                k = (side_key, float(c.get("strike",0)), c.get("expiry",""))
                prev_lookup[k] = c

    def _signed_greek(v: float | None, precision: int = 5) -> str:
        if v is None: return "—"
        fmt = f"+{v:.{precision}f}" if v > 0 else f"{v:.{precision}f}"
        return fmt

    def _delta_style(val: str) -> str:
        s = str(val)
        if s.startswith("+"): return "color:#00c853;font-weight:bold"
        if s.startswith("-"): return "color:#d50000;font-weight:bold"
        return "color:#666"

    rows = []
    for side, vol_key, is_call in [
        ("CALL", "top_calls", True),
        ("PUT",  "top_puts",  False),
    ]:
        for c in (vol_curr.get(vol_key) or []):
            strike = float(c.get("strike") or 0)
            iv     = float(c.get("impliedVolatility") or 0)
            dte    = int(c.get("dte") or 0)
            expiry = c.get("expiry", "?")
            price  = float(c.get("lastPrice") or 0)

            gamma, theta = _bs_greeks(spot, strike, iv, dte, is_call=is_call)

            # Previous greeks
            p = prev_lookup.get((side, strike, expiry))
            if p and prev_spot > 0:
                p_iv  = float(p.get("impliedVolatility") or 0)
                p_dte = int(p.get("dte") or 0)
                pg, pt = _bs_greeks(prev_spot, strike, p_iv, p_dte, is_call=is_call)
            else:
                pg, pt = None, None

            d_gamma = (gamma - pg) if (gamma is not None and pg is not None) else None
            d_theta = (theta - pt) if (theta is not None and pt is not None) else None

            row = {
                "Side":    side,
                "Strike":  f"${strike:.1f}",
                "Expiry":  expiry,
                "DTE":     f"{dte}d",
                "Price":   f"${price:.2f}",
                "IV":      f"{iv:.1%}" if iv > 0 else "—",
                "Gamma":   f"{gamma:.5f}" if gamma is not None else "—",
                "Theta/d": f"${theta:.4f}" if theta is not None else "—",
                "_strike": strike,
                "_gamma":  gamma,
                "_theta":  theta,
            }
            if vol_prev:
                row["ΔGamma"] = _signed_greek(d_gamma, 5)
                row["ΔTheta"] = _signed_greek(d_theta, 4)
            rows.append(row)

    if not rows:
        st.caption("No contract data for greeks.")
        return

    df = pd.DataFrame(rows)

    def _side_style(val: str) -> str:
        if val == "CALL": return "color:#00c853;font-weight:bold"
        if val == "PUT":  return "color:#d50000;font-weight:bold"
        return ""

    display_cols = ["Side","Strike","Expiry","DTE","Price","IV","Gamma","Theta/d"]
    if vol_prev:
        display_cols = ["Side","Strike","Expiry","DTE","Price","IV",
                        "Gamma","ΔGamma","Theta/d","ΔTheta"]

    styled = df[display_cols].style.map(_side_style, subset=["Side"])
    if vol_prev:
        styled = styled.map(_delta_style, subset=["ΔGamma", "ΔTheta"])
    st.dataframe(styled, use_container_width=True, hide_index=True)

    # ── Altair charts: Gamma | Theta by strike ─────────────────────────────
    chart_df = df[df["_gamma"].notna()].copy()
    if chart_df.empty:
        st.caption("All contracts are 0DTE — greeks undefined.")
        return

    color_scale = alt.Scale(domain=["CALL","PUT"], range=["#00c853","#d50000"])

    def _greek_chart(field: str, title: str, fmt: str) -> alt.Chart:
        return (
            alt.Chart(chart_df)
            .mark_bar(opacity=0.85)
            .encode(
                x=alt.X("_strike:Q", title="Strike ($)",
                         axis=alt.Axis(format="$.0f")),
                y=alt.Y(f"{field}:Q", title=title),
                color=alt.Color("Side:N", scale=color_scale,
                                legend=alt.Legend(title=None, orient="top-right")),
                xOffset="Side:N",
                tooltip=[
                    alt.Tooltip("Side:N"),
                    alt.Tooltip("_strike:Q", title="Strike", format="$.1f"),
                    alt.Tooltip(f"{field}:Q", title=title, format=fmt),
                    alt.Tooltip("Expiry:N"),
                    alt.Tooltip("IV:N"),
                ],
            )
            .properties(title=title, height=220)
        )

    gc, tc = st.columns(2, gap="small")
    with gc:
        st.altair_chart(_greek_chart("_gamma", "Gamma", ".5f"), use_container_width=True)
    with tc:
        st.altair_chart(_greek_chart("_theta", "Theta ($/day)", ".4f"), use_container_width=True)

    st.caption(
        "Gamma: rate of delta change per $1 move in spot.  "
        "Theta: option value lost per calendar day (negative = cost).  "
        "Computed from Black-Scholes using archived IV — not live quotes."
    )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — Ticker Manager
# ══════════════════════════════════════════════════════════════════════════════

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


def _render_tab4() -> None:
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


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — Spread gate
# ══════════════════════════════════════════════════════════════════════════════

def _render_tab3(cfg: dict):
    st.header("Spread Gate Evaluator")
    st.caption(
        "Parameters → evaluate_spread_gate() → verdict rendered verbatim. "
        "No UI logic scores or approves trades."
    )

    # Prefill spot from latest archive
    latest     = cfg["latest_archive"]
    default_spot = float(latest.get("spot", 200.0)) if latest else 200.0

    today_str  = date.today().isoformat()

    with st.form("gate_form"):
        st.subheader("Spread parameters")
        col1, col2, col3 = st.columns(3)
        with col1:
            spot          = st.number_input("Spot ($)",          value=default_spot,  step=0.5,  format="%.2f")
            iv            = st.number_input("IV (decimal)",      value=0.28,          step=0.01, format="%.3f")
            risk_free     = st.number_input("Risk-free rate",    value=0.045,         step=0.005,format="%.3f")
        with col2:
            long_strike   = st.number_input("Long strike ($)",   value=round(default_spot)+2.5, step=2.5, format="%.1f")
            short_strike  = st.number_input("Short strike ($)",  value=round(default_spot)+12.5,step=2.5, format="%.1f")
            commission    = st.number_input("Commission ($/rt)", value=3.0,           step=0.5,  format="%.2f")
        with col3:
            long_premium  = st.number_input("Long premium ($)",  value=3.00,          step=0.05, format="%.2f")
            short_premium = st.number_input("Short premium ($)", value=1.50,          step=0.05, format="%.2f")

        st.subheader("Dates")
        dcol1, dcol2, dcol3 = st.columns(3)
        with dcol1:
            entry_date  = st.date_input("Entry date",      value=date.today())
        with dcol2:
            exit_date   = st.date_input("Hard exit date",  value=date.today())
        with dcol3:
            expiry_date = st.date_input("Expiration date", value=date.today())

        st.subheader("Thresholds")
        th1, th2 = st.columns(2)
        with th1:
            min_pop = st.number_input("Min PoP", value=0.40, step=0.01, format="%.2f")
        with th2:
            min_ev  = st.number_input("Min EV ($/contract)", value=0.0, step=1.0, format="%.2f")

        submitted = st.form_submit_button("Evaluate spread →", type="primary", use_container_width=True)

    if submitted:
        result = evaluate_spread_gate(
            spot=spot,
            iv=iv,
            long_strike=long_strike,
            short_strike=short_strike,
            long_premium=long_premium,
            short_premium=short_premium,
            entry_date=entry_date.isoformat(),
            exit_date=exit_date.isoformat(),
            expiration=expiry_date.isoformat(),
            risk_free_rate=risk_free,
            commission_per_contract=commission,
            min_pop=min_pop,
            min_ev_per_contract=min_ev,
        )

        # Verdict badge
        verdict = result["verdict"]
        if verdict == "TRADE":
            st.success(f"## ✅ {verdict}", icon="✅")
        else:
            st.error(f"## 🚫 {verdict}", icon="🚫")

        # Metrics
        mc1, mc2 = st.columns(2)
        pop_val = result.get("pop")
        ev_val  = result.get("ev_per_contract")
        mc1.metric("Probability of profit", f"{pop_val:.1%}" if pop_val is not None else "n/a")
        mc2.metric("EV per contract",       f"${ev_val:+.2f}" if ev_val is not None else "n/a")

        # Reasons (verbatim from gate)
        reasons = result.get("reasons", [])
        if reasons:
            st.subheader("Failed checks")
            for r in reasons:
                st.markdown(f"- {r}")

        # Persist to history
        history = ss.load_gate_history()
        history.insert(0, {
            "timestamp": datetime.now(ET).isoformat(),
            "inputs": {
                "spot": spot, "iv": iv,
                "long_strike": long_strike, "short_strike": short_strike,
                "long_premium": long_premium, "short_premium": short_premium,
                "entry_date": entry_date.isoformat(),
                "exit_date": exit_date.isoformat(),
                "expiration": expiry_date.isoformat(),
            },
            "verdict":       verdict,
            "pop":           pop_val,
            "ev_per_contract": ev_val,
            "reasons":       reasons,
        })
        ss.save_gate_history(history)

    # ── History table ─────────────────────────────────────────────────────────
    history = ss.load_gate_history()
    if history:
        st.subheader("Last evaluations")
        rows = []
        for h in history:
            ts = datetime.fromisoformat(h["timestamp"]).strftime("%Y-%m-%d %H:%M ET")
            verdict = h.get("verdict", "?")
            pop = h.get("pop")
            ev  = h.get("ev_per_contract")
            n_reasons = len(h.get("reasons", []))
            rows.append({
                "Time (ET)":    ts,
                "Verdict":      verdict,
                "PoP":          f"{pop:.1%}" if pop is not None else "—",
                "EV":           f"${ev:+.2f}" if ev is not None else "—",
                "Failed checks": n_reasons,
                "Long / Short": f"{h['inputs'].get('long_strike','?')} / {h['inputs'].get('short_strike','?')}",
            })

        hist_df = pd.DataFrame(rows)

        def _verdict_color(row):
            c = "#1b3a1b" if row["Verdict"] == "TRADE" else "#3a1b1b"
            return [f"background-color:{c}"] * len(row)

        st.dataframe(
            hist_df.style.apply(_verdict_color, axis=1),
            use_container_width=True,
            hide_index=True,
        )


def _fmt_news_ts(ts: int) -> str:
    """Unix seconds → readable local string; em-dash if missing."""
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "—"


def _render_tab5(cfg: dict) -> None:
    """Market News — timeline for the sidebar-selected ticker only."""
    ticker = (cfg.get("ticker") or "").strip().upper()
    if not ticker:
        st.info("Pick a ticker in the sidebar.")
        return

    st.caption(f"Headlines for **{ticker}** (same ticker as the sidebar selector).")
    articles = _cached_market_news((ticker,), limit=15)
    if not articles:
        st.info(f"No headlines found for {ticker}.")
        return

    _badge = {
        "BULLISH": ("#00c853", "#0d2818"),
        "BEARISH": ("#d50000", "#2a1010"),
        "NEUTRAL": ("#9e9e9e", "#1a1a1a"),
    }

    st.markdown(f"### Latest {len(articles)} headlines — {ticker}")
    for a in articles:
        bias = a.get("news_bias") or "NEUTRAL"
        fg, bg = _badge.get(bias, _badge["NEUTRAL"])
        headline = (a.get("headline") or "(no title)").replace("<", "&lt;").replace(">", "&gt;")
        url = (a.get("url") or "").replace('"', "&quot;")
        source = (a.get("source") or "Unknown").replace("<", "&lt;")
        when = _fmt_news_ts(int(a.get("datetime") or 0))
        if url:
            title_html = (
                f'<a href="{url}" target="_blank" rel="noopener noreferrer" '
                f'style="color:#e3e3e3;text-decoration:none">{headline}</a>'
            )
        else:
            title_html = f'<span style="color:#e3e3e3">{headline}</span>'

        st.markdown(
            f'<div style="border-left:3px solid {fg};padding:0.7rem 1rem;'
            f'margin:0.5rem 0;background:{bg};border-radius:0 6px 6px 0">'
            f'<div style="display:flex;gap:0.65rem;align-items:center;flex-wrap:wrap;'
            f'margin-bottom:0.35rem">'
            f'<span style="font-size:0.72rem;font-weight:700;color:{fg};'
            f'border:1px solid {fg};padding:0.12rem 0.5rem;border-radius:4px">'
            f'{bias}</span>'
            f'<span style="color:#888;font-size:0.85rem">{source} · {when}</span>'
            f'</div>'
            f'<div style="font-size:1.05rem;font-weight:600;line-height:1.35">'
            f'{title_html}</div>'
            f'</div>',
            unsafe_allow_html=True,
        )


def _fmt_journal_money(x) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "—"
    try:
        return f"${float(x):+,.0f}"
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


def _render_tab_journal() -> None:
    """Trade journal — bought / sold options with performance tracking."""
    st.markdown("### Trade Journal")
    st.caption(
        "Tracks options you **buy** (＋ on Best Value) and **sell** (− close). "
        "Each day is saved to `data/journal/YYYY-MM-DD.json`."
    )

    # Ensure existing ledger trades exist as daily files (idempotent)
    try:
        portfolio_store.backfill_daily_journal_from_ledgers()
    except Exception:
        pass

    open_df = st.session_state.get("portfolio_df")
    if open_df is None:
        open_df = portfolio_store.load_portfolio()
        st.session_state["portfolio_df"] = open_df

    journal = portfolio_store.journal_dataframe(open_df=open_df)
    stats = portfolio_store.journal_performance(journal)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Closed trades", stats["n_closed"])
    m2.metric(
        "Win rate",
        "—" if stats["win_rate"] is None else f"{stats['win_rate']:.0%}",
        help=f"{stats['wins']} wins / {stats['losses']} losses",
    )
    m3.metric(
        "Realized PnL",
        f"${stats['total_realized_pnl']:+,.0f}",
    )
    m4.metric(
        "Avg PnL %",
        "—" if stats["avg_pnl_pct"] is None else f"{stats['avg_pnl_pct']:+.1%}",
    )
    m5.metric(
        "Open / unrealized",
        f"{stats['n_open']} · ${stats['unrealized_pnl']:+,.0f}",
    )

    st.markdown("#### Daily record")
    days = portfolio_store.list_journal_days()
    today = portfolio_store.today_et()
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
    day_stats = portfolio_store.day_performance(selected_day)
    with d2:
        st.caption(
            f"File: `{os.path.relpath(day_stats['file'], os.path.dirname(os.path.abspath(__file__)))}` "
            f"· buys {day_stats['n_buys']} · sells {day_stats['n_sells']} · "
            f"day PnL ${day_stats['realized_pnl']:+,.0f}"
        )

    day_df = portfolio_store.load_journal_day(selected_day)
    if day_df.empty:
        st.info(f"No buy/sell events saved for {selected_day} yet.")
    else:
        day_show = day_df.copy()
        day_show["When"] = day_show["At"].map(_fmt_journal_ts)
        day_show["Price $"] = day_show["Price"].map(
            lambda x: f"${float(x):.2f}" if pd.notna(x) else "—"
        )
        day_show["Strike"] = day_show["Strike"].map(
            lambda x: f"${float(x):.1f}" if pd.notna(x) else "—"
        )
        day_show["Qty"] = day_show["Quantity"].map(
            lambda x: f"{float(x):.0f}" if pd.notna(x) else "—"
        )
        day_show["PnL %"] = day_show["PnL_Pct"].map(_fmt_journal_pct)
        day_show["PnL $"] = day_show["PnL_Dollars"].map(_fmt_journal_money)
        st.dataframe(
            day_show[
                ["Action", "Ticker", "Side", "Strike", "Expiry", "Qty",
                 "When", "Price $", "PnL %", "PnL $"]
            ],
            use_container_width=True,
            hide_index=True,
            height=min(280, 48 + 36 * len(day_show)),
        )
        st.download_button(
            f"Download {selected_day} JSON",
            data=json.dumps(
                day_df.drop(columns=["Day"], errors="ignore").to_dict(orient="records"),
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
        lambda r: _fmt_journal_pct(
            r["PnL_Pct"] if r["Status"] == "CLOSED" else r["Unrealized_Pct"]
        ),
        axis=1,
    )
    show["PnL $"] = show.apply(
        lambda r: _fmt_journal_money(
            r["PnL_Dollars"] if r["Status"] == "CLOSED" else r["Unrealized_Dollars"]
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
            lambda x: f"${float(x):+,.0f}" if pd.notna(x) else "—"
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


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    cfg = _sidebar()

    # Service-down alerts sit above tabs so they're visible on every page
    _services_alert()

    tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs(_main_tab_labels())

    with tab1:
        _market_banner()
        _render_tab1(cfg)

    with tab2:
        _market_banner()
        _render_tab2(cfg)

    with tab3:
        _market_banner()
        _render_tab3(cfg)

    with tab4:
        _render_tab4()

    with tab5:
        _render_tab5(cfg)

    with tab6:
        _render_tab_journal()


if __name__ == "__main__" or True:
    main()
