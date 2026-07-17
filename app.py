"""
app.py — AAPL Options Scanner Dashboard
Display-only layer. All analytical numbers come from dailyScaner.py
functions or archive JSON files. No indicators recomputed here.
"""

import glob
import json
import os
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

def _sidebar() -> dict:
    with st.sidebar:
        st.title("📊 AAPL Scanner")
        st.caption("Display layer — no analysis computed here")
        st.divider()

        run = st.button("🔄 Reload archive", use_container_width=True, type="primary")

        st.subheader("Flow filters")
        min_dte = st.number_input("Min DTE", min_value=0, value=1, step=1)
        sort_by = st.selectbox(
            "Sort by",
            ["Volume", "Premium $", "Strike"],
        )

        st.divider()
        latest = _latest_archive()
        if latest:
            ts = datetime.fromisoformat(latest["timestamp"]).astimezone(ET)
            st.caption(f"Last archive: {ts.strftime('%Y-%m-%d %H:%M ET')}")
            st.caption(f"Spot at run: ${latest.get('spot', '—')}")

    return {
        "run": run,
        "min_dte": min_dte,
        "sort_by": sort_by,
        "latest_archive": latest,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Flow table (reads from archive — no live data fetched)
# ══════════════════════════════════════════════════════════════════════════════

def _load_archive_chain() -> tuple[pd.DataFrame, dict | None]:
    """
    Build a flat per-contract DataFrame from the most recent daily archive JSON.
    Uses top_calls / top_puts stored by the scanner; no live data fetched.
    Returns (df, raw_payload).
    """
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
                "side":         side,
                "strike":       float(c.get("strike", 0)),
                "expiry":       c.get("expiry", ""),
                "dte":          int(c.get("dte", 0)),
                "last":         last,
                "volume":       vol_int,
                "openInterest": oi_int,
                "premium":      last * vol_int * 100,
            })

    if not rows:
        return pd.DataFrame(), payload
    return pd.DataFrame(rows), payload


def _render_tab1(cfg: dict):
    st.header("Options Flow — Top Contracts")

    df, payload = _load_archive_chain()

    if df.empty or payload is None:
        st.info("No archive data found. Run the scanner first (`python dailyScaner.py`).")
        return

    vol_block = payload.get("volume", {})
    spot   = payload.get("spot", "—")
    ts_str = payload.get("timestamp", "")
    if ts_str:
        ts_et = datetime.fromisoformat(ts_str).astimezone(ET)
        st.caption(f"Data from archive run: **{ts_et.strftime('%Y-%m-%d %H:%M ET')}**")

    # ── Summary pills (straight from scanner-computed values) ─────────────────
    total_call_vol = int(vol_block.get("total_call_vol") or 0)
    total_put_vol  = int(vol_block.get("total_put_vol")  or 0)
    pc_ratio       = float(vol_block.get("pc_ratio")     or 0)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Spot (last run)",     f"${spot}")
    c2.metric("Total call volume",   f"{total_call_vol:,}")
    c3.metric("Total put volume",    f"{total_put_vol:,}")
    c4.metric("P/C vol ratio",       f"{pc_ratio:.2f}")
    c5.metric("Top contracts shown", len(df))

    # ── Filters ───────────────────────────────────────────────────────────────
    view = df[df["dte"] >= cfg["min_dte"]].copy()

    sort_col = {"Volume": "volume", "Premium $": "premium", "Strike": "strike"}.get(
        cfg["sort_by"], "volume"
    )
    if sort_col in view.columns:
        view = view.sort_values(sort_col, ascending=False, na_position="last")

    # ── Display table ─────────────────────────────────────────────────────────
    def _side_badge(row):
        return "🟢 CALL" if row["side"] == "call" else "🔴 PUT"

    def _expiry_label(row):
        label = row["expiry"]
        if row["dte"] == 0:
            label += " (0DTE)"
        return label

    display = pd.DataFrame({
        "Side":         view.apply(_side_badge, axis=1),
        "Strike":       view["strike"].map(lambda x: f"${x:.1f}"),
        "Expiry":       view.apply(_expiry_label, axis=1),
        "DTE":          view["dte"],
        "Last":         view["last"].map(lambda x: f"${x:.2f}"),
        "Volume":       view["volume"].map(lambda x: f"{int(x):,}"),
        "Vol/OI":       view.apply(
            lambda r: f"{r['volume']/r['openInterest']:.1f}x"
            if r["openInterest"] > 0 else "—", axis=1
        ),
        "OI":           view["openInterest"].map(lambda x: f"{int(x):,}"),
        "Est. Premium": view["premium"].map(_fmt_dollars),
    })

    st.dataframe(display, use_container_width=True, height=500)
    st.caption(
        f"{len(view)} contracts shown "
        f"(top 10 calls + top 10 puts by volume from last scanner run)"
    )


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


def _render_tab2():
    st.header("Scanner Archive")

    # Gather both daily and weekly archives
    all_files: list[tuple[str, str, str]] = []  # (run_date, run_time, fpath)

    for pattern in ["archive/AAPL_*.json", "archive_weekly/AAPL_*.json"]:
        for fpath in glob.glob(pattern):
            fname = os.path.basename(fpath)
            try:
                parts    = fname.replace("AAPL_", "").replace(".json", "").split("_")
                run_date = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:]}"
                run_time = f"{parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:]}"
            except Exception:
                run_date, run_time = "unknown", "?"
            all_files.append((run_date, run_time, fpath))

    if not all_files:
        st.info("No archive files found in archive/ or archive_weekly/")
        return

    # Group by date, newest first
    by_date: dict[str, list] = {}
    for run_date, run_time, fpath in all_files:
        by_date.setdefault(run_date, []).append((run_time, fpath))

    sorted_days = sorted(by_date.keys(), reverse=True)

    for day in sorted_days:
        runs = sorted(by_date[day], reverse=True)
        label = (
            f"📅 {day}  —  {len(runs)} run{'s' if len(runs)>1 else ''}"
        )
        with st.expander(label, expanded=(day == sorted_days[0])):
            for run_time, fpath in runs:
                try:
                    with open(fpath) as f:
                        payload = json.load(f)
                except Exception:
                    st.error(f"Could not read {fpath}")
                    continue

                is_weekly = "checklist_score" in payload
                spot      = payload.get("spot", "—")
                src_tag   = "📆 Weekly" if is_weekly else "📊 Daily"
                st.markdown(f"**{src_tag} · {run_time} ET**", unsafe_allow_html=True)

                if is_weekly:
                    _render_weekly_run(payload, spot, run_time)
                else:
                    _render_daily_run(payload, spot, run_time)

                st.divider()


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
