"""chart_indicators — per-bar indicator series for the price chart.

Pure functions over an OHLCV frame: no network, no Streamlit, no writes. Display only;
nothing here feeds scoring. A value that cannot be computed yet (not enough bars) is
NaN, never a default.

EMA uses the same definition as ``ema_stack.compute_emas`` (span, adjust=False), so the
last value of ``ema_series`` equals the trend banner's EMA on the same bars.
"""

from __future__ import annotations

import pandas as pd

EMA_PERIODS = (9, 21, 50)
STOCH_K = 14
STOCH_D = 3
ATR_PERIOD = 14

OHLC = ["Open", "High", "Low", "Close"]


def _num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def ema_series(close: pd.Series, period: int) -> pd.Series:
    """EMA of ``close``; NaN until ``period`` bars exist."""
    c = _num(close)
    out = c.ewm(span=period, adjust=False).mean()
    out[c.notna().cumsum() < period] = float("nan")
    return out


def fast_stochastic(df: pd.DataFrame, k: int = STOCH_K, d: int = STOCH_D) -> pd.DataFrame:
    """Fast stochastic: %K = 100 * (close - lowest low) / (highest high - lowest low)
    over ``k`` bars; %D = ``d``-bar simple average of %K. NaN during warm-up and when
    the range is zero."""
    high, low, close = _num(df["High"]), _num(df["Low"]), _num(df["Close"])
    hh = high.rolling(k, min_periods=k).max()
    ll = low.rolling(k, min_periods=k).min()
    rng = (hh - ll).where((hh - ll) > 0)
    pct_k = 100.0 * (close - ll) / rng
    pct_d = pct_k.rolling(d, min_periods=d).mean()
    return pd.DataFrame({"StochK": pct_k, "StochD": pct_d}, index=df.index)


def atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> pd.Series:
    """Average True Range, Wilder smoothing. NaN until ``period`` true ranges exist."""
    high, low, close = _num(df["High"]), _num(df["Low"]), _num(df["Close"])
    prev = close.shift(1)
    tr = pd.concat([high - low, (high - prev).abs(), (low - prev).abs()], axis=1).max(axis=1)
    tr[prev.isna()] = (high - low)[prev.isna()]
    out = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    out[tr.notna().cumsum() < period] = float("nan")
    return out


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Copy of ``df`` with EMA9/EMA21/EMA50, StochK/StochD and ATR columns.

    Compute this on the full history (so the averages are warmed up) and slice to the
    window you want to show afterwards. Returns an empty frame when OHLC is missing.
    """
    if df is None or getattr(df, "empty", True) or any(c not in df.columns for c in OHLC):
        return pd.DataFrame()
    out = df.copy()
    for p in EMA_PERIODS:
        out[f"EMA{p}"] = ema_series(out["Close"], p)
    stoch = fast_stochastic(out)
    out["StochK"], out["StochD"] = stoch["StochK"], stoch["StochD"]
    out["ATR"] = atr(out)
    return out


def last_session(df: pd.DataFrame, tz: str = "America/New_York") -> pd.DataFrame:
    """Rows of the most recent trading day.

    A timezone-aware index is converted to ``tz`` first. A naive index is taken as
    exchange wall-clock time already (the Yahoo source strips the timezone that way).
    """
    if df is None or getattr(df, "empty", True):
        return pd.DataFrame()
    idx = pd.DatetimeIndex(df.index)
    days = (idx.tz_convert(tz) if idx.tz is not None else idx).date
    return df[days == days[-1]]
