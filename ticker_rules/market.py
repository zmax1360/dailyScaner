"""market — the context the rules read: 15-minute bars with session VWAP, the day's first
5-minute bar and its relative volume. Built from the configured market-data source."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta

import pandas as pd

RVOL_DAYS = 14


@dataclass
class Context:
    now: datetime                 # naive or aware; compared by wall-clock time
    bars: pd.DataFrame            # 15-min, complete at ``now``, today and earlier
    first5: pd.Series | None      # today's 09:30 5-min bar
    rvol: float | None            # first5 volume / mean of the previous RVOL_DAYS


def _naive_et(idx) -> pd.DatetimeIndex:
    di = pd.DatetimeIndex(idx)
    if di.tz is not None:
        di = di.tz_convert("America/New_York").tz_localize(None)
    return di


def build(five: pd.DataFrame, now: datetime) -> Context:
    """``five``: 5-min OHLCV bars, regular session only (any tz or naive ET)."""
    now_n = pd.Timestamp(now)
    if now_n.tzinfo is not None:
        now_n = now_n.tz_convert("America/New_York").tz_localize(None)
    df = five.copy()
    df.index = _naive_et(df.index)
    df = df[df.index + timedelta(minutes=5) <= now_n]           # complete 5-min bars only
    if df.empty:
        return Context(now=now, bars=pd.DataFrame(), first5=None, rvol=None)
    day = df.index.date
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    pv = (tp * df["Volume"]).groupby(day).cumsum()
    vv = df["Volume"].groupby(day).cumsum()
    df["VWAP"] = pv / vv.where(vv > 0)
    b = df.resample("15min").agg({"Open": "first", "High": "max", "Low": "min",
                                  "Close": "last", "Volume": "sum", "VWAP": "last"})
    b = b.dropna(subset=["Close"])
    b = b[b.index + timedelta(minutes=15) <= now_n]
    today = now_n.date()
    opens = df[df.index.time == time(9, 30)]
    first5 = opens[opens.index.date == today]
    f5 = first5.iloc[0] if len(first5) else None
    prev = opens[opens.index.date < today]["Volume"].tail(RVOL_DAYS)
    rvol = None
    if f5 is not None and len(prev) >= 5 and prev.mean() > 0:
        rvol = float(f5["Volume"] / prev.mean())
    return Context(now=now, bars=b, first5=f5, rvol=rvol)


def fetch(ticker: str, now: datetime, *, source=None) -> Context:
    """Fetch ~1 month of 5-min bars (enough for EMA 100 on 15-min and 14-day RVOL)."""
    from volume_analysis import _resolve_source, regular_session_only
    src = _resolve_source(source)
    hist = src.fetch_history(ticker, interval="5m", period="1mo")
    if hist is None or hist.empty:
        return Context(now=now, bars=pd.DataFrame(), first5=None, rvol=None)
    hist = regular_session_only(hist[["Open", "High", "Low", "Close", "Volume"]])
    return build(hist, now)
