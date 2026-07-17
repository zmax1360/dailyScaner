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

        run = st.button("🔄 Run scan", use_container_width=True, type="primary")

        st.subheader("Flow filters")
        min_dte = st.number_input("Min DTE", min_value=0, value=1, step=1)
        min_dvol = st.number_input("Min Δvolume", min_value=0, value=0, step=100)
        sort_by = st.selectbox(
            "Sort by",
            ["Premium $", "Δ-premium", "Volume", "Strike"],
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
        "min_dvol": min_dvol,
        "sort_by": sort_by,
        "latest_archive": latest,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Flow table
# ══════════════════════════════════════════════════════════════════════════════

_SORT_MAP = {
    "Premium $":  "premium",
    "Δ-premium":  "delta_premium",
    "Volume":     "volume",
    "Strike":     "strike",
}


def _run_scan_and_store():
    """Fetch chain, diff vs snapshot, save new snapshot. Returns enriched df."""
    with st.spinner("Fetching full options chain…"):
        current = data_adapter.fetch_full_chain()

    if current.empty:
        st.error("Chain fetch returned no data. Check network / market hours.")
        return None

    current["premium"] = current["volume"].astype(float) * current["mid"].astype(float) * 100

    prev_df, prev_ts = ss.load_snapshot()
    if prev_df is not None and prev_ts is not None:
        enriched = ss.compute_deltas(current, prev_df, prev_ts)
    else:
        enriched = current.copy()
        enriched["delta_volume"]  = None
        enriched["delta_premium"] = None
        enriched["is_new"]        = False
        enriched["is_stale_day"]  = False
        enriched["is_block"]      = False

    ss.save_snapshot(current)
    st.session_state["flow_df"]  = enriched
    st.session_state["scan_ts"]  = datetime.now(ET).isoformat()
    return enriched


def _render_tab1(cfg: dict):
    st.header("Options Flow")

    # Run scan
    if cfg["run"]:
        _run_scan_and_store()

    df: pd.DataFrame | None = st.session_state.get("flow_df")
    if df is None:
        st.info("Press **Run scan** to load the options chain.")
        return

    # ── Summary pills ────────────────────────────────────────────────────────
    latest = cfg["latest_archive"]
    spot   = latest.get("spot", "—") if latest else "—"

    calls_df = df[df["side"] == "call"]
    puts_df  = df[df["side"] == "put"]
    total_call_prem = calls_df["premium"].sum()
    total_put_prem  = puts_df["premium"].sum()
    pc_vol = (
        puts_df["volume"].sum() / calls_df["volume"].sum()
        if calls_df["volume"].sum() > 0 else 0
    )
    block_count = int(df["is_block"].sum()) if "is_block" in df.columns else 0

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Spot (last run)", f"${spot}")
    c2.metric("Call premium", _fmt_dollars(total_call_prem))
    c3.metric("Put premium",  _fmt_dollars(total_put_prem))
    c4.metric("P/C vol ratio", f"{pc_vol:.2f}")
    c5.metric("🔥 Blocks", block_count)

    # ── Stale warning ────────────────────────────────────────────────────────
    stale = df.get("is_stale_day", pd.Series([False])).iloc[0] if len(df) else False
    if stale:
        st.info(
            "ℹ️  Δ columns are greyed out — previous snapshot is from a prior day. "
            "Refresh again to start tracking intraday deltas.",
            icon="📅",
        )

    # ── Apply filters ────────────────────────────────────────────────────────
    view = df[df["dte"] >= cfg["min_dte"]].copy()
    view = view[view["volume"] > 0]
    if cfg["min_dvol"] > 0 and not stale:
        view = view[view["delta_volume"].fillna(0) >= cfg["min_dvol"]]

    sort_col = _SORT_MAP.get(cfg["sort_by"], "premium")
    if sort_col in view.columns:
        view = view.sort_values(sort_col, ascending=False, na_position="last")

    # ── Build display table ──────────────────────────────────────────────────
    def _side_badge(row):
        base = "🟢 CALL" if row["side"] == "call" else "🔴 PUT"
        if row.get("is_new"):
            base += " 🆕"
        return base

    def _expiry_label(row):
        label = row["expiry"]
        if row["dte"] == 0:
            label += " (0DTE)"
        return label

    def _dvol(row):
        if stale or row.get("delta_volume") is None:
            return "—"
        return f"{int(row['delta_volume']):+,}"

    def _dprem(row):
        if stale or row.get("delta_premium") is None:
            return "—"
        prefix = "🔥 " if row.get("is_block") else ""
        return prefix + _fmt_dollars(row["delta_premium"])

    display = pd.DataFrame({
        "Side":       view.apply(_side_badge, axis=1),
        "Strike":     view["strike"].map(lambda x: f"${x:.1f}"),
        "Expiry":     view.apply(_expiry_label, axis=1),
        "DTE":        view["dte"],
        "Mid":        view["mid"].map(lambda x: f"${x:.2f}"),
        "Volume":     view["volume"].map(lambda x: f"{int(x):,}"),
        "Δ Volume":   view.apply(_dvol, axis=1),
        "Vol/OI":     view.apply(
            lambda r: f"{r['volume']/r['openInterest']:.1f}x"
            if r["openInterest"] > 0 else "—", axis=1
        ),
        "OI":         view["openInterest"].map(lambda x: f"{int(x):,}"),
        "Premium $":  view["premium"].map(_fmt_dollars),
        "Δ Premium":  view.apply(_dprem, axis=1),
    })

    # Highlight block rows
    def _highlight(row):
        idx = row.name
        if idx < len(view):
            orig = view.iloc[idx]
            if orig.get("is_block"):
                return ["background-color: #3d1a00"] * len(row)
            if orig.get("is_new"):
                return ["background-color: #2a2a00"] * len(row)
        return [""] * len(row)

    st.dataframe(
        display.style.apply(_highlight, axis=1),
        use_container_width=True,
        height=600,
    )

    scan_ts = st.session_state.get("scan_ts")
    if scan_ts:
        ts_et = datetime.fromisoformat(scan_ts).strftime("%H:%M:%S ET")
        st.caption(f"Last refresh: {ts_et}  ·  {len(view):,} contracts shown")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Scanner runs (archive)
# ══════════════════════════════════════════════════════════════════════════════

_RSI_COLOR = {
    "oversold":  "#00c853",
    "bullish":   "#69f0ae",
    "neutral":   "#9e9e9e",
    "bearish":   "#ff5252",
    "overbought":"#d50000",
}


def _rsi_badge(v) -> str:
    if v is None or v == "—":
        return "—"
    try:
        v = float(v)
    except Exception:
        return "—"
    if v <= 35:
        color, label = _RSI_COLOR["oversold"],  f"OVERSOLD ({v:.0f})"
    elif v >= 65:
        color, label = _RSI_COLOR["overbought"], f"OVERBOUGHT ({v:.0f})"
    elif v < 45:
        color, label = _RSI_COLOR["bearish"],    f"BEARISH ({v:.0f})"
    elif v > 55:
        color, label = _RSI_COLOR["bullish"],    f"BULLISH ({v:.0f})"
    else:
        color, label = _RSI_COLOR["neutral"],    f"NEUTRAL ({v:.0f})"
    return f'<span style="color:{color};font-weight:bold">{label}</span>'


def _direction_badge(d: str | None) -> str:
    if not d:
        return "—"
    color = {"BULLISH": "#00c853", "BEARISH": "#ff5252"}.get(d, "#9e9e9e")
    return f'<span style="color:{color};font-weight:bold">{d}</span>'


def _or_summary(or_data: dict | None, tf: str) -> str:
    if not or_data:
        return "—"
    d = or_data.get(tf)
    if not d:
        return "—"
    return (
        f"H {d.get('high','—')} / L {d.get('low','—')}  "
        f"→ {d.get('bias','—')}"
    )


def _render_tab2():
    st.header("Scanner Archive")

    files = sorted(glob.glob("archive/AAPL_*.json"), reverse=True)
    if not files:
        st.info("No archive files found in archive/")
        return

    # Parse and group by date
    by_date: dict[str, list] = {}
    for fpath in files:
        fname = os.path.basename(fpath)
        try:
            # archive/AAPL_YYYYMMDD_HHMMSS.json
            parts = fname.replace("AAPL_", "").replace(".json", "").split("_")
            run_date = f"{parts[0][:4]}-{parts[0][4:6]}-{parts[0][6:]}"
            run_time = f"{parts[1][:2]}:{parts[1][2:4]}:{parts[1][4:]}"
        except Exception:
            run_date, run_time = "unknown", "?"

        by_date.setdefault(run_date, []).append((run_time, fpath))

    for day in sorted(by_date.keys(), reverse=True):
        runs = sorted(by_date[day], reverse=True)
        with st.expander(f"📅 {day}  —  {len(runs)} run{'s' if len(runs)>1 else ''}",
                         expanded=(day == sorted(by_date.keys(), reverse=True)[0])):
            for run_time, fpath in runs:
                try:
                    with open(fpath) as f:
                        payload = json.load(f)
                except Exception:
                    st.error(f"Could not read {fpath}")
                    continue

                spot      = payload.get("spot", "—")
                direction = payload.get("direction")
                or_data   = payload.get("or_data")
                tfs       = payload.get("timeframes", {})
                vol       = payload.get("volume", {})
                magnets   = payload.get("signal_magnets", {})

                st.markdown(f"**{run_time} ET** — Spot **${spot}**")

                # Direction + OR
                col1, col2 = st.columns(2)
                with col1:
                    st.markdown(
                        f"Direction: {_direction_badge(direction)}  "
                        f"&nbsp;&nbsp; P/C ratio: **{vol.get('pc_ratio', '—')}**",
                        unsafe_allow_html=True,
                    )
                    st.markdown(
                        f"OR 5M: `{_or_summary(or_data, '5M')}`  "
                        f"  OR 15M: `{_or_summary(or_data, '15M')}`",
                        unsafe_allow_html=True,
                    )
                with col2:
                    # Qualified magnets
                    for side in ("call", "put"):
                        m = magnets.get(side)
                        if m:
                            st.markdown(
                                f"{'📈' if side=='call' else '📉'} **{side.upper()} magnet** "
                                f"${m.get('strike','—')} exp {m.get('expiry','—')} "
                                f"DTE {m.get('dte','—')} "
                                f"vol {int(m.get('volume',0)):,} "
                                f"OI {int(m.get('openInterest',0)):,}"
                            )
                        else:
                            st.markdown(f"{'📈' if side=='call' else '📉'} {side.upper()} magnet: none qualified")

                # RSI grid per timeframe
                if tfs:
                    rsi_cols = st.columns(len(tfs))
                    for i, (tf, td) in enumerate(tfs.items()):
                        with rsi_cols[i]:
                            rsi_val = td.get("rsi")
                            st.markdown(
                                f"**{tf}**<br>{_rsi_badge(rsi_val)}",
                                unsafe_allow_html=True,
                            )

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
