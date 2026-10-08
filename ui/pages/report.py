"""Report page — one period at a glance: the stock, the scanner's top picks, gamma, and
your own trades. Display only."""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

import gex
import period_report as pr
import trade_history as th
from ui import broker_files
from ui.common import ET
from ui.widgets import centered_table_html
from volume_analysis import fetch_intraday_vwap_df

PERIODS = ["1 day", "5 days", "1 month", "Custom"]


@st.cache_data(ttl=300, show_spinner=False)
def _daily_bars(ticker: str) -> pd.DataFrame:
    return fetch_intraday_vwap_df(ticker, last_session_only=False, timeframe="1D")


@st.cache_data(ttl=300, show_spinner=False)
def _picks(ticker: str, days: tuple, stamp: str) -> list[dict]:
    return pr.picks_report(ticker, list(days))


@st.cache_data(ttl=300, show_spinner=False)
def _gamma(ticker: str, days: tuple, stamp: str) -> list[dict]:
    return pr.gamma_report(ticker, list(days))


def resolve_period(choice: str, end: date, bar_days: list[date],
                   custom_start: date | None = None) -> tuple[date, date]:
    """(start, end) for a preset. The end snaps back to the latest day that has a price
    bar, so "1 day" on a weekend means the last session, not an empty day."""
    usable = [d for d in bar_days if d <= end]
    last = usable[-1] if usable else end
    if choice == "1 day":
        return last, last
    if choice == "5 days":
        return (usable[-5] if len(usable) >= 5 else (usable[0] if usable else last)), last
    if choice == "1 month":
        return last - timedelta(days=30), last
    start = custom_start or last
    return (min(start, end), end)


def usd(v, signed: bool = False) -> str:
    if v is None or pd.isna(v):
        return "—"
    sign = "-" if v < 0 else "+" if signed and v > 0 else ""
    return f"{sign}${abs(v):,.2f}"


def _section(title: str) -> None:
    st.markdown(f"#### {title}")


def render_stock(stock: dict) -> None:
    _section("1 · The stock")
    if not stock:
        st.info("No price bars for this period.")
        return
    c = st.columns(5)
    c[0].metric("Open", usd(stock["open"]), help=f"Open on {stock['first_day']:%a %b %d}.")
    c[1].metric("High", usd(stock["high"]), help=f"Reached on {stock['high_day']:%a %b %d}.")
    c[2].metric("Low", usd(stock["low"]), help=f"Reached on {stock['low_day']:%a %b %d}.")
    c[3].metric("Close", usd(stock["close"]), help=f"Close on {stock['last_day']:%a %b %d}.")
    pct = "" if stock["change_pct"] is None else f" ({stock['change_pct']:+.2%})"
    c[4].metric("Change", usd(stock["change"], signed=True) + pct,
                help="Close of the last day against the open of the first day.")
    days = stock["days"]
    if len(days) > 1:
        st.markdown(centered_table_html(pd.DataFrame({
            "Day": [f"{d:%a %b %d}" for d in days["day"]],
            "Open": days["Open"].map(usd), "High": days["High"].map(usd),
            "Low": days["Low"].map(usd), "Close": days["Close"].map(usd),
            "Change": days["change"].map(lambda v: usd(v, signed=True)),
        }), "stock"), unsafe_allow_html=True)


def picks_frame(picks: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{
        "Day": f"{p['day']:%a %b %d}",
        "Pool": p["pool"],
        "Contract": f"{p['side']} ${p['strike']:g} {p['expiry'][5:]}",
        "Picked at": f"{p['picked_at']:%H:%M}",
        "Open": usd(p["open"]),
        "High": usd(p["high"]), "Low": usd(p["low"]), "Close": usd(p["close"]),
        "Change": "—" if p["change_pct"] is None else f"{p['change_pct']:+.0%}",
    } for p in picks])


def render_picks(picks: list[dict]) -> None:
    _section("2 · The scanner's top pick")
    if not picks:
        st.info("No ranked pick was stored for this period. Picks are read from the scans "
                "at or after 9:45.")
        return
    st.markdown(centered_table_html(picks_frame(picks), "picks"), unsafe_allow_html=True)
    st.caption("The top-ranked contract of each pool in the day's first scan at or after "
               "9:45. Open is its price in that scan; high, low and close are from the "
               "15-minute chain snapshots for the rest of that day. Prices are per share.")


def gamma_frame(rows: list[dict]) -> pd.DataFrame:
    def wall(strike, amount):
        return "—" if strike is None else f"${strike:g} ({gex.fmt_money(amount)})"

    return pd.DataFrame([{
        "Day": f"{g['day']:%a %b %d}",
        "Net high": f"{gex.fmt_money(g['net_high'])} at {g['net_high_at'].astimezone(ET):%H:%M}",
        "Net low": f"{gex.fmt_money(g['net_low'])} at {g['net_low_at'].astimezone(ET):%H:%M}",
        "Net at last reading": gex.fmt_money(g["net_last"]),
        "Call wall": wall(g["call_wall"], g["call_wall_gex"]),
        "Put wall": wall(g["put_wall"], g["put_wall_gex"]),
    } for g in rows])


def render_gamma(rows: list[dict]) -> None:
    _section("3 · Gamma")
    if not rows:
        st.info("No chain snapshots for this period. Recording began on Sep 28, and each "
                "snapshot needs the archive of its scan.")
        return
    st.markdown(centered_table_html(gamma_frame(rows), "gamma"), unsafe_allow_html=True)
    readings = sum(g["readings"] for g in rows)
    skipped = sum(g["skipped"] for g in rows)
    st.caption(f"Net gamma exposure for the nearest expiry, in dollars of dealer hedging per "
               f"\\$1 move, from {readings} snapshots"
               + (f" ({skipped} skipped: no archive or no usable contracts)" if skipped else "")
               + ". Walls are as of the day's last reading.")


def render_trades(card: dict, path: str | None) -> None:
    _section("4 · Your trades")
    if path is None:
        st.info("No trade file yet. Add your broker export on the Scorecard page.")
        return
    if not card:
        st.info("No closed trades in this period.")
        return
    c = st.columns(5)
    c[0].metric("Trades", f"{card['trades']:,}",
                help=f"{card['wins']} wins, {card['losses']} losses, "
                     f"{card['scratches']} closed at break-even.")
    c[1].metric("Winning trades", f"{card['wins']:,} ({card['win_rate']:.0%})")
    c[2].metric("Gained", usd(card["gained"]))
    c[3].metric("Lost", usd(card["lost"]))
    rr = card["reward_to_risk"]
    c[4].metric("Reward to risk", "—" if rr is None else f"{rr:.2f}",
                help="Average win per contract divided by average loss per contract."
                     + ("" if card["reward_to_risk_needed"] is None else
                        f" To break even at this win rate it needs "
                        f"{card['reward_to_risk_needed']:.2f}."))
    st.caption(f"Net result {usd(card['net'], signed=True).replace('$', chr(92) + '$')} · "
               f"from {os.path.basename(path)} · trades are counted by the day they were entered.")


def render(cfg: dict) -> None:
    ticker = str(cfg.get("ticker") or "AAPL").upper()
    st.subheader(f"{ticker} report")

    today = datetime.now(ET).date()
    left, mid, right = st.columns([3, 1, 1])
    choice = left.radio("Period", PERIODS, horizontal=True, key="report_period")
    end = right.date_input("Ending", value=today, max_value=today, key="report_end")
    custom_start = None
    if choice == "Custom":
        custom_start = mid.date_input("From", value=end - timedelta(days=4), max_value=end,
                                      key="report_start")

    try:
        bars = _daily_bars(ticker)
    except Exception as exc:                       # network or data-source failure
        bars = pd.DataFrame()
        st.warning(f"Price bars are unavailable ({type(exc).__name__}). "
                   "The stock section is empty; the rest uses recorded scans.")
    bar_days = sorted(set(pd.to_datetime(bars.index).date)) if not bars.empty else []
    start, end = resolve_period(choice, end, bar_days, custom_start)
    if (end - start).days > pr.MAX_DAYS:
        start = end - timedelta(days=pr.MAX_DAYS)
        st.caption(f"Reports cover at most {pr.MAX_DAYS} days; showing from {start:%b %d}.")
    st.caption(f"{start:%a %b %d, %Y}" if start == end
               else f"{start:%a %b %d} to {end:%a %b %d, %Y}")

    stock = pr.stock_period(bars, start, end)
    days = pr.trading_days(start, end, stock)
    stamp = f"{today}:{len(pr.archives_for(ticker, end))}"     # refresh as today's scans land

    render_stock(stock)
    render_picks(_picks(ticker, tuple(days), stamp))
    render_gamma(_gamma(ticker, tuple(days), stamp))

    path = broker_files.latest_export()
    card: dict = {}
    if path is not None:
        try:
            trades, _ = th.closed_trades(th.load_activities(path))
            card = pr.scorecard_period(pr.trades_in(trades, start, end, ticker))
        except ValueError as exc:
            st.error(f"{os.path.basename(path)} could not be read: {exc}")
    render_trades(card, path)
