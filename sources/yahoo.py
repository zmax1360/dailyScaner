"""
sources.yahoo — yfinance MarketDataSource (parallel implementation).

MOVE of existing fetch logic from dailyScaner / data_adapter / attribution.
Callers are NOT rewired in this step — this module is additive only.

Preserved intentionally (do not "fix" here):
  - bid/ask ``fillna(0)`` (chain_quality owns usability)
  - volume / openInterest ``fillna(0)`` as in dailyScaner.fetch_data

Rate-limit path in ``_yf_retry`` is intentionally fail-fast (see constants):
long 15s/30s sleeps starve mark_runner's 600s budget across hundreds of contracts.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

from sources.base import CHAIN_COLUMNS, validate_chain

ET = ZoneInfo("America/New_York")
log = logging.getLogger("sources.yahoo")

# Transient network errors: keep brief exponential backoff.
# Rate limits: one short retry then fail — mark_runner seals/skips and moves on.
# Old path slept 15s+30s per contract and burned the 600s runtime cap.
_YF_RATE_LIMIT_ATTEMPTS = 2       # initial try + 1 retry
_YF_RATE_LIMIT_SLEEP_SEC = 1.0    # single backoff; never 15s/30s


def _is_rate_limit_error(exc: BaseException) -> bool:
    name = type(exc).__name__
    msg = str(exc)
    return (
        name == "YFRateLimitError"
        or "Too Many Requests" in msg
        or "Rate limited" in msg
    )


def _yf_retry(fn, *, label: str, attempts: int = 5, base_sleep: float = 3.0):
    """Call Yahoo via yfinance with backoff on rate limits / transient errors.

    ValueError (e.g. expiry/strike not found) is permanent — never retried.
    YFRateLimitError: at most ``_YF_RATE_LIMIT_ATTEMPTS`` tries with a short
    sleep — prefer fail-fast over burning the mark_runner runtime budget.
    Other transient errors keep the caller-supplied attempts / exponential sleep.
    """
    last_err = None
    rate_limit_tries = 0
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except ValueError:
            raise
        except Exception as e:
            last_err = e
            name = type(e).__name__
            rate_limited = _is_rate_limit_error(e)
            if rate_limited:
                rate_limit_tries += 1
                if rate_limit_tries >= _YF_RATE_LIMIT_ATTEMPTS:
                    break
                wait = _YF_RATE_LIMIT_SLEEP_SEC
            else:
                if attempt >= attempts:
                    break
                wait = base_sleep * (2 ** (attempt - 1))
            log.warning(
                "Yahoo %s retry %d/%d after %.0fs (%s)",
                label, attempt, attempts, wait, name,
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


# Sentinel: prior fetch for (ticker, expiry) failed this pass — do not re-hit.
_CHAIN_FAILED = object()


class YahooSource:
    """yfinance-backed MarketDataSource. volume may carry prior session.

    Instance-scoped ``_option_chain_cache`` collapses per-contract
    ``option_chain`` HTTP calls to one fetch per (ticker, expiry) for the
    lifetime of this object. Construct once per mark_runner pass (see
    mark_runner.main) — never reuse across passes or process-global.
    """

    name = "yahoo"
    volume_is_session_scoped = False
    provides_quotes = True

    def __init__(self) -> None:
        # Mirrors MassiveSource._exit_chain_cache: per-instance, not persisted.
        self._option_chain_cache: dict[tuple[str, str], Any] = {}
        # Diagnostic counter — HTTP option_chain fetches this instance made.
        self.option_chain_fetches: int = 0

    def _option_chain(self, ticker: str, expiry: str):
        """Cached yf.Ticker.option_chain — one HTTP call per (ticker, expiry)."""
        key = (str(ticker).upper(), str(expiry)[:10])
        cached = self._option_chain_cache.get(key)
        if cached is _CHAIN_FAILED:
            raise RuntimeError(
                f"option_chain failed earlier this pass for {key[0]} {key[1]}"
            )
        if cached is not None:
            return cached
        t = yf.Ticker(ticker)
        try:
            chain = _yf_retry(
                lambda: t.option_chain(key[1]),
                label=f"chain {key[1]}",
                attempts=3,
                base_sleep=2.0,
            )
        except Exception as exc:
            # Rate-limit: one burn per key this pass (not once per contract).
            if _is_rate_limit_error(exc):
                self._option_chain_cache[key] = _CHAIN_FAILED
            raise
        self._option_chain_cache[key] = chain
        self.option_chain_fetches += 1
        return chain

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
                chain = self._option_chain(ticker, expiry)
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
        try:
            chain = self._option_chain(ticker, expiry)
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

    def fetch_option_exit(
        self,
        ticker: str,
        side: str,
        strike: float,
        expiry: str,
        *,
        now_et: datetime | None = None,
        trade_max_age: timedelta | None = None,
    ) -> tuple[float | None, str | None]:
        """
        Live BID (quote) or fresh last trade — never mid. For t15m/t30m exits.

        lastPrice is only accepted when lastTradeDate is within trade_max_age
        of mark time (default 5 minutes). Stale last → (None, None).
        """
        max_age = trade_max_age if trade_max_age is not None else timedelta(minutes=5)
        mark_now = now_et or datetime.now(ET)
        if mark_now.tzinfo is None:
            mark_now = mark_now.replace(tzinfo=ET)
        mark_now = mark_now.astimezone(ET)

        try:
            chain = self._option_chain(ticker, expiry)
        except ValueError:
            raise
        except Exception as exc:
            log.warning(
                "fetch_option_exit failed %s %s %s %s: %s",
                ticker, side, strike, expiry, exc,
            )
            return None, None
        book = chain.calls if str(side).upper() == "CALL" else chain.puts
        row = book[abs(book["strike"] - float(strike)) < 1e-6]
        if row.empty:
            raise ValueError(
                f"strike not found: {ticker} {side} {strike} {expiry}"
            )
        r = row.iloc[0]
        bid = float(r.get("bid") or 0)
        last = float(r.get("lastPrice") or 0)
        if bid > 0:
            return bid, "quote"
        if last > 0 and _yahoo_last_trade_is_fresh(r, mark_now=mark_now, max_age=max_age):
            return last, "trade"
        return None, None


def _yahoo_last_trade_is_fresh(
    row,
    *,
    mark_now: datetime,
    max_age: timedelta,
) -> bool:
    """True when lastTradeDate is present and within max_age of mark_now (ET)."""
    raw = None
    for key in ("lastTradeDate", "lastTradeDateUtc", "lastTrade"):
        if hasattr(row, "index") and key in row.index:
            raw = row[key]
            break
        if isinstance(row, dict) and key in row:
            raw = row[key]
            break
    if raw is None or (isinstance(raw, float) and raw != raw):
        return False
    try:
        ts = pd.Timestamp(raw)
    except (TypeError, ValueError):
        return False
    if pd.isna(ts):
        return False
    if ts.tzinfo is None:
        # yfinance often emits naive UTC or exchange-local; treat as UTC then ET.
        traded = ts.to_pydatetime().replace(tzinfo=ZoneInfo("UTC")).astimezone(ET)
    else:
        traded = ts.to_pydatetime().astimezone(ET)
    age = mark_now - traded
    return timedelta(0) <= age <= max_age
