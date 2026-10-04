"""Options Flow page — trend, Best Value picks, portfolio, charts and flow detail."""

from __future__ import annotations

from datetime import datetime
import glob
import json

from best_value import build_best_value_df
from best_value_archive import add_times_flagged
from best_value_archive import ensure_archive_loaded
from best_value_archive import filter_today
from best_value_archive import log_best_value_run
from best_value_ui import CONTRACT_KEY_COL
from best_value_ui import STAR_COL
from best_value_ui import apply_display_keep
from best_value_ui import attach_contract_keys
from best_value_ui import best_value_star
from best_value_ui import filter_ranked_display
from best_value_ui import greeks_display_columns
from best_value_ui import hidden_delta_band_caption
from best_value_ui import is_fully_extrinsic
from best_value_ui import pending_add_pos_payload
from best_value_ui import ranked_delta_band_mask
from cost_distribution import BLUE_SKY_TAG
from cost_distribution import is_blue_sky_breakout
from distribution import expiry_distribution
from distribution import prob_beyond_strike
from news_service import get_news_sentiment
from pov_leakage import URGENCY_TAG
from pov_leakage import fetch_pov_leakage
from strategy_engine import attach_optimal_strategy
from strategy_engine import recommend_strategy
from strategy_engine import resolve_has_catalyst
from strategy_engine import resolve_spot_below_support
from strategy_engine import ticker_expected_range
from time_stop import format_exit_by_cell
from ui.components.cost_distribution import cached_cost_distribution as _cached_cost_distribution
from ui.market import _build_best_value_df
from ui.market import _cached_vwap_state
from ui.market import _market_is_closed
from ui.market import _rsi_plain
from volume_analysis import fetch_intraday_vwap_df
from ui import shell
from ui.components import price_chart
from ui.context import ScanContext
from volume_analysis import get_stock_volume_analysis
from zero_dte_gex import STATE_CASCADE
from zero_dte_gex import STATE_SQUEEZE
from zero_dte_gex import calculate_0dte_gamma_flow
from zero_dte_gex import call_put_progress_bar_html
import data_adapter
import pandas as pd
import portfolio_store as portfolio_store
import pre_trade_check as pre_trade_check
import streamlit as st
from ui.common import ET

_SURGE_THRESH = 0.15
_EXIT_THRESH  = -0.15
_EXTENDED_MOVE_PCT = 0.035   # ≥3.5% off intraday low → caution buying Calls
_RUNNER_VEL_THRESH = 0.20    # strong velocity → hold runner (with daily bias)
_SCALE_PREMIUM_PCT = 0.25    # +25% off entry → scale 50%
_STOP_LOSS_PCT = -0.15       # portfolio personal stop
_PORTFOLIO_COLS = list(portfolio_store.EDITOR_COLS)
_PORTFOLIO_LEDGER_COLS = list(portfolio_store.LEDGER_COLS)


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


@st.cache_data(ttl=120)
def _cached_volume_analysis(ticker: str) -> dict:
    """Cached intraday buy/sell/neutral volume breakdown (2 min TTL)."""
    return get_stock_volume_analysis(ticker)


@st.cache_data(ttl=60)
def _cached_vwap_chart_df(ticker: str, timeframe: str = "5M") -> pd.DataFrame:
    """Cached OHLC + VWAP for the candlestick chart at the selected timeframe."""
    return fetch_intraday_vwap_df(ticker, timeframe=timeframe)


@st.cache_data(ttl=60)
def _cached_pov_leakage(ticker: str) -> tuple[pd.DataFrame, dict]:
    """Cached 5m POV participation metrics + urgency flag (1 min TTL)."""
    return fetch_pov_leakage(ticker, last_session_only=True)


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
        # Match evaluate_portfolio key dtypes (editor Strike is often object).
        marks = st.session_state["portfolio_df"][
            ["Ticker", "Side", "Strike", "Expiry", "Mark_Price"]
        ].copy()
        marks["Ticker"] = marks["Ticker"].astype(str).str.strip().str.upper()
        marks["Side"] = (
            marks["Side"].astype(str).str.strip().str.upper()
            .replace({"C": "CALL", "P": "PUT"})
        )
        marks["Strike"] = pd.to_numeric(marks["Strike"], errors="coerce")
        marks["Expiry"] = marks["Expiry"].astype(str).str.strip()
        scored = scored.merge(
            marks,
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
            f"Filters: Volume ≥ {min_vol_input:,} · Price > \\$0.01  ·  "
            f"Showing top **{show_n}** (Settings → Flow filters)  ·  "
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
    if "extrinsic" in df.columns:
        keep.append("extrinsic")
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

    # Snapshot for Check this — extra quote cols stay on scored df, not the table.
    snapshot = pre_trade_check.build_scan_snapshot(
        ticker, df, top5, run_timestamp,
    )
    pre_trade_check.store_scan_snapshot(snapshot)

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

    # Display-only provider greeks from the already-fetched chain (vol_curr).
    # Do not use scoring ``delta`` (Black-Scholes overwrite). Missing → "—".
    greeks_disp = greeks_display_columns(top5, vol_curr)

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
    disp["Delta"] = greeks_disp["Delta"].to_numpy() if not greeks_disp.empty else "—"
    disp["Theta/Prem"] = (
        greeks_disp["Theta/Prem"].to_numpy() if not greeks_disp.empty else "—"
    )
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

    now_et = datetime.now(ET)
    exit_cells = []
    for _, r in top5.iterrows():
        worthless = (
            is_fully_extrinsic(r.get("extrinsic"), r.get("last"))
            if "extrinsic" in r.index else False
        )
        exit_cells.append(
            format_exit_by_cell(
                r.get("dte"), r.get("expiry"),
                now=now_et, fully_extrinsic=worthless,
            )
        )
    disp["Exit by"] = exit_cells

    # Set on the Settings page and saved; not a per-visit toggle any more.
    show_all = bool(shell.setting(st, "show_all_ranked"))
    vis_top5, n_hidden, n_ranked = filter_ranked_display(
        top5, vol_curr, show_all=show_all,
    )
    if show_all:
        vis_disp = disp
        st.caption(
            f"Showing all {n_ranked} ranked contracts (delta band filter off · "
            "change in Settings)."
        )
    else:
        keep = ranked_delta_band_mask(top5, vol_curr)
        vis_disp = apply_display_keep(disp, keep)
        st.caption(hidden_delta_band_caption(n_hidden, n_ranked)
                   + " · show them all in Settings")

    # Interactive table: ＋ column in-row (st.dataframe cannot host buttons)
    _render_best_value_table_with_plus(
        ticker, vis_top5, vis_disp, has_dvol=has_dvol,
        scan_id=snapshot.get("scan_id"),
    )
    # Caption: Velocity/Target columns removed from the table. Score_Velocity is
    # computed as 0.0 for first-seen contracts this session (by design, not a
    # formatting bug); later refreshes can produce non-zero velocity that still
    # feeds Action_Signal. Dropped the Velocity-named exit clauses so the caption
    # does not document a column that is no longer shown.
    st.caption(
        "**SCALE 50%** if premium ≥ +25% vs tracked entry "
        "(prior archive / first-seen).  "
        "Select a row, then **Check this** or **＋ Add … to Open Positions**.  "
        "Default view: 0.35 ≤ |δ| ≤ 0.50 (Pre-Trade band; null δ hidden).  "
        "Delta tones when showing all: 🔴 abs(δ)<0.15 · 🟠 0.15–0.25.  "
        "**Exit by** is now-relative (0DTE +30m / 1DTE+ +60m, cap 15:45 ET)."
    )
    _render_add_position_form(ticker)

    # ── Summary callout (one star per DTE pool) ───────────────────────────────
    best = df[df["Status"].astype(str).str.contains("BEST VALUE", na=False)]
    if not best.empty:
        st.success("  \n".join(_best_value_callout_lines(best)))

    # Charts sit below the scanner table/callout so the table layout stays untouched.
    _render_expiry_distribution_charts(vis_top5, spot)


def _expiry_dist_degenerate_reason(iv, dte_days) -> str:
    """Short caption when expiry_distribution refuses to draw."""
    if iv is None:
        return "no chart: implied volatility unavailable"
    try:
        iv_f = float(iv)
    except (TypeError, ValueError):
        return "no chart: implied volatility unavailable"
    if iv_f < 0.01:
        return "no chart: implied volatility unavailable below 1%"
    try:
        dte_f = float(dte_days)
    except (TypeError, ValueError):
        return "no chart: DTE missing (no fabricated time-to-expiry)"
    if dte_f <= 0:
        return "no chart: DTE is zero (no fabricated time-to-expiry)"
    return "no chart: distribution inputs unusable"


def _render_expiry_distribution_charts(top_df: pd.DataFrame, spot) -> None:
    """
    One lognormal density chart per expiry among the displayed Best Value rows.
    Most-populated expiries first; max 2 charts. Display aid only — not scored.
    """
    import plotly.graph_objects as go

    if top_df is None or top_df.empty or spot is None:
        return
    try:
        spot_f = float(spot)
    except (TypeError, ValueError):
        return
    if spot_f <= 0:
        return

    need = {"expiry", "dte", "strike", "side", "iv"}
    if not need.issubset(set(top_df.columns)):
        return

    work = top_df.reset_index(drop=True).copy()
    if "Value_Score" in work.columns:
        work = work.sort_values("Value_Score", ascending=False, kind="mergesort")

    grouped: list[tuple[str, pd.DataFrame]] = []
    for expiry, g in work.groupby("expiry", sort=False):
        grouped.append((str(expiry), g.reset_index(drop=True)))
    grouped.sort(key=lambda item: len(item[1]), reverse=True)

    if not grouped:
        return

    omitted_groups = grouped[2:]
    show = grouped[:2]
    omitted_n = sum(len(g) for _, g in omitted_groups)

    st.markdown("#### Expiry distribution")
    if omitted_n:
        st.caption(
            f"Showing the {len(show)} most-populated expir"
            f"{'y' if len(show) == 1 else 'ies'}; "
            f"{omitted_n} contract(s) in {len(omitted_groups)} other "
            f"expir{'y' if len(omitted_groups) == 1 else 'ies'} omitted "
            f"(0DTE and longer-dated curves must not share a plot)."
        )

    any_chart = False
    for expiry, g in show:
        # Curve IV = nearest-to-spot contract in this expiry group only.
        nearest_i = (g["strike"].astype(float) - spot_f).abs().idxmin()
        nearest = g.loc[nearest_i]
        curve_iv = nearest.get("iv")
        try:
            dte_days = float(nearest["dte"])
        except (TypeError, ValueError):
            dte_days = None
        # Prefer a representative DTE from the group (same expiry → same dte).
        if dte_days is None or (isinstance(dte_days, float) and dte_days != dte_days):
            dte_days = pd.to_numeric(g["dte"], errors="coerce").dropna()
            dte_days = float(dte_days.iloc[0]) if len(dte_days) else None

        dist = expiry_distribution(spot_f, curve_iv, dte_days)
        if dist is None:
            st.caption(_expiry_dist_degenerate_reason(curve_iv, dte_days))
            continue

        prices, density = dist
        try:
            curve_k = float(nearest["strike"])
            dte_label = int(dte_days) if dte_days is not None else "?"
        except (TypeError, ValueError):
            curve_k, dte_label = float("nan"), "?"

        st.markdown(
            f"spot ${spot_f:.2f} · expiry {expiry} ({dte_label}d) · "
            f"curve uses IV from the ${curve_k:.1f} contract"
        )

        # Strike lines: top 5 by rank (Value_Score order already applied).
        line_rows = g.head(5)
        # Shade selectbox: all contracts in the group, default top-ranked.
        shade_labels = []
        shade_meta = []
        for _, row in g.iterrows():
            try:
                k = float(row["strike"])
                side = str(row["side"]).upper()
                iv = row.get("iv")
                dte = float(row["dte"])
            except (TypeError, ValueError):
                continue
            pr = prob_beyond_strike(spot_f, k, iv, dte, side)
            label = (
                f"{side} ${k:.1f}"
                + (f" · P={pr:.0%}" if pr is not None else " · P=—")
            )
            shade_labels.append(label)
            shade_meta.append((k, side, pr, iv, dte))

        if not shade_labels:
            st.caption("no chart: no usable strikes in this expiry group")
            continue

        pick = st.selectbox(
            f"Shade payoff beyond strike ({expiry})",
            options=list(range(len(shade_labels))),
            format_func=lambda i, labels=shade_labels: labels[i],
            index=0,
            key=f"exp_dist_shade_{expiry}",
        )
        shade_k, shade_side, shade_pr, _, _ = shade_meta[pick]

        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=prices,
            y=density,
            mode="lines",
            name="density",
            line=dict(color="#4FC3F7", width=2),
            hovertemplate="S=%{x:.2f}<br>dens=%{y:.4g}<extra></extra>",
        ))

        # Shade: CALL → right of strike; PUT → left of strike.
        shade_x: list[float] = []
        shade_y: list[float] = []
        for px, dens in zip(prices, density):
            if shade_side in ("CALL", "C") and px >= shade_k:
                shade_x.append(px)
                shade_y.append(dens)
            elif shade_side in ("PUT", "P") and px <= shade_k:
                shade_x.append(px)
                shade_y.append(dens)
        if shade_x:
            fig.add_trace(go.Scatter(
                x=[shade_x[0]] + shade_x + [shade_x[-1]],
                y=[0.0] + shade_y + [0.0],
                fill="toself",
                fillcolor="rgba(255, 193, 7, 0.25)",
                line=dict(width=0),
                name=(
                    f"P(beyond ${shade_k:.1f})"
                    + (f"={shade_pr:.0%}" if shade_pr is not None else "")
                ),
                hoverinfo="skip",
            ))

        fig.add_vline(
            x=spot_f,
            line_width=1.5,
            line_dash="solid",
            line_color="#FFFFFF",
            annotation_text="spot",
            annotation_position="top",
        )

        for _, row in line_rows.iterrows():
            try:
                k = float(row["strike"])
                side = str(row["side"]).upper()
                iv = row.get("iv")
                dte = float(row["dte"])
            except (TypeError, ValueError):
                continue
            pr = prob_beyond_strike(spot_f, k, iv, dte, side)
            pr_txt = f"{pr:.0%}" if pr is not None else "—"
            fig.add_vline(
                x=k,
                line_width=1,
                line_dash="dot",
                line_color="#FF8A65",
                annotation_text=f"${k:.1f} · {pr_txt}",
                annotation_position="top right",
                annotation_font_size=10,
            )

        fig.update_layout(
            height=320,
            margin=dict(l=40, r=20, t=30, b=40),
            xaxis_title="Underlying price at expiry",
            yaxis_title="Density",
            showlegend=False,
            template="plotly_dark",
        )
        st.plotly_chart(
            fig,
            use_container_width=True,
            config={"displayModeBar": False},
            key=f"exp_dist_chart_{expiry}",
        )
        any_chart = True

    if any_chart:
        st.caption(
            "Lognormal distribution implied by the contract's own IV. "
            "Probabilities are risk-neutral, not a forecast, and say nothing "
            "about whether the option is fairly priced."
        )


def _render_best_value_table_with_plus(
    ticker: str,
    top5: pd.DataFrame,
    disp: pd.DataFrame,
    *,
    has_dvol: bool,
    scan_id: str | None = None,
) -> None:
    """Best Value table with native single-row selection + Add button below."""
    if top5 is None or top5.empty or disp is None or disp.empty:
        return

    top5_r = top5.reset_index(drop=True)
    disp_r = disp.reset_index(drop=True)

    show_cols = [
        STAR_COL, "Side", "Strike", "Expiry", "DTE", "Exit by", "Price", "Volume", "OI",
    ]
    if has_dvol and "ΔVol" in disp_r.columns:
        show_cols.append("ΔVol")
    # Velocity / Target dropped — typically uniform zeros/"—" and steal width
    # from Optimal Strategy. Score_Velocity still drives Action_Signal upstream.
    # TODO(2026-08-28): Signal often renders "(0)" and Optimal Strategy is
    # identical across rows even as Value_Score ranges (~0.08–0.29). Consistent
    # with known flow_norm / leverage_norm / category-multiplier bugs. Do not
    # fix during the measurement window.
    show_cols += [
        "IV", "Delta", "Theta/Prem", "Value_Score", "Signal", "Optimal Strategy",
    ]

    # ★ marker replaces pandas Styler highlight (see note below on Streamlit 1.37.1).
    view = disp_r.copy()
    if "Status" in top5_r.columns:
        view[STAR_COL] = [best_value_star(s) for s in top5_r["Status"]]
    else:
        view[STAR_COL] = ""
    # Hidden identity column — selection resolves by key, not display position,
    # so client-side column sorts cannot attach the wrong contract.
    view[CONTRACT_KEY_COL] = attach_contract_keys(top5_r).to_numpy()
    show_cols = [c for c in show_cols if c in view.columns or c == STAR_COL]
    # Ensure ★ is present even if not in disp_r originally
    ordered = [c for c in show_cols if c in view.columns]
    view = view[ordered + [CONTRACT_KEY_COL]].copy()

    # Streamlit 1.37.1 (verified via arrow.py marshall path): both Styler CSS and
    # column_config are written into the Arrow proto. Styler CSS is row-index
    # tied (#T_…rowN_colM), so after a header sort the green highlight paints the
    # wrong visual row (or sorting feels broken). Widths matter more for Optimal
    # Strategy truncation — keep column_config, drop Styler, use ★ instead.
    col_cfg: dict = {
        STAR_COL: st.column_config.TextColumn(STAR_COL, width="small"),
        "Side": st.column_config.TextColumn("Side", width="small"),
        "Strike": st.column_config.TextColumn("Strike", width="small"),
        "Expiry": st.column_config.TextColumn("Expiry", width="medium"),
        "DTE": st.column_config.TextColumn("DTE", width="small"),
        "Exit by": st.column_config.TextColumn("Exit by", width="medium"),
        "Price": st.column_config.TextColumn("Price", width="small"),
        "Volume": st.column_config.TextColumn("Volume", width="small"),
        "OI": st.column_config.TextColumn("OI", width="small"),
        "ΔVol": st.column_config.TextColumn("ΔVol", width="small"),
        "IV": st.column_config.TextColumn("IV", width="small"),
        "Delta": st.column_config.TextColumn("Delta", width="small"),
        "Theta/Prem": st.column_config.TextColumn("Theta/Prem", width="small"),
        "Value_Score": st.column_config.TextColumn("Value_Score", width="small"),
        "Signal": st.column_config.TextColumn("Signal", width="medium"),
        "Optimal Strategy": st.column_config.TextColumn(
            "Optimal Strategy", width="large",
        ),
        # Hidden: None → ColumnConfig(hidden=True) in process_config_mapping
        CONTRACT_KEY_COL: None,
    }
    col_cfg = {k: v for k, v in col_cfg.items() if k in view.columns}

    table_key = f"bv_select_{str(ticker).upper()}"

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
        # Older Streamlit: no on_select / column_config combo — fallback picker
        st.dataframe(
            view.drop(columns=[CONTRACT_KEY_COL], errors="ignore"),
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

    payload = pending_add_pos_payload(
        ticker, top5_r, sel, display=view,
    )
    if payload is None:
        st.caption("Select a row, then Check this or add to Open Positions.")
        return

    add_label = (
        f"＋  Add {payload['Side']} ${float(payload['Strike']):.1f} · "
        f"{payload['Expiry']} to Open Positions"
    )
    b_add, b_chk = st.columns(2)
    with b_add:
        if st.button(add_label, type="primary", key=f"bv_add_selected_{ticker}"):
            st.session_state["_pending_add_pos"] = payload
            st.rerun()
    with b_chk:
        if st.button("Check this", key=f"bv_check_selected_{ticker}"):
            cid = None
            if sel and CONTRACT_KEY_COL in view.columns:
                try:
                    cid = str(view.reset_index(drop=True).iloc[int(sel[0])][CONTRACT_KEY_COL])
                except (IndexError, TypeError, ValueError, KeyError):
                    cid = None
            if cid and scan_id:
                ref = pre_trade_check.candidate_ref(scan_id, cid)
                pre_trade_check.set_candidate_query(st, ref)
                # jump on the next run: main() opens Pre-Trade Check for a new candidate
                st.session_state.pop("ptc_nav_done_for", None)
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


def _best_value_callout_lines(best: pd.DataFrame) -> list[str]:
    """One line per DTE pool for the Best Value callout. Dollar signs are escaped so
    the strike and the premium on one line are not rendered as LaTeX math."""
    from scoring_pool import POOL_0DTE, POOL_1DTE

    lines = []
    for pool_name in (POOL_1DTE, POOL_0DTE):
        sub = best[best["pool"] == pool_name] if "pool" in best.columns else best
        if sub.empty:
            lines.append(f"⭐ **{pool_name}** — not ranked")
            continue
        b = sub.iloc[0]
        voi = b["volume"] / max(int(b["openInterest"]), 1)
        dte_s = (
            f"{int(b['dte'])}d"
            if "dte" in b.index and pd.notna(b.get("dte"))
            else "?"
        )
        lines.append(
            f"⭐ **{pool_name}** {b['side']} \\${b['strike']:.1f} "
            f"{b['expiry']} ({dte_s}) · score {b['Value_Score']:.2f} · "
            f"\\${b['last']:.2f} · vol/OI {voi:.1f}x"
        )
    return lines


def _usd(v) -> str:
    """Dollar amount for st.caption / st.markdown text: the ``$`` is escaped so two
    amounts on one line are not rendered as LaTeX math."""
    return f"\\${float(v):.2f}"


SECTIONS = ("header", "chart", "context", "positions", "news", "best_value", "details")
# Options Flow shows the market picture. The chart and Best Value have their own pages.
OVERVIEW = frozenset({"header", "context", "positions", "news", "details"})


def render_best_value(cfg: dict) -> None:
    """Best Value page: the ranked picks only."""
    render(cfg, sections=frozenset({"best_value"}))


def render(cfg: dict, sections: frozenset[str] | None = None):
    """Options Flow — market picture for the selected ticker.

    ``sections`` picks which parts are drawn (see SECTIONS); the default is OVERVIEW.
    Every section reads the same scan context, loaded once at the top.
    """
    sections = OVERVIEW if sections is None else frozenset(sections)
    unknown = sections - set(SECTIONS)
    if unknown:
        raise ValueError(f"unknown sections: {sorted(unknown)}")

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

    if "header" in sections:
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
            f'<div style="font-size:0.7rem;letter-spacing:0.08em;color:#888;text-transform:uppercase">'
            f'Scanner direction · multi-timeframe score (the trend rule above decides calls or puts)</div>'
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
                detail.append(f"EM ±{_usd(em_s)}")
            if iv_s is not None:
                detail.append(f"IV {float(iv_s):.1%}")
            if dte_s is not None:
                detail.append(f"DTE {float(dte_s):.0f}d")
            st.caption(
                f"**1SD Expected Range:** "
                f"{_usd(em_range['Lower_1SD'])} – {_usd(em_range['Upper_1SD'])}"
                + (f"  ·  {' · '.join(detail)}" if detail else "")
                + f"  ·  **Strategy:** {optimal_strat}"
            )

    if "chart" in sections:
        # ══════════════════════════════════════════════════════════════════════════
        # ZONE 2 — Main Workspace Grid
        # ══════════════════════════════════════════════════════════════════════════
        with st.container():
            # One chart: candles + EMA 9/21/50 + VWAP, volume with participation, stochastic.
            price_chart.render(ScanContext(ticker=ticker, curr=curr, prev=prev, top_n=int(top_n)))

            # Institutional POV leakage read-out (5-minute participation math)
            if pov_df is not None and not pov_df.empty:
                if pov_info.get("urgency"):
                    st.caption(
                        f"**{URGENCY_TAG}** · last bar POV "
                        f"**{pov_info.get('ratio')}×** · price above VWAP"
                    )
                elif pov_info.get("ratio") is not None:
                    st.caption(
                        f"POV last 5-minute bar: **{pov_info.get('ratio')}×** "
                        f"(leakage threshold {3.0:.1f}×) · "
                        f"{'above' if pov_info.get('above_vwap') else 'below/at'} VWAP"
                    )

    if "context" in sections:
        # Row 1: Volume Analysis | Multi-Timeframe
        sub_c1, sub_c2 = st.columns([1, 1.4])
        with sub_c1:
            _render_volume_analysis(ticker, compact=True, vol_curr=vol)
        with sub_c2:
            _render_mtf_matrix(tfs, prev_tfs)

    if "positions" in sections:
        # Row 2: My Open Positions, full width
        _render_portfolio_manager(
            ticker, vol, spot, prev_vol,
            daily_bias=(daily_bias_info or {}).get("daily_bias"),
            market_state=(market_state_info or {}).get("market_state"),
            news_bias=news_bias,
            compact=True,
        )

    if "news" in sections:
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

    if "best_value" in sections:
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

        # Flow Magnets, Expiration Breakdown and Cost Distribution are their own menu
        # pages now (ui/components); they are no longer repeated here.

    if "details" in sections:
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

