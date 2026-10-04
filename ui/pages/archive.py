"""Scanner Archive page — browse past scan runs, expiry drill-downs and greeks."""

from __future__ import annotations

from datetime import datetime
import glob
import json

from best_value_archive import add_times_flagged
from best_value_archive import archive_csv_bytes
from best_value_archive import clear_todays_log
from best_value_archive import ensure_archive_loaded
from best_value_archive import filter_today
from best_value_archive import most_persistent_today
from best_value_ui import CONTRACT_KEY_COL
from best_value_ui import contract_key
from ui.option_math import _bs_greeks
import pandas as pd
import pre_trade_check as pre_trade_check
import streamlit as st
from ui.common import ET


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


def render(cfg: dict):
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
        ticker=ticker,
        scan_ts=payload.get("timestamp"),
    )


def _render_greeks_panel(
    vol_curr: dict, spot: float,
    vol_prev: dict | None = None, prev_spot: float = 0.0,
    *,
    ticker: str = "",
    scan_ts: str | None = None,
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

    def _quote_cell(v: float) -> str:
        if v != v or v <= 0:
            return "—"
        return f"${v:.2f}"

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
            oi     = c.get("openInterest")
            try:
                bid = float(c["bid"]) if c.get("bid") is not None else float("nan")
            except (TypeError, ValueError):
                bid = float("nan")
            try:
                ask = float(c["ask"]) if c.get("ask") is not None else float("nan")
            except (TypeError, ValueError):
                ask = float("nan")

            delta, gamma, theta = _bs_greeks(
                spot, strike, iv, dte, is_call=is_call,
            )

            # Previous greeks
            p = prev_lookup.get((side, strike, expiry))
            if p and prev_spot > 0:
                p_iv  = float(p.get("impliedVolatility") or 0)
                p_dte = int(p.get("dte") or 0)
                _, pg, pt = _bs_greeks(
                    prev_spot, strike, p_iv, p_dte, is_call=is_call,
                )
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
                "Bid":     _quote_cell(bid),
                "Ask":     _quote_cell(ask),
                "IV":      f"{iv:.1%}" if iv > 0 else "—",
                "Delta":   f"{abs(delta):.3f}" if delta is not None else "—",
                "Gamma":   f"{gamma:.5f}" if gamma is not None else "—",
                "Theta/d": f"${theta:.4f}" if theta is not None else "—",
                "_strike": strike,
                "_dte":    dte,
                "_delta":  delta,
                "_gamma":  gamma,
                "_theta":  theta,
                # Unit of _theta from _bs_greeks (calendar-day dollars). Do not assume at prefill.
                "_theta_units": "per day",
                "_oi":     oi,
                "_bid":    bid if bid == bid and bid > 0 else None,
                "_ask":    ask if ask == ask and ask > 0 else None,
                CONTRACT_KEY_COL: contract_key(side, strike, expiry),
            }
            if vol_prev:
                row["ΔGamma"] = _signed_greek(d_gamma, 5)
                row["ΔTheta"] = _signed_greek(d_theta, 4)
            rows.append(row)

    if not rows:
        st.caption("No contract data for greeks.")
        return

    df = pd.DataFrame(rows)

    display_cols = ["Side","Strike","Expiry","DTE","Price","Bid","Ask","IV","Delta","Gamma","Theta/d"]
    if vol_prev:
        display_cols = ["Side","Strike","Expiry","DTE","Price","Bid","Ask","IV","Delta",
                        "Gamma","ΔGamma","Theta/d","ΔTheta"]

    view = df[display_cols + [CONTRACT_KEY_COL]].copy()
    col_cfg = {CONTRACT_KEY_COL: None}
    table_key = f"greeks_select_{str(ticker or 'NA').upper()}"
    sel: list[int] = []
    try:
        event = st.dataframe(
            view,
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
        st.dataframe(
            view.drop(columns=[CONTRACT_KEY_COL], errors="ignore"),
            use_container_width=True,
            hide_index=True,
            key=f"{table_key}_fallback_df",
        )
        labels = [
            f"{r['Side']} {r['Strike']} · {r['Expiry']}"
            for _, r in df.iterrows()
        ]
        pick = st.selectbox(
            "Contract to check",
            options=["—"] + labels,
            key=f"{table_key}_fallback_pick",
        )
        if pick and pick != "—":
            sel = [labels.index(pick)]

    raw = df.reset_index(drop=True)
    chosen = None
    if sel:
        try:
            chosen = raw.iloc[int(sel[0])]
        except (IndexError, TypeError, ValueError):
            chosen = None

    if chosen is None:
        st.caption("Select a row, then **Check this** to prefill Pre-Trade Check.")
    else:
        refuse = pre_trade_check.archive_greeks_refusal(
            chosen.get("_delta"), chosen.get("_theta"),
        )
        help_s = refuse or "Open Pre-Trade Check with this contract prefilled."
        if refuse:
            st.caption(refuse)
        if st.button(
            "Check this",
            key=f"greeks_check_{str(ticker or 'NA').upper()}",
            disabled=bool(refuse),
            help=help_s,
        ):
            prefill = pre_trade_check.archive_prefill_from_row(
                chosen, ticker=ticker, underlying=spot, scan_ts=scan_ts,
            )
            if prefill is None:
                st.warning(refuse or "Cannot prefill this row.")
            else:
                cid = str(chosen.get(CONTRACT_KEY_COL) or "")
                pre_trade_check.stage_archive_prefill(
                    st, prefill, contract_id=cid or None,
                )
                st.rerun()

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
