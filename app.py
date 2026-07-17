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
import time
from datetime import datetime, date
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

import data_adapter
import snapshot_store as ss
from spread_gate import evaluate_spread_gate
from dailyScaner import market_is_open, proximity_filter, MIN_OI_FOR_MAGNET

ET = ZoneInfo("America/New_York")

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="AAPL Options Scanner",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Shared helpers ─────────────────────────────────────────────────────────────

def _now_et() -> datetime:
    return datetime.now(ET)


def _latest_archive() -> dict | None:
    files = sorted(glob.glob("archive/AAPL_*.json"), reverse=True)
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
    now = _now_et()
    if not market_is_open(now):
        st.warning(
            f"⚠️  MARKET CLOSED — data is end-of-day  "
            f"({now.strftime('%A %H:%M ET')})",
            icon="🔴",
        )


# ── Sidebar ───────────────────────────────────────────────────────────────────

_SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))


def _run_daily_scanner() -> tuple[bool, str]:
    """
    Invoke dailyScaner.py with the same Python that runs Streamlit.
    Returns (success, combined_output).
    """
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, "dailyScaner.py"],
            cwd=_SCANNER_DIR,
            capture_output=True,
            text=True,
            timeout=1800,   # 30-min hard cap
        )
        elapsed = time.time() - t0
        out = result.stdout + ("\n" + result.stderr if result.stderr.strip() else "")
        return result.returncode == 0, f"[{elapsed:.0f}s]\n{out}"
    except subprocess.TimeoutExpired:
        return False, "Scanner timed out after 30 minutes."
    except Exception as exc:
        return False, f"Could not launch scanner: {exc}"


def _sidebar() -> dict:
    with st.sidebar:
        st.title("📊 AAPL Scanner")
        st.caption("Display layer — results from archive JSONs")
        st.divider()

        st.markdown("**Daily scan**")
        run_scan = st.button(
            "🚀 Run Daily Scan",
            use_container_width=True,
            type="primary",
            help="Runs dailyScaner.py and saves a new archive JSON",
        )

        if run_scan:
            with st.spinner("Running daily scanner… (may take a few minutes)"):
                ok, output = _run_daily_scanner()
            if ok:
                st.success("Scan complete — archive updated.")
                _scan_archive_metadata.clear()   # bust cache so Tab 2 reloads
            else:
                st.error("Scanner returned an error.")
            with st.expander("Scanner output", expanded=not ok):
                st.code(output[-4000:], language="text")   # last 4 KB

        st.divider()
        st.subheader("Flow filters")
        min_dte = st.number_input("Min DTE", min_value=0, value=1, step=1)
        sort_by = st.selectbox("Sort by", ["Volume", "Premium $", "Strike"])

        st.divider()
        latest = _latest_archive()
        if latest:
            ts = datetime.fromisoformat(latest["timestamp"]).astimezone(ET)
            st.caption(f"Last archive: {ts.strftime('%Y-%m-%d %H:%M ET')}")
            st.caption(f"Spot at run: ${latest.get('spot', '—')}")

    return {
        "run": run_scan,
        "min_dte": min_dte,
        "sort_by": sort_by,
        "latest_archive": latest,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Latest Run (full rich report from most recent archive JSON)
# ══════════════════════════════════════════════════════════════════════════════

def _load_archive_chain() -> tuple[pd.DataFrame, dict | None]:
    """Return per-contract DataFrame + raw payload for the latest daily archive."""
    files = sorted(glob.glob("archive/AAPL_*.json"), reverse=True)
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
        cv, pv = d["call_vol"], d["put_vol"]
        dte    = d["dte"]
        pc     = pv / cv if cv > 0 else 0
        chart_pc[exp] = pc

        if pc < 0.7:   bias = "▲ BULLISH"
        elif pc < 0.9: bias = "▲ MILD BULLISH"
        elif pc < 1.1: bias = "— NEUTRAL"
        elif pc < 1.5: bias = "▼ MILD BEARISH"
        else:          bias = "▼ BEARISH"

        notable = abs(pc - overall_pc) > 0.25 if overall_pc > 0 else False

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
            "P/C":      f"{pc:.2f}",
            "BIAS":     bias,
            "NOTABLE":  "◄ notable" if notable else "",
            "CALL Δ":   _ds(cv_d),
            "PUT Δ":    _ds(pv_d),
        })

    return rows, chart_pc


def _render_tab1(cfg: dict):
    """Options Flow — Magnets heatmap + Volume-by-Expiry term structure."""

    files = sorted(glob.glob("archive/AAPL_*.json"), reverse=True)
    if not files:
        st.info("No archive data found. Run the scanner first (`python dailyScaner.py`).")
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

    # ── Top bar ───────────────────────────────────────────────────────────────
    dir_color = "#00c853" if "BULL" in direction else "#d50000" if "BEAR" in direction else "#9e9e9e"
    dir_icon  = "▲" if "BULL" in direction else "▼" if "BEAR" in direction else "─"
    pc_bias   = "BULLISH SKEW" if pc_ratio < 0.7 else ("BEARISH SKEW" if pc_ratio > 1.0 else "NEUTRAL")

    if ts_str:
        ts_et = datetime.fromisoformat(ts_str).astimezone(ET)
        st.caption(f"Last run: **{ts_et.strftime('%Y-%m-%d %H:%M ET')}**")

    st.markdown(
        f'<div style="background:#1a1a2e;padding:0.75rem 1.5rem;border-radius:8px;margin-bottom:0.5rem">'
        f'<span style="font-size:1.5rem;font-weight:900;color:{dir_color}">{dir_icon} {direction}</span>'
        f'&ensp;<span style="font-size:1.1rem;color:#eee">Spot ${spot:.2f}</span>'
        f'&ensp;<span style="color:#aaa">P/C {pc_ratio:.2f} ← {pc_bias}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Spot",        f"${spot:.2f}")
    m2.metric("Call Volume", f"{int(vol.get('total_call_vol') or 0):,}")
    m3.metric("Put Volume",  f"{int(vol.get('total_put_vol')  or 0):,}")
    m4.metric("P/C Ratio",   f"{pc_ratio:.2f}")

    st.markdown("---")

    # ══ Two-column main layout ════════════════════════════════════════════════
    left, right = st.columns([1, 1.4], gap="large")

    # ── LEFT: The Magnets ─────────────────────────────────────────────────────
    with left:
        st.markdown("### The Magnets — Top 5 Calls / Top 5 Puts")
        st.caption("🔥 Vol/OI heatmap — values ≥ 2.0x glow hot (unusual vs open interest)")

        for key, label in [("top_calls", "Top 10 CALLS"), ("top_puts", "Top 10 PUTS")]:
            contracts = vol.get(key) or []
            st.markdown(f"**{label}**")
            styled = _contracts_table(contracts, n=10)
            if styled is not None:
                st.dataframe(styled, use_container_width=True, hide_index=True)
            else:
                st.caption("No data")

    # ── RIGHT: Volume by Expiry ───────────────────────────────────────────────
    with right:
        st.markdown("### Volume by Expiry — P/C term structure")
        st.caption("Institutional-style skew across the expiry curve")

        exp_rows, chart_pc = _build_expiry_table(vol, prev_vol, pc_ratio)

        if exp_rows:
            exp_df = pd.DataFrame(exp_rows)

            def _bias_style(val: str) -> str:
                if "BULL" in str(val): return "color:#00c853;font-weight:bold"
                if "BEAR" in str(val): return "color:#d50000;font-weight:bold"
                return "color:#9e9e9e"

            def _delta_style(val: str) -> str:
                s = str(val)
                if s.startswith("▲"): return "color:#00c853"
                if s.startswith("▼"): return "color:#d50000"
                return "color:#666"

            styled_exp = (
                exp_df.style
                .map(_bias_style,   subset=["BIAS"])
                .map(_delta_style,  subset=["CALL Δ", "PUT Δ"])
            )
            st.dataframe(styled_exp, use_container_width=True, hide_index=True)

            # Bar chart — P/C ratio by expiry
            if chart_pc:
                chart_df = pd.DataFrame(
                    {"P/C ratio": list(chart_pc.values())},
                    index=list(chart_pc.keys()),
                )
                st.bar_chart(chart_df, height=200, color="#1565c0")
        else:
            st.caption("No expiry data available")

    # ══ Collapsible detail sections ═══════════════════════════════════════════
    with st.expander("📊 Multi-Timeframe Detail", expanded=False):
        tf_rows = []
        for tf in ["5M", "10M", "15M", "1H", "4H", "1D"]:
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

    if prev:
        prev_spot = float(prev.get("spot") or 0)
        prev_pc   = float((prev.get("volume") or {}).get("pc_ratio") or 0)
        try:
            prev_ts_str = datetime.fromisoformat(prev.get("timestamp", "")).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
        except Exception:
            prev_ts_str = "previous run"
        spot_chg = spot - prev_spot
        pc_chg   = pc_ratio - prev_pc
        pct_chg  = (spot_chg / prev_spot * 100) if prev_spot else 0

        with st.expander(f"📈 Changes vs last run  (since {prev_ts_str})", expanded=False):
            ca, cb = st.columns(2)
            with ca:
                st.metric("Spot",      f"${spot:.2f}",    delta=f"{spot_chg:+.2f} ({pct_chg:+.1f}%)")
                st.metric("P/C Ratio", f"{pc_ratio:.3f}", delta=f"{pc_chg:+.3f}")
            with cb:
                rsi_lines = []
                for tf in ["5M", "10M", "15M", "1H", "4H", "1D"]:
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
        or1, or2 = st.columns(2)
        for col, tf_key in [(or1, "5M"), (or2, "15M")]:
            or_tf = or_data.get(tf_key) or {}
            with col:
                if or_tf:
                    bias     = or_tf.get("bias", "—")
                    bias_dir = or_tf.get("bias_dir", "")
                    color    = "#00c853" if bias_dir == "bull" else "#d50000" if bias_dir == "bear" else "#aaa"
                    st.markdown(
                        f'<div style="border:1px solid #333;padding:0.75rem 1rem;border-radius:6px">'
                        f'<b>{tf_key} OR</b> ({or_tf.get("open_time","?")} ET)<br>'
                        f'<span style="color:{color};font-weight:bold;font-size:1.1rem">{bias}</span><br>'
                        f'<span style="color:#888;font-size:0.85rem">'
                        f'Open ${or_tf.get("open",0):.2f} &nbsp; '
                        f'High ${or_tf.get("high",0):.2f} &nbsp; '
                        f'Low ${or_tf.get("low",0):.2f} &nbsp; '
                        f'Range ${or_tf.get("range",0):.2f} ({or_tf.get("range_pct",0):.2f}%)'
                        f'</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.caption(f"No {tf_key} OR data in this archive.")


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
    """Checklist card layout for daily scanner archives."""
    direction = payload.get("direction")          # "BULLISH" / "BEARISH" / "NEUTRAL" / None
    or_data   = payload.get("or_data") or {}
    tfs       = payload.get("timeframes") or {}
    vol       = payload.get("volume") or {}
    magnets   = payload.get("signal_magnets") or {}

    or_15m    = or_data.get("15M") or {}
    or_5m     = or_data.get("5M")  or {}
    pc_ratio  = vol.get("pc_ratio")
    rsi_1d    = tfs.get("1D", {}).get("rsi")
    rsi_1h    = tfs.get("1H", {}).get("rsi")

    # ── 1. Daily Trend ───────────────────────────────────────────────────────
    bias_dir  = or_15m.get("bias_dir")           # "bull" / "bear" / "neutral" / "forming"
    trend_pass = (direction == "BULLISH") if direction else None
    or_bias   = or_15m.get("bias", "—")
    or_5m_bias = or_5m.get("bias", "—")
    trend_sum = (
        f"Direction={direction or '—'};  15M OR: {or_bias};  5M OR: {or_5m_bias}"
    )
    trend_det = (
        f"**Direction (engine):** {direction or '—'}<br>"
        f"**15M OR:** H={or_15m.get('high','—')}  L={or_15m.get('low','—')}  "
        f"Range={or_15m.get('range','—')}pt ({or_15m.get('range_pct','—')}%)  "
        f"Bias: **{or_bias}**<br>"
        f"**5M OR:** H={or_5m.get('high','—')}  L={or_5m.get('low','—')}  "
        f"Bias: **{or_5m_bias}**"
    )

    # ── 2. P/C Skew ──────────────────────────────────────────────────────────
    pc_pass   = (pc_ratio < 0.95) if pc_ratio is not None else None
    pc_sum    = f"Near-term P/C={pc_ratio:.2f} (want < 0.95 for calls)" if pc_ratio is not None else "—"
    tc = vol.get("total_call_vol", 0); tp = vol.get("total_put_vol", 0)
    pc_det    = (
        f"**P/C ratio:** {pc_ratio if pc_ratio is not None else '—'}<br>"
        f"**Total call vol:** {int(tc):,} &nbsp; **Total put vol:** {int(tp):,}"
    )

    # ── 3. Vol/OI ≥ 2x ───────────────────────────────────────────────────────
    best_voi  = None
    best_side = None
    voi_lines = []
    for side in ("call", "put"):
        m = magnets.get(side)
        if m and m.get("openInterest", 0) > 0:
            voi = m["volume"] / m["openInterest"]
            voi_lines.append(
                f"**{side.upper()}** ${m.get('strike','?')} exp {m.get('expiry','?')} "
                f"DTE {m.get('dte','?')}  Vol={int(m['volume']):,}  "
                f"OI={int(m['openInterest']):,}  **Vol/OI={voi:.1f}x**"
            )
            if best_voi is None or voi > best_voi:
                best_voi = voi; best_side = side
        else:
            voi_lines.append(f"**{side.upper()}** — none qualified")
    voi_pass = (best_voi >= 2.0) if best_voi is not None else None
    voi_sum  = f"Max Vol/OI={best_voi:.2f}x ({best_side})" if best_voi is not None else "—"
    voi_det  = "<br>".join(voi_lines) if voi_lines else "No magnet data in this archive."

    # ── 4. RSI Health (1D not overbought) ────────────────────────────────────
    rsi_pass  = (rsi_1d < 70) if rsi_1d is not None else None
    rsi_sum   = (
        f"1D RSI={rsi_1d:.1f}  1H RSI={rsi_1h:.1f}" if rsi_1d and rsi_1h
        else f"1D RSI={rsi_1d}" if rsi_1d else "—"
    )
    rsi_rows  = "".join(
        f"<b>{tf}</b>: RSI {_rsi_label(d.get('rsi'))} &nbsp; MACD hist {d.get('hist','—')}<br>"
        for tf, d in tfs.items()
    )
    rsi_det   = rsi_rows or "No timeframe data in this archive."

    # ── 5. OR Clear (15M not inside range) ───────────────────────────────────
    or_clear_pass = (bias_dir in ("bull", "bear")) if bias_dir else None
    or_clear_sum  = f"15M OR bias: {or_bias}" if or_bias != "—" else "OR data not in this archive"
    or_clear_det  = (
        f"15M OR high {or_15m.get('high','—')} / low {or_15m.get('low','—')}<br>"
        f"Range {or_15m.get('range','—')}pt — **{or_bias}**<br>"
        f"Open time {or_15m.get('open_time','—')} ET"
    )

    # ── Banner ───────────────────────────────────────────────────────────────
    checks   = [trend_pass, pc_pass, voi_pass, rsi_pass, or_clear_pass]
    n_pass   = sum(1 for c in checks if c is True)
    n_scored = sum(1 for c in checks if c is not None)
    dir_color = {"BULLISH": "#00c853", "BEARISH": "#d50000"}.get(direction, "#9e9e9e")
    if direction == "BULLISH" and n_pass >= 4:
        banner_bg, banner_icon, verdict_txt = "#1a3a1a", "✅", "GO"
    elif direction == "BEARISH" or n_pass <= 1:
        banner_bg, banner_icon, verdict_txt = "#3a1a1a", "🔴", "CAUTION"
    else:
        banner_bg, banner_icon, verdict_txt = "#2a2a1a", "⚠️", "MIXED"

    st.markdown(
        f'<div style="background:{banner_bg};padding:1rem 1.5rem;border-radius:8px;margin-bottom:1rem">'
        f'<span style="font-size:1.4rem;font-weight:900;color:{dir_color}">'
        f'{banner_icon} {verdict_txt} &nbsp;·&nbsp; {n_pass}/{n_scored} checks &nbsp;·&nbsp; '
        f'{direction or "—"}'
        f'</span><br>'
        f'<span style="color:#aaa;font-size:0.85rem">Daily scanner · {run_time} ET · Spot ${spot}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.caption("Daily Scanner — 5-Point Snapshot")

    cols = st.columns(5)
    items = [
        ("Daily Trend",   trend_pass,     trend_sum,     trend_det),
        ("P/C Skew",      pc_pass,        pc_sum,        pc_det),
        ("Vol/OI ≥ 2x",  voi_pass,       voi_sum,       voi_det),
        ("RSI < 70",      rsi_pass,       rsi_sum,       rsi_det),
        ("OR Clear",      or_clear_pass,  or_clear_sum,  or_clear_det),
    ]
    for col, (name, passed, summary, detail) in zip(cols, items):
        with col:
            _check_card(name, passed, summary, detail)


def _render_weekly_run(payload: dict, spot: float | str, run_time: str):
    """Checklist card layout for weekly scanner archives."""
    macro    = payload.get("macro") or {}
    oi       = payload.get("oi_structure") or {}
    daily    = payload.get("daily") or {}
    score    = payload.get("checklist_score")
    e_date   = payload.get("earnings_date")
    e_days   = payload.get("earnings_days")
    thesis   = payload.get("thesis", "")

    spy      = macro.get("SPY")  or {}
    qqq      = macro.get("QQQ")  or {}
    vix_d    = macro.get("^VIX") or {}

    # ── 1. Macro Trend (SPY + QQQ above EMA20) ───────────────────────────────
    spy_ok  = spy.get("above_ema20"); qqq_ok = qqq.get("above_ema20")
    macro_pass = (spy_ok and qqq_ok) if (spy_ok is not None and qqq_ok is not None) else None
    macro_sum  = (
        f"SPY {'✓' if spy_ok else '✗'} EMA20  QQQ {'✓' if qqq_ok else '✗'} EMA20"
        if spy_ok is not None else "—"
    )
    macro_det  = (
        f"**SPY:** ${spy.get('spot','—')}  EMA20 ${spy.get('ema20','—')}  "
        f"5d {spy.get('ret5d','—'):+.1f}%<br>" if spy.get('spot') else ""
    ) + (
        f"**QQQ:** ${qqq.get('spot','—')}  EMA20 ${qqq.get('ema20','—')}  "
        f"5d {qqq.get('ret5d','—'):+.1f}%<br>" if qqq.get('spot') else ""
    )

    # ── 2. VIX < 20 ──────────────────────────────────────────────────────────
    vix_spot = vix_d.get("spot")
    vix_pass = (vix_spot < 20) if vix_spot is not None else None
    vix_sum  = f"VIX={vix_spot:.2f}" if vix_spot is not None else "—"
    vix_det  = (
        f"**VIX:** {vix_spot:.2f}  (< 20 = cheap options, good for buying debit spreads)<br>"
        f"5d return: {vix_d.get('ret5d','—')}"
    ) if vix_spot else "VIX not in this archive."

    # ── 3. Daily Trend (AAPL above EMA14) ────────────────────────────────────
    d_spot  = daily.get("spot"); ema14 = daily.get("ema14")
    trend_pass = (d_spot > ema14) if (d_spot and ema14) else None
    trend_sum  = (
        f"${d_spot:.2f} vs EMA14 ${ema14:.2f}  ({'above ✓' if trend_pass else 'below ✗'})"
        if d_spot else "—"
    )
    trend_det  = (
        f"**Spot:** ${d_spot}  **EMA14:** ${ema14}  **EMA28:** ${daily.get('ema28','—')}"
        f"  **EMA50:** ${daily.get('ema50','—')}<br>"
        f"**RSI(14):** {daily.get('rsi','—')}  **MACD hist:** {daily.get('macd_hist','—')}<br>"
        f"**ATR(14):** ${daily.get('atr','—')}  "
        f"Support ${daily.get('support','—')}  Resist ${daily.get('resist','—')}"
    ) if d_spot else "Daily data not in this archive."

    # ── 4. P/C OI Skew (< 0.80) ──────────────────────────────────────────────
    pc_oi   = oi.get("pc_oi")
    pc_pass = (pc_oi < 0.80) if pc_oi is not None else None
    pc_sum  = f"Near-term P/C OI={pc_oi:.2f} (want < 0.80 for calls)" if pc_oi is not None else "—"
    pc_det  = (
        f"**P/C OI ratio:** {pc_oi}<br>"
        f"**Total call OI:** {int(oi.get('total_call_oi',0)):,}  "
        f"**Total put OI:** {int(oi.get('total_put_oi',0)):,}<br>"
        f"**Max pain:** ${oi.get('max_pain','—')}  "
        f"**IV skew:** {oi.get('iv_skew','—')}%"
    ) if pc_oi is not None else "OI structure not in this archive."

    # ── 5. Earnings > 7 days ──────────────────────────────────────────────────
    earn_pass = (e_days > 7) if e_days is not None else None
    earn_sum  = (
        f"Next earnings hint {e_date} ({e_days}d away)"
        if e_days is not None else "No earnings date in this archive"
    )
    earn_det  = (
        f"**Earnings date:** {e_date}<br>**Days away:** {e_days}<br>"
        f"{'✅ Safe window' if earn_pass else '⚠️ Too close — avoid holding through earnings'}"
    ) if e_days is not None else earn_sum

    # ── Banner ───────────────────────────────────────────────────────────────
    checks  = [macro_pass, vix_pass, trend_pass, pc_pass, earn_pass]
    n_pass  = sum(1 for c in checks if c is True)
    display_score = score if score is not None else n_pass

    if display_score >= 4:
        banner_bg, banner_icon, verdict_txt = "#1a3a1a", "✅", "TRADE APPROVED"
    elif display_score >= 3:
        banner_bg, banner_icon, verdict_txt = "#2a2a1a", "⚠️", "MARGINAL"
    else:
        banner_bg, banner_icon, verdict_txt = "#3a1a1a", "🔴", "NO-GO"

    st.markdown(
        f'<div style="background:{banner_bg};padding:1rem 1.5rem;border-radius:8px;margin-bottom:1rem">'
        f'<span style="font-size:1.4rem;font-weight:900;color:#fff">'
        f'{banner_icon} {verdict_txt} &nbsp;·&nbsp; {display_score}/5 &nbsp;·&nbsp; '
        f'{"BULLISH" if trend_pass else ("BEARISH" if trend_pass is False else "—")}'
        f'</span><br>'
        f'<span style="color:#aaa;font-size:0.85rem">Weekly scanner · {run_time} ET · Spot ${spot}</span>'
        f'</div>',
        unsafe_allow_html=True,
    )
    st.caption("Execution Gate — 5-Point Checklist")

    cols = st.columns(5)
    items = [
        ("Macro Clear",   macro_pass,  macro_sum,  macro_det),
        ("VIX < 20",      vix_pass,    vix_sum,    vix_det),
        ("Daily Trend",   trend_pass,  trend_sum,  trend_det),
        ("P/C Skew",      pc_pass,     pc_sum,     pc_det),
        ("Earnings > 7d", earn_pass,   earn_sum,   earn_det),
    ]
    for col, (name, passed, summary, detail) in zip(cols, items):
        with col:
            _check_card(name, passed, summary, detail)

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
        pc  = pv / cv if cv > 0 else 0

        if pc < 0.7:   bias = "▲ BULLISH"
        elif pc < 0.9: bias = "▲ MILD BULLISH"
        elif pc < 1.1: bias = "- NEUTRAL"
        elif pc < 1.5: bias = "▼ MILD BEARISH"
        else:          bias = "▼ BEARISH"

        notable = abs(pc - overall_pc) > 0.25 if overall_pc > 0 else False
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
            "P/C":      f"{pc:.2f}",
            "BIAS":     bias,
            "NOTABLE":  "◄ notable" if notable else "",
            "CALL Δ":   _ds(cv_d),
            "PUT Δ":    _ds(pv_d),
            # numeric shadows for styling
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


@st.cache_data(ttl=120)
def _scan_archive_metadata() -> list[dict]:
    """
    Read minimal metadata from every archive file. Cached for 2 minutes.
    Only parses top-level JSON fields — no heavy rendering.
    """
    rows: list[dict] = []
    for pattern, atype in [
        ("archive/AAPL_*.json",        "Daily"),
        ("archive_weekly/AAPL_*.json", "Weekly"),
    ]:
        for fpath in sorted(glob.glob(pattern), reverse=True):
            fname = os.path.basename(fpath)
            try:
                parts    = fname.replace("AAPL_", "").replace(".json", "").split("_")
                run_date = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:]}"
                run_time = f"{parts[1][:2]}:{parts[1][2:4]}"
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


def _render_tab2():
    st.header("Scanner Archive")

    meta = _scan_archive_metadata()
    if not meta:
        st.info("No archive files found in archive/ or archive_weekly/")
        return

    # ── Summary table (metadata only — fast) ──────────────────────────────────
    table_rows = [{
        "Type":      r["type"],
        "Date":      r["date"],
        "Time":      r["time"],
        "Spot":      f"${r['spot']:.2f}" if r["spot"] is not None else "—",
        "Direction": r["direction"],
        "P/C":       f"{r['pc_ratio']:.2f}" if r["pc_ratio"] is not None else "—",
        "Score":     f"{r['score']}/5" if r["score"] is not None else "—",
    } for r in meta]

    st.dataframe(
        pd.DataFrame(table_rows),
        use_container_width=True,
        hide_index=True,
        height=260,
    )

    st.markdown("---")

    # ── Run selector — only ONE run rendered at a time ────────────────────────
    options = [
        f"{'📆' if r['type']=='Weekly' else '📊'}  {r['date']}  {r['time']}  |  "
        f"{r['direction']}  ·  Spot ${r['spot']:.2f}" if r["spot"] else
        f"{'📆' if r['type']=='Weekly' else '📊'}  {r['date']}  {r['time']}"
        for r in meta
    ]
    sel_idx = st.selectbox(
        "Select run to view details:",
        range(len(options)),
        format_func=lambda i: options[i],
    )

    if sel_idx is None:
        return

    sel = meta[sel_idx]
    try:
        with open(sel["fpath"]) as f:
            payload = json.load(f)
    except Exception as e:
        st.error(f"Could not read {sel['fpath']}: {e}")
        return

    # Load the previous run of the same type for delta computation
    prev_payload = None
    for i in range(sel_idx + 1, len(meta)):
        if meta[i]["type"] == sel["type"]:
            try:
                with open(meta[i]["fpath"]) as f:
                    prev_payload = json.load(f)
            except Exception:
                pass
            break

    spot      = payload.get("spot", "—")
    is_weekly = "checklist_score" in payload
    src_tag   = "📆 Weekly" if is_weekly else "📊 Daily"

    st.markdown(f"**{src_tag} · {sel['date']} {sel['time']} ET · Spot ${spot}**")
    st.divider()

    if is_weekly:
        _render_weekly_run(payload, spot, sel["time"])
    else:
        # ── Volume by Expiry table ─────────────────────────────────────────
        vol_curr = payload.get("volume") or {}
        vol_prev = (prev_payload.get("volume") or {}) if prev_payload else None
        pc_ratio = float(vol_curr.get("pc_ratio") or 0)

        st.markdown("#### Volume by Expiry")
        if prev_payload:
            prev_ts = prev_payload.get("timestamp", "")
            try:
                prev_et = datetime.fromisoformat(prev_ts).astimezone(ET).strftime("%H:%M ET")
            except Exception:
                prev_et = "previous run"
            st.caption(f"CALL Δ / PUT Δ vs {prev_et}  ·  green = higher  ·  red = lower")
        _render_expiry_vol_table(vol_curr, vol_prev, pc_ratio)

        st.divider()
        # ── 5-point checklist ──────────────────────────────────────────────
        _render_daily_run(payload, spot, sel["time"])


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


# ══════════════════════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════════════════════

def main():
    _market_banner()
    cfg = _sidebar()

    tab1, tab2, tab3 = st.tabs([
        "📈 Flow table",
        "📋 Scanner runs",
        "🔬 Spread gate",
    ])

    with tab1:
        _render_tab1(cfg)

    with tab2:
        _render_tab2()

    with tab3:
        _render_tab3(cfg)


if __name__ == "__main__" or True:
    main()
