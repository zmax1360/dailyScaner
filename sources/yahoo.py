"""
sources.yahoo — yfinance MarketDataSource (parallel implementation).

MOVE of existing fetch logic from dailyScaner / data_adapter / attribution.
Callers are NOT rewired in this step — this module is additive only.

Preserved intentionally (do not "fix" here):
  - ``_yf_retry`` attempts / base_sleep / rate-limit backoff
  - bid/ask ``fillna(0)`` (chain_quality owns usability)
  - volume / openInterest ``fillna(0)`` as in dailyScaner.fetch_data
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from sources.base import CHAIN_COLUMNS, validate_chain

ET = ZoneInfo("America/New_York")


def _yf_retry(fn, *, label: str, attempts: int = 5, base_sleep: float = 3.0):
    """Call Yahoo via yfinance with backoff on rate limits / transient errors.

    ValueError (e.g. expiry/strike not found) is permanent — never retried.
    """
    last_err = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except ValueError:
            raise
        except Exception as e:
            last_err = e
            name = type(e).__name__
            rate_limited = (
                name == "YFRateLimitError"
                or "Too Many Requests" in str(e)
                or "Rate limited" in str(e)
            )
            if attempt >= attempts:
                break
            wait = base_sleep * (2 ** (attempt - 1))
            if rate_limited:
                wait = max(wait, 15.0 * attempt)
            print(
                f"  Yahoo {label} retry {attempt}/{attempts} "
                f"after {wait:.0f}s ({name})",
                flush=True,
            )
            time.sleep(wait)
    raise last_err


def _clean_history(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    if hasattr(df.index, "tz"):
        df = df.copy()
        df.index = df.index.tz_localize(None) if df.index.tz else df.index
    return df


def fetch_underlying_close_on(ticker: str, day: date) -> float | None:
    """
    Equity close on an ET calendar ``day`` (used for expiry intrinsic marks).

    Returns None when the bar is confirmed missing / non-positive.
    Propagates transient transport errors after ``_yf_retry`` exhausts.
    """
    end = day + timedelta(days=1)
    hist = _yf_retry(
        lambda: yf.Ticker(ticker).history(
            start=day.isoformat(),
            end=end.isoformat(),
            auto_adjust=True,
        ),
        label=f"close {ticker} {day.isoformat()}",
        attempts=3,
        base_sleep=2.0,
    )
    hist = _clean_history(hist)
    if hist is None or hist.empty or "Close" not in hist.columns:
        return None
    close = float(hist["Close"].iloc[-1])
    if not (close == close and close > 0):
        return None
    return close


def _today_et() -> datetime.date:
    return datetime.now(ET).date()


def chain_frame_from_yahoo_legs(
    calls: list[pd.DataFrame],
    puts: list[pd.DataFrame],
    *,
    today_et: datetime.date | None = None,
) -> pd.DataFrame:
    """
    Map Yahoo option legs (already tagged with expiry) into CHAIN_COLUMNS.

    Preserves bid/ask/volume/OI fillna(0). Emits NaN delta (never 0.5).
    IV of exactly 0 becomes NaN so validate_chain accepts the frame.
    """
    today = today_et or _today_et()
    rows: list[dict[str, Any]] = []

    for side, frames in (("CALL", calls), ("PUT", puts)):
        for df in frames:
            if df is None or not hasattr(df, "columns") or df.empty:
                continue
            cols = [
                "strike", "lastPrice", "volume", "openInterest",
                "impliedVolatility",
            ]
            for opt_col in ("bid", "ask"):
                if opt_col in df.columns:
                    cols.append(opt_col)
            if "expiry" not in df.columns:
                continue
            tmp = df[[c for c in cols if c in df.columns] + ["expiry"]].copy()
            for opt_col in ("bid", "ask"):
                if opt_col not in tmp.columns:
                    tmp[opt_col] = 0.0
            tmp["bid"] = tmp["bid"].fillna(0)
            tmp["ask"] = tmp["ask"].fillna(0)
            tmp["volume"] = tmp["volume"].fillna(0)
            tmp["openInterest"] = tmp["openInterest"].fillna(0)

            for _, r in tmp.iterrows():
                expiry = str(r["expiry"])[:10]
                try:
                    dte = (
                        datetime.strptime(expiry, "%Y-%m-%d").date() - today
                    ).days
                except ValueError:
                    continue
                iv_raw = r.get("impliedVolatility")
                try:
                    iv = float(iv_raw) if iv_raw is not None else float("nan")
                except (TypeError, ValueError):
                    iv = float("nan")
                if iv == 0.0:
                    iv = float("nan")
                rows.append({
                    "side": side,
                    "strike": float(r.get("strike") or 0),
                    "expiry": expiry,
                    "dte": float(dte),
                    "bid": float(r.get("bid") or 0),
                    "ask": float(r.get("ask") or 0),
                    "last": float(r.get("lastPrice") or 0),
                    "volume": float(r.get("volume") or 0),
                    "openInterest": float(r.get("openInterest") or 0),
                    "iv": iv,
                    "delta": float("nan"),
                })

    if not rows:
        return validate_chain(pd.DataFrame(columns=CHAIN_COLUMNS))
    return validate_chain(pd.DataFrame(rows, columns=CHAIN_COLUMNS))


class YahooSource:
    """yfinance-backed MarketDataSource. volume may carry prior session."""

    name = "yahoo"
    volume_is_session_scoped = False
    provides_quotes = True

    def fetch_chain(self, ticker: str, *, max_dte: int) -> pd.DataFrame:
        t = yf.Ticker(ticker)
        today = _today_et()
        try:
            expiries = list(
                _yf_retry(lambda: t.options or [], label="options list") or []
            )
        except Exception as last_err:
            raise ValueError(
                f"{ticker} options list unavailable from Yahoo "
                f"({type(last_err).__name__}: {last_err}). "
                "Usually a temporary rate-limit or empty response - wait ~30s "
                "and retry."
            ) from last_err
        if not expiries:
            raise ValueError(
                f"{ticker} has no listed option expiries. "
                "Indices (^VIX, ^SPX) are not supported - use an equity/ETF "
                "with options."
            )

        # Cap by max_dte (ET calendar days); keep order from Yahoo.
        kept: list[str] = []
        for expiry in expiries:
            try:
                dte = (
                    datetime.strptime(expiry, "%Y-%m-%d").date() - today
                ).days
            except ValueError:
                continue
            if 0 <= dte <= int(max_dte):
                kept.append(expiry)

        all_calls: list[pd.DataFrame] = []
        all_puts: list[pd.DataFrame] = []
        for expiry in kept:
            try:
                chain = _yf_retry(
                    lambda e=expiry: t.option_chain(e),
                    label=f"chain {expiry}",
                    attempts=3,
                    base_sleep=2.0,
                )
            except Exception:
                continue
            for side_df, bucket in (
                (chain.calls, all_calls),
                (chain.puts, all_puts),
            ):
                if side_df is None or not hasattr(side_df, "columns") or side_df.empty:
                    continue
                tagged = side_df.copy()
                tagged["expiry"] = expiry
                bucket.append(tagged)

        if not all_calls or not all_puts:
            raise ValueError(
                f"{ticker} has no options chain data. "
                "Indices (^VIX, ^SPX) are not supported - use an ETF with "
                "options instead (e.g. SPY, QQQ, UVXY for volatility exposure)."
            )
        return chain_frame_from_yahoo_legs(
            all_calls, all_puts, today_et=today,
        )

    def fetch_history(
        self, ticker: str, *, interval: str, period: str
    ) -> pd.DataFrame:
        t = yf.Ticker(ticker)
        return _clean_history(
            _yf_retry(
                lambda: t.history(period=period, interval=interval),
                label=f"{interval} history",
            )
        )

    def fetch_spot(self, ticker: str) -> float | None:
        try:
            hist = self.fetch_history(ticker, interval="1d", period="5d")
            if hist is None or hist.empty or "Close" not in hist.columns:
                return None
            spot = float(hist["Close"].iloc[-1])
            return spot if spot == spot and spot > 0 else None
        except Exception:
            return None

    def fetch_option_mid(
        self,
        ticker: str,
        side: str,
        strike: float,
        expiry: str,
    ) -> float | None:
        """
        Best-effort live mid. Never returns 0.0 as a stand-in.

        Raises ValueError for permanent failures (unknown expiry / strike).
        Returns None only for transient/empty-quote cases that may succeed later.
        """
        t = yf.Ticker(ticker)
        try:
            chain = _yf_retry(
                lambda: t.option_chain(expiry),
                label=f"mid {expiry}",
                attempts=3,
                base_sleep=2.0,
            )
        except ValueError:
            raise
        except Exception:
            return None
        book = chain.calls if str(side).upper() == "CALL" else chain.puts
        row = book[abs(book["strike"] - float(strike)) < 1e-6]
        if row.empty:
            raise ValueError(
                f"strike not found: {ticker} {side} {strike} {expiry}"
            )
        r = row.iloc[0]
        bid = float(r.get("bid") or 0)
        ask = float(r.get("ask") or 0)
        last = float(r.get("lastPrice") or 0)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            return mid if mid > 0 else None
        if last > 0:
            return last
        return None
