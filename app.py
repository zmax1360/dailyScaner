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
from datetime import datetime, date, timedelta
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


def _discover_tickers() -> list[str]:
    """
    Return sorted list of tickers that have at least one daily archive file.
    Derived purely from filenames — no JSON parsing.
    Example: archive/AAPL_20260717_0930.json → "AAPL"
    """
    seen: set[str] = set()
    for fpath in glob.glob("archive/*.json"):
        name = os.path.basename(fpath)
        parts = name.split("_")
        if len(parts) >= 3:          # TICKER_YYYYMMDD_HHMM.json
            seen.add(parts[0].upper())
    return sorted(seen) or ["AAPL"]  # always at least AAPL


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


@st.fragment(run_every=timedelta(minutes=5))
def _auto_scan_watcher():
    """
    Fires every 5 minutes via Streamlit's server-side timer.
    During market hours: launches one background daemon thread per known ticker
    (from archive filenames). Each ticker has an independent cooldown so they
    don't block each other. Non-blocking — event loop never stalls.
    """
    if _market_is_closed():
        st.caption("⏸ Auto-scan paused — market closed")
        return

    tickers = _discover_tickers()
    now     = time.time()
    launched, scanning, done = [], [], []

    for ticker in tickers:
        s = _bg_state(ticker)
        if s["running"]:
            elapsed = int(now - s["t0"])
            scanning.append(f"⏳ {ticker} ({elapsed}s)…")
        elif s["t0"] > 0 and (now - s["t0"]) < 270:   # 4.5-min cooldown
            icon = "✅" if s["last_ok"] else "⚠"
            done.append(f"{icon} {ticker}: {s.get('last_ts','—')}")
        else:
            s["running"] = True
            s["t0"]      = now
            threading.Thread(target=_bg_scan_worker, args=(ticker,), daemon=True).start()
            launched.append(ticker)

    lines = (
        ([f"🔄 Launched: {', '.join(launched)}"] if launched else [])
        + scanning + done
    )
    for line in lines:
        st.caption(line)


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
            else:
                st.error(f"{focus_ticker} scanner returned an error.")
            with st.expander("Scanner output", expanded=not ok):
                st.code(output[-4000:], language="text")

        # ── Auto-scan status / watcher ────────────────────────────────────
        _auto_scan_watcher()

        st.divider()
        st.subheader("Flow filters")
        min_dte  = st.number_input("Min DTE", min_value=0, value=1, step=1)
        top_n    = st.number_input("Top N contracts/side", min_value=1, max_value=30, value=5, step=1,
                                   help="How many top calls / puts to show in The Magnets panel")
        sort_by  = st.selectbox("Sort by", ["Volume", "Premium $", "Strike"])

        st.divider()
        latest = _latest_archive(focus_ticker)
        if latest:
            ts = datetime.fromisoformat(latest["timestamp"]).astimezone(ET)
            st.caption(f"Last archive: {ts.strftime('%Y-%m-%d %H:%M ET')}")
            st.caption(f"Spot at run: ${latest.get('spot', '—')}")

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

    # ── Quote strip (session block — absent in older archives) ────────────────
    session     = curr.get("session") or {}
    prev_close  = session.get("prev_close")
    open_today  = session.get("open")
    day_high    = session.get("day_high")
    day_low     = session.get("day_low")
    spot_label  = "Close" if _market_is_closed() else "Spot"

    if ts_str:
        ts_et = datetime.fromisoformat(ts_str).astimezone(ET)
        st.caption(f"Last run: **{ts_et.strftime('%Y-%m-%d %H:%M ET')}**")

    if prev_close is not None:
        chg     = spot - prev_close
        chg_pct = chg / prev_close * 100 if prev_close else 0
        chg_color = "#00c853" if chg >= 0 else "#d50000"
        chg_sign  = "+" if chg >= 0 else ""
        parts = [
            f'<span style="font-size:1.1rem;font-weight:700;color:#eee">'
            f'{ticker} &nbsp; {spot_label} <b>${spot:.2f}</b></span>',
            f'<span style="color:{chg_color};font-weight:bold">'
            f'{chg_sign}${chg:.2f} ({chg_sign}{chg_pct:.2f}%)</span>',
        ]
        if open_today is not None:
            parts.append(f'<span style="color:#aaa">Open ${open_today:.2f}</span>')
        parts.append(f'<span style="color:#aaa">Prev close ${prev_close:.2f}</span>')
        if day_high is not None and day_low is not None:
            parts.append(f'<span style="color:#888">H ${day_high:.2f} &nbsp; L ${day_low:.2f}</span>')
        st.markdown(
            '<div style="background:#0d1117;padding:0.6rem 1.2rem;border-radius:6px;'
            'margin-bottom:0.5rem;display:flex;gap:1.5rem;align-items:center;flex-wrap:wrap">'
            + "&ensp;·&ensp;".join(parts)
            + "</div>",
            unsafe_allow_html=True,
        )
    else:
        # Older archive without session block — show spot only
        st.markdown(
            f'<div style="background:#0d1117;padding:0.6rem 1.2rem;border-radius:6px;margin-bottom:0.5rem">'
            f'<span style="font-size:1.1rem;font-weight:700;color:#eee">'
            f'{ticker} &nbsp; {spot_label} <b>${spot:.2f}</b></span></div>',
            unsafe_allow_html=True,
        )

    # ── Direction / P/C banner ────────────────────────────────────────────────
    dir_color    = "#00c853" if "BULL" in direction else "#d50000" if "BEAR" in direction else "#9e9e9e"
    dir_icon     = "▲" if "BULL" in direction else "▼" if "BEAR" in direction else "─"
    pc_bias      = "BULLISH SKEW" if pc_ratio < 0.7 else ("BEARISH SKEW" if pc_ratio > 1.0 else "NEUTRAL")
    hist_suffix  = " (historical)" if _market_is_closed() else ""

    st.markdown(
        f'<div style="background:#1a1a2e;padding:0.75rem 1.5rem;border-radius:8px;margin-bottom:0.5rem">'
        f'<span style="font-size:1.5rem;font-weight:900;color:{dir_color}">'
        f'{dir_icon} {direction}{hist_suffix}</span>'
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

    # ── Multi-Timeframe Detail (above magnets so context comes first) ─────────
    prev_tfs = (prev.get("timeframes") or {}) if prev else {}
    with st.expander("📊 Multi-Timeframe Detail", expanded=True):
        tf_rows = []
        for tf in ["5M", "10M", "15M", "1H", "4H", "1D"]:
            d    = tfs.get(tf) or {}
            pd_  = prev_tfs.get(tf) or {}
            rsi  = d.get("rsi")
            hist = d.get("hist")
            vs   = d.get("vs")
            p_rsi  = pd_.get("rsi")
            p_hist = pd_.get("hist")

            d_rsi  = (rsi  - p_rsi)  if (rsi  is not None and p_rsi  is not None) else None
            d_hist = (hist - p_hist) if (hist is not None and p_hist is not None) else None

            tf_rows.append({
                "TF":        tf,
                "RSI":       _rsi_plain(rsi),
                "ΔRSI":      f"{d_rsi:+.1f}" if d_rsi is not None else "—",
                "MACD hist": f"{hist:+.4f}"  if hist  is not None else "—",
                "ΔMACD":     f"{d_hist:+.4f}" if d_hist is not None else "—",
                "Vol Spike": f"{vs:.2f}×" if vs is not None else "—",
                "Support":   f"${d.get('support', '—')}",
                "Resist":    f"${d.get('resist', '—')}",
                "_d_rsi":    d_rsi,
                "_d_hist":   d_hist,
            })

        df_tf = pd.DataFrame(tf_rows)

        def _delta_style(val: str) -> str:
            s = str(val)
            if s.startswith("+"): return "color:#00c853;font-weight:bold"
            if s.startswith("-"): return "color:#d50000;font-weight:bold"
            return "color:#666"

        dcols = ["TF","RSI","ΔRSI","MACD hist","ΔMACD","Vol Spike","Support","Resist"]
        if not prev_tfs:
            dcols = ["TF","RSI","MACD hist","Vol Spike","Support","Resist"]

        styled_tf = df_tf[dcols].style
        if prev_tfs:
            styled_tf = styled_tf.map(_delta_style, subset=["ΔRSI", "ΔMACD"])

        st.dataframe(styled_tf, use_container_width=True, hide_index=True)

    # ── The Magnets (calls | puts side-by-side) ──────────────────────────────
    top_n = cfg.get("top_n", 5)
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

    st.markdown("---")

    # ── Volume by Expiry — P/C term structure (below magnets) ────────────────
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

        # ── P/C term structure bar chart ──────────────────────────────────
        _render_pc_term_chart(chart_pc)
    else:
        st.caption("No expiry data available")

    # ══ Collapsible detail sections ═══════════════════════════════════════════
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

        with st.expander(f"📈 Changes vs last run  (since {prev_ts_str})", expanded=True):
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

    with st.expander("⏰ Opening Range Breakout", expanded=True):
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
                if "BULL" in str(val): return "color:#00c853;font-weight:bold"
                if "BEAR" in str(val): return "color:#d50000;font-weight:bold"
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


def _render_expiry_drill_down(
    vol_curr: dict, expiry: str, vol_prev: dict | None = None
) -> None:
    """
    Calls + puts filtered to one expiry, shown side-by-side.
    When vol_prev is provided, shows ΔPrice and ΔVol vs the previous run:
    green = increased, red = decreased.
    """
    def _filter(vol, key):
        return [c for c in (vol.get(key) or []) if c.get("expiry") == expiry] if vol else []

    def _signed(n: int | float, fmt_int: bool = True) -> str:
        if n > 0: return f"+{int(n):,}" if fmt_int else f"+{n:.2f}"
        if n < 0: return f"{int(n):,}"  if fmt_int else f"{n:.2f}"
        return "·0"

    def _delta_style(val: str) -> str:
        s = str(val)
        if s.startswith("+"): return "color:#00c853;font-weight:bold"
        if s.startswith("-"): return "color:#d50000;font-weight:bold"
        return "color:#666"

    st.markdown(f"#### {expiry} — Contract Detail")
    cc, pc_ = st.columns(2, gap="small")

    for col, curr_key, prev_key, label in [
        (cc,  "top_calls", "top_calls", "🟢 CALLS"),
        (pc_, "top_puts",  "top_puts",  "🔴 PUTS"),
    ]:
        curr_contracts = _filter(vol_curr, curr_key)
        prev_by_strike = {
            float(c.get("strike", 0)): c
            for c in _filter(vol_prev, prev_key)
        } if vol_prev else {}

        with col:
            st.markdown(f"**{label}**")
            if not curr_contracts:
                st.caption("No contracts for this expiry.")
                continue

            rows = []
            for c in curr_contracts:
                strike = float(c.get("strike") or 0)
                vol    = int(c.get("volume") or 0)
                oi     = int(c.get("openInterest") or 0)
                price  = float(c.get("lastPrice") or 0)
                iv     = float(c.get("impliedVolatility") or 0)
                voi    = vol / max(oi, 1)

                p      = prev_by_strike.get(strike)
                d_price = price - float(p.get("lastPrice") or 0) if p else None
                d_vol   = vol   - int(p.get("volume") or 0)     if p else None

                row = {
                    "Strike": f"${strike:.1f}",
                    "Price":  f"${price:.2f}",
                    "Volume": f"{vol:,}",
                    "OI":     f"{oi:,}",
                    "VOL/OI": f"{voi:.2f}x 🔥" if voi >= 2 else f"{voi:.2f}x",
                    "IV":     f"{iv:.1%}" if iv > 0 else "—",
                }
                if vol_prev:
                    row["ΔPrice"] = _signed(d_price, fmt_int=False) if d_price is not None else "new"
                    row["ΔVol"]   = _signed(d_vol)                  if d_vol   is not None else "new"
                rows.append(row)

            df = pd.DataFrame(rows)
            dcols = ["Strike","Price","Volume","OI","VOL/OI","IV"]
            if vol_prev:
                dcols = ["Strike","Price","ΔPrice","Volume","ΔVol","OI","VOL/OI","IV"]

            styled = df[dcols].style
            if vol_prev:
                styled = styled.map(_delta_style, subset=["ΔPrice", "ΔVol"])
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

    st.caption("Click a row to see the contracts for that expiry.")
    event = st.dataframe(
        styled,
        on_select="rerun",
        selection_mode="single-row",
        use_container_width=True,
        hide_index=True,
    )

    sel = event.selection.rows
    if sel:
        sel_exp = expiry_list[sel[0]]
        st.markdown("---")
        _render_expiry_drill_down(vol_curr, sel_exp, vol_prev)


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


def _render_tab2(cfg: dict):
    ticker = cfg.get("ticker", "AAPL")
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

def _render_tab4() -> None:
    """
    Ticker Manager — run the daily scanner for any symbol and see the status
    of every ticker that already has archive data.
    Adding a ticker here is as simple as running it once; it then appears
    in the sidebar focus-ticker selector automatically.
    """
    st.markdown("### Run scanner for a new ticker")
    st.caption(
        "Enter any valid symbol (e.g. TSLA, NVDA, SPY). "
        "The scanner saves results to `archive/{TICKER}_*.json` — "
        "the ticker then appears in the sidebar selector automatically."
    )

    col_input, col_btn = st.columns([3, 1], gap="small")
    with col_input:
        new_ticker = st.text_input(
            "Ticker symbol",
            placeholder="e.g. TSLA",
            label_visibility="collapsed",
        ).strip().upper()
    with col_btn:
        run_new = st.button("🚀 Run scan", type="primary", use_container_width=True)

    if run_new:
        if not new_ticker or not new_ticker.isalpha():
            st.error("Enter a valid ticker symbol (letters only).")
        else:
            with st.spinner(f"Scanning {new_ticker}… (may take several minutes)"):
                ok, output = _run_daily_scanner(new_ticker)
            if ok:
                st.success(f"✅ {new_ticker} scan complete — it now appears in the ticker selector.")
                _scan_archive_metadata.clear()
            else:
                st.error(f"❌ Scan failed for {new_ticker}.")
            with st.expander("Scanner output", expanded=not ok):
                st.code(output[-4000:], language="text")

    st.markdown("---")
    st.markdown("### Known tickers")
    st.caption("All tickers that have at least one daily archive file. Click **Run** to refresh any of them.")

    known = _discover_tickers()
    if not known:
        st.info("No archive files found yet. Run a scan above to get started.")
        return

    # Build summary table
    rows = []
    for t in known:
        files = sorted(glob.glob(f"archive/{t}_*.json"), reverse=True)
        last_ts, last_spot, last_dir = "—", "—", "—"
        if files:
            try:
                with open(files[0]) as f:
                    p = json.load(f)
                ts_raw  = p.get("timestamp", "")
                last_ts = datetime.fromisoformat(ts_raw).astimezone(ET).strftime("%Y-%m-%d %H:%M ET") if ts_raw else "—"
                last_spot = f"${float(p.get('spot') or 0):.2f}"
                last_dir  = p.get("direction", "—")
            except Exception:
                pass
        s = _bg_state(t)
        if s["running"]:
            status = f"⏳ scanning ({int(time.time()-s['t0'])}s)…"
        elif s.get("last_ts"):
            status = f"{'✅' if s['last_ok'] else '⚠'} last auto: {s['last_ts']}"
        else:
            status = "—"
        rows.append({
            "Ticker":     t,
            "Last scan":  last_ts,
            "Spot":       last_spot,
            "Direction":  last_dir,
            "Auto-scan":  status,
            "# files":    len(files),
        })

    def _dir_style(val: str) -> str:
        if "BULL" in str(val): return "color:#00c853;font-weight:bold"
        if "BEAR" in str(val): return "color:#d50000;font-weight:bold"
        return "color:#9e9e9e"

    df = pd.DataFrame(rows)
    st.dataframe(
        df.style.map(_dir_style, subset=["Direction"]),
        use_container_width=True,
        hide_index=True,
    )

    st.markdown("**Run scan for a specific ticker:**")
    sel_ticker = st.selectbox("Select ticker to rescan", known, key="tab4_rescan_select")
    if st.button(f"🔄 Rescan {sel_ticker}", key="tab4_rescan_btn"):
        with st.spinner(f"Rescanning {sel_ticker}…"):
            ok, output = _run_daily_scanner(sel_ticker)
        if ok:
            st.success(f"✅ {sel_ticker} rescanned.")
            _scan_archive_metadata.clear()
        else:
            st.error(f"❌ Rescan failed for {sel_ticker}.")
        with st.expander("Output", expanded=not ok):
            st.code(output[-4000:], language="text")


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
    cfg = _sidebar()

    tab1, tab2, tab3, tab4 = st.tabs([
        "📈 Options Flow",
        "📋 Scanner Archive",
        "🔬 Spread Gate",
        "🗂 Tickers",
    ])

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


if __name__ == "__main__" or True:
    main()
