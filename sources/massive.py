"""
sources.massive — Massive.com (Polygon-compatible) MarketDataSource.

Auth: MASSIVE_API_KEY from the environment only. Never hardcode, never log,
never write the key into archives.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from sources.base import CHAIN_COLUMNS, validate_chain

ET = ZoneInfo("America/New_York")
log = logging.getLogger("sources.massive")

REST_BASE = "https://api.massive.com"


class MassivePlanError(RuntimeError):
    """Raised on HTTP 403 — plan does not cover the endpoint. Do not retry."""


class MassiveChainTruncatedError(RuntimeError):
    """
    Raised when pagination hits massive_max_pages with more results remaining.

    Chosen (a): abort rather than score a silent partial chain — truncated
    totals and top-30 rankings are worse than skipping the scan.
    """


def _today_et() -> date:
    return datetime.now(ET).date()


def ns_utc_to_et(ts_ns: Any) -> datetime | None:
    """
    Convert Massive UNIX nanosecond UTC timestamps to America/New_York.

    Values are ~1e18 (ns), not seconds — dividing by 1e9 twice would be wrong.
    """
    try:
        if ts_ns is None:
            return None
        ns = int(ts_ns)
        if ns <= 0:
            return None
        # Accept seconds-scale accidentally (rare); prefer ns.
        if ns < 10_000_000_000:  # before year ~2286 in seconds
            secs = float(ns)
        else:
            secs = ns / 1_000_000_000.0
        return datetime.fromtimestamp(secs, tz=ZoneInfo("UTC")).astimezone(ET)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _as_float(v: Any, *, zero_to_nan: bool = False) -> float:
    try:
        if v is None:
            return float("nan")
        f = float(v)
        if f != f:
            return float("nan")
        if zero_to_nan and f == 0.0:
            return float("nan")
        return f
    except (TypeError, ValueError):
        return float("nan")


def map_snapshot_results_to_chain(
    results: list[dict[str, Any]],
    *,
    today_et: date | None = None,
    max_dte: int | None = None,
) -> pd.DataFrame:
    """Map Massive options snapshot ``results`` into CHAIN_COLUMNS."""
    today = today_et or _today_et()
    rows: list[dict[str, Any]] = []
    for item in results or []:
        details = item.get("details") or {}
        day = item.get("day") or {}
        quote = item.get("last_quote") or {}
        greeks = item.get("greeks") or {}
        if not isinstance(details, dict):
            continue
        ctype = str(details.get("contract_type") or "").strip().lower()
        if ctype not in ("call", "put"):
            continue
        expiry = str(details.get("expiration_date") or "")[:10]
        if not expiry:
            continue
        try:
            dte = float((date.fromisoformat(expiry) - today).days)
        except ValueError:
            continue
        if max_dte is not None and (dte < 0 or dte > float(max_dte)):
            continue

        iv = _as_float(item.get("implied_volatility"), zero_to_nan=True)
        delta = float("nan")
        if isinstance(greeks, dict) and greeks:
            delta = _as_float(greeks.get("delta"))

        # Absent last_quote → NaN bid/ask (never 0, never synthesised from close)
        if quote:
            bid = _as_float(quote.get("bid"), zero_to_nan=True)
            ask = _as_float(quote.get("ask"), zero_to_nan=True)
        else:
            bid = float("nan")
            ask = float("nan")

        rows.append({
            "side": ctype.upper(),
            "strike": _as_float(details.get("strike_price")),
            "expiry": expiry,
            "dte": dte,
            "bid": bid,
            "ask": ask,
            "last": _as_float(day.get("close")),
            "volume": _as_float(day.get("volume")),
            "openInterest": _as_float(item.get("open_interest")),
            "iv": iv,
            "delta": delta,
        })

    if not rows:
        return validate_chain(pd.DataFrame(columns=CHAIN_COLUMNS))
    return validate_chain(pd.DataFrame(rows, columns=CHAIN_COLUMNS))


def _cap_strikes_per_expiry(
    df: pd.DataFrame,
    *,
    spot: float,
    max_per_side: int,
) -> pd.DataFrame:
    """Keep at most *max_per_side* nearest-to-spot strikes per expiry×side."""
    if df.empty or max_per_side <= 0:
        return df
    parts: list[pd.DataFrame] = []
    for _, g in df.groupby(["expiry", "side"], sort=False):
        if len(g) <= max_per_side:
            parts.append(g)
            continue
        g2 = g.copy()
        g2["_dist"] = (g2["strike"].astype(float) - float(spot)).abs()
        parts.append(
            g2.nsmallest(int(max_per_side), "_dist").drop(columns=["_dist"])
        )
    if not parts:
        return df
    out = pd.concat(parts, ignore_index=True)
    return validate_chain(out[CHAIN_COLUMNS])


def _retry_sleep(attempt: int, *, base: float = 2.0) -> float:
    return base * (2 ** (attempt - 1))


class MassiveSource:
    name = "massive"
    volume_is_session_scoped = True
    provides_quotes = False  # Starter: day bar only; no NBBO entitlement

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float = 20.0,
        max_pages: int | None = None,
        strike_window_pct: float | None = None,
        max_strikes_per_expiry: int | None = None,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("MASSIVE_API_KEY", "")
        if not str(key).strip():
            raise ValueError("MASSIVE_API_KEY is required for MassiveSource")
        self._api_key = str(key).strip()
        self._timeout = float(timeout)
        from config import SCORING

        if max_pages is not None:
            self._max_pages = int(max_pages)
        else:
            self._max_pages = int(SCORING.get("massive_max_pages", 20))
        if strike_window_pct is not None:
            self._strike_window_pct = float(strike_window_pct)
        else:
            self._strike_window_pct = float(
                SCORING.get("massive_strike_window_pct", 0.06)
            )
        if max_strikes_per_expiry is not None:
            self._max_strikes_per_expiry = int(max_strikes_per_expiry)
        else:
            self._max_strikes_per_expiry = int(
                SCORING.get("massive_max_strikes_per_expiry", 20)
            )
        # Last request URL with key redacted — for tests / debugging
        self.last_request_url_redacted: str | None = None
        # Diagnostics from the most recent fetch_chain call
        self.last_chain_pages: int = 0
        self.last_chain_contracts: int = 0
        self.last_chain_used_strike_window: bool = False
        self.last_chain_spot: float | None = None
        # Per-ticker chain cache for short-horizon exit marks (one fetch / run).
        self._exit_chain_cache: dict[str, pd.DataFrame] = {}

    def _request(
        self,
        path_or_url: str,
        params: dict[str, Any] | None = None,
        *,
        attempts: int = 5,
    ) -> dict[str, Any]:
        if path_or_url.startswith("http"):
            # next_url from Massive may already include apiKey — strip & re-add
            parsed = urllib.parse.urlparse(path_or_url)
            q = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
            q.pop("apiKey", None)
            if params:
                q.update({k: str(v) for k, v in params.items() if v is not None})
            q["apiKey"] = self._api_key
            url = urllib.parse.urlunparse(
                parsed._replace(query=urllib.parse.urlencode(q))
            )
        else:
            q = dict(params or {})
            q["apiKey"] = self._api_key
            url = f"{REST_BASE}{path_or_url}?{urllib.parse.urlencode(q)}"

        redacted = url.replace(self._api_key, "***")
        self.last_request_url_redacted = redacted

        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    body = resp.read().decode("utf-8")
                    return json.loads(body)
            except urllib.error.HTTPError as exc:
                last_err = exc
                if exc.code == 403:
                    raise MassivePlanError(
                        "plan does not cover this endpoint"
                    ) from exc
                if exc.code == 429:
                    if attempt >= attempts:
                        break
                    wait = max(_retry_sleep(attempt), 15.0 * attempt)
                    log.warning(
                        "Massive 429 retry %s/%s after %.0fs (%s)",
                        attempt, attempts, wait, redacted,
                    )
                    time.sleep(wait)
                    continue
                raise
            except Exception as exc:
                last_err = exc
                if attempt >= attempts:
                    break
                wait = _retry_sleep(attempt)
                log.warning(
                    "Massive retry %s/%s after %.0fs (%s: %s)",
                    attempt, attempts, wait, type(exc).__name__, redacted,
                )
                time.sleep(wait)
        assert last_err is not None
        raise last_err

    def fetch_chain(self, ticker: str, *, max_dte: int) -> pd.DataFrame:
        today = _today_et()
        end = today + timedelta(days=int(max_dte))
        params: dict[str, Any] = {
            "expiration_date.lte": end.isoformat(),
            "expiration_date.gte": today.isoformat(),
            "limit": 250,
            "sort": "expiration_date",
            "order": "asc",
        }

        spot = self.fetch_spot(ticker)
        used_window = False
        if spot is not None and spot > 0 and spot == spot:
            pct = float(self._strike_window_pct)
            lo = float(spot) * (1.0 - pct)
            hi = float(spot) * (1.0 + pct)
            params["strike_price.gte"] = round(lo, 4)
            params["strike_price.lte"] = round(hi, 4)
            used_window = True
            self.last_chain_spot = float(spot)
        else:
            self.last_chain_spot = None
            log.warning(
                "Massive spot unavailable for %s — falling back to full chain "
                "(no strike_price window)",
                ticker,
            )

        path = f"/v3/snapshot/options/{urllib.parse.quote(ticker)}"
        all_results: list[dict[str, Any]] = []
        url: str | None = path
        pages = 0
        max_pages = int(self._max_pages)
        while url and pages < max_pages:
            pages += 1
            payload = self._request(
                url if pages > 1 else path,
                None if pages > 1 else params,
            )
            batch = list(payload.get("results") or [])
            all_results.extend(batch)
            nxt = payload.get("next_url")
            if not nxt or not batch:
                break
            url = str(nxt)
        else:
            # Loop exhausted on page cap with next_url still set → truncated.
            raise MassiveChainTruncatedError(
                f"Massive chain pagination hit {max_pages}-page cap for {ticker} "
                f"with more results remaining ({len(all_results)} contracts so far). "
                f"Raise SCORING['massive_max_pages'] or narrow the strike/expiry "
                f"filter; refusing to score a partial chain."
            )

        df = map_snapshot_results_to_chain(
            all_results, today_et=today, max_dte=max_dte,
        )
        if used_window and spot is not None and not df.empty:
            df = _cap_strikes_per_expiry(
                df, spot=float(spot), max_per_side=int(self._max_strikes_per_expiry),
            )

        self.last_chain_pages = pages
        self.last_chain_contracts = int(len(df))
        self.last_chain_used_strike_window = used_window
        return df

    def fetch_history(
        self, ticker: str, *, interval: str, period: str
    ) -> pd.DataFrame:
        """
        Massive aggregates → Yahoo-like OHLCV DataFrame.

        interval examples: 1m, 5m, 15m, 1h, 1d, 1wk
        period examples: 1d, 5d, 30d, 3mo, 6mo, 52wk
        """
        mult, span = _interval_to_agg(interval)
        start, end = _period_to_range(period)
        path = (
            f"/v2/aggs/ticker/{urllib.parse.quote(ticker)}/"
            f"range/{mult}/{span}/{start}/{end}"
        )
        payload = self._request(path, {"adjusted": "true", "sort": "asc", "limit": 50000})
        rows = []
        for bar in payload.get("results") or []:
            # Aggregates use millisecond Unix time in `t`
            ts = None
            if bar.get("t") is not None:
                try:
                    ms = int(bar["t"])
                    ts = datetime.fromtimestamp(
                        ms / 1000.0, tz=ZoneInfo("UTC")
                    ).astimezone(ET)
                except Exception:
                    ts = None
            if ts is None:
                continue
            rows.append({
                "Open": float(bar.get("o") or float("nan")),
                "High": float(bar.get("h") or float("nan")),
                "Low": float(bar.get("l") or float("nan")),
                "Close": float(bar.get("c") or float("nan")),
                "Volume": float(bar.get("v") or float("nan")),
                "_ts": ts.replace(tzinfo=None),
            })
        if not rows:
            return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"])
        df = pd.DataFrame(rows).set_index("_ts").sort_index()
        df.index.name = None
        return df[["Open", "High", "Low", "Close", "Volume"]]

    def fetch_spot(self, ticker: str) -> float | None:
        hist = self.fetch_history(ticker, interval="1d", period="5d")
        if hist is None or hist.empty or "Close" not in hist.columns:
            return None
        try:
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
        try:
            chain = self.fetch_chain(ticker, max_dte=3650)
            want = str(side).upper()
            exp = str(expiry)[:10]
            sub = chain[
                (chain["side"] == want)
                & (chain["expiry"].astype(str).str[:10] == exp)
                & ((chain["strike"] - float(strike)).abs() < 1e-6)
            ]
            if sub.empty:
                return None
            r = sub.iloc[0]
            bid, ask, last = float(r["bid"]), float(r["ask"]), float(r["last"])
            if bid == bid and ask == ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                return mid if mid > 0 else None
            if last == last and last > 0:
                return last
            return None
        except Exception:
            return None

    def _exit_chain(self, ticker: str) -> pd.DataFrame:
        """Cached near-term chain for exit marks — max_dte=7, once per ticker."""
        key = str(ticker).upper()
        cached = self._exit_chain_cache.get(key)
        if cached is not None:
            return cached
        chain = self.fetch_chain(ticker, max_dte=7)
        self._exit_chain_cache[key] = chain
        return chain

    def fetch_option_exit(
        self,
        ticker: str,
        side: str,
        strike: float,
        expiry: str,
    ) -> tuple[float | None, str | None]:
        """Live BID (quote) or last trade — never mid. For t15m/t30m exits."""
        try:
            chain = self._exit_chain(ticker)
            want = str(side).upper()
            exp = str(expiry)[:10]
            sub = chain[
                (chain["side"] == want)
                & (chain["expiry"].astype(str).str[:10] == exp)
                & ((chain["strike"] - float(strike)).abs() < 1e-6)
            ]
            if sub.empty:
                raise ValueError(
                    f"strike not found: {ticker} {side} {strike} {expiry}"
                )
            r = sub.iloc[0]
            bid, last = float(r["bid"]), float(r["last"])
            if bid == bid and bid > 0:
                return bid, "quote"
            if last == last and last > 0:
                return last, "trade"
            return None, None
        except ValueError:
            raise
        except Exception as exc:
            log.warning(
                "fetch_option_exit failed %s %s %s %s: %s",
                ticker, side, strike, expiry, exc,
            )
            return None, None


def _interval_to_agg(interval: str) -> tuple[int, str]:
    iv = (interval or "1d").strip().lower()
    mapping = {
        "1m": (1, "minute"),
        "2m": (2, "minute"),
        "5m": (5, "minute"),
        "15m": (15, "minute"),
        "30m": (30, "minute"),
        "60m": (1, "hour"),
        "1h": (1, "hour"),
        "1d": (1, "day"),
        "1wk": (1, "week"),
        "1w": (1, "week"),
    }
    if iv not in mapping:
        raise ValueError(f"unsupported interval for Massive aggregates: {interval!r}")
    return mapping[iv]


def _period_to_range(period: str) -> tuple[str, str]:
    """Return (from, to) as YYYY-MM-DD in ET."""
    end = _today_et()
    p = (period or "1mo").strip().lower()
    days = 30
    if p.endswith("d") and p[:-1].isdigit():
        days = int(p[:-1])
    elif p.endswith("mo") and p[:-2].isdigit():
        days = int(p[:-2]) * 30
    elif p.endswith("wk") and p[:-2].isdigit():
        days = int(p[:-2]) * 7
    elif p.endswith("w") and p[:-1].isdigit():
        days = int(p[:-1]) * 7
    elif p.endswith("y") and p[:-1].isdigit():
        days = int(p[:-1]) * 365
    elif p in {"ytd"}:
        start = date(end.year, 1, 1)
        return start.isoformat(), end.isoformat()
    start = end - timedelta(days=max(days, 1))
    return start.isoformat(), end.isoformat()
