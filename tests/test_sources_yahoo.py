"""Step 2 — sources.yahoo parallel MarketDataSource (no live Yahoo in tests)."""

from __future__ import annotations

import json
import math
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from sources.base import CHAIN_COLUMNS, MarketDataSource, validate_chain
from sources.yahoo import (
    YahooSource,
    _YF_RATE_LIMIT_ATTEMPTS,
    _YF_RATE_LIMIT_SLEEP_SEC,
    _yf_retry,
    chain_frame_from_yahoo_legs,
)

GOLDEN = Path(__file__).resolve().parent / "golden"


def _leg_from_archive_rows(rows: list[dict], side_label: str) -> pd.DataFrame:
    """Build a Yahoo-shaped option leg DataFrame from a golden archive volume block."""
    out = []
    for r in rows:
        out.append({
            "strike": float(r["strike"]),
            "lastPrice": float(r.get("lastPrice") or r.get("last") or 0),
            "volume": float(r.get("volume") or 0),
            "openInterest": float(r.get("openInterest") or 0),
            "impliedVolatility": float(r.get("impliedVolatility") or r.get("iv") or 0.3),
            "bid": float(r.get("bid") or 0),
            "ask": float(r.get("ask") or 0),
            "expiry": str(r["expiry"])[:10],
        })
    return pd.DataFrame(out)


def test_yahoo_source_satisfies_protocol():
    src = YahooSource()
    assert isinstance(src, MarketDataSource)
    assert src.name == "yahoo"
    assert src.volume_is_session_scoped is False
    assert src.provides_quotes is True
    assert callable(src.fetch_chain)
    assert callable(src.fetch_history)
    assert callable(src.fetch_spot)
    assert callable(src.fetch_option_mid)


def test_yahoo_chain_passes_validate_chain():
    payload = json.loads((GOLDEN / "AAPL_20260728_093102.json").read_text())
    vol = payload["volume"]
    calls = _leg_from_archive_rows(vol.get("top_calls") or [], "CALL")
    puts = _leg_from_archive_rows(vol.get("top_puts") or [], "PUT")
    # Archive rows already have expiry column on the synthetic leg
    df = chain_frame_from_yahoo_legs(
        [calls], [puts], today_et=date(2026, 7, 28),
    )
    out = validate_chain(df)
    assert list(out.columns) == CHAIN_COLUMNS
    assert not out.empty
    assert set(out["side"].unique()) <= {"CALL", "PUT"}


def test_yahoo_emits_nan_delta_not_default():
    calls = pd.DataFrame([{
        "strike": 340.0,
        "lastPrice": 2.3,
        "volume": 100.0,
        "openInterest": 50.0,
        "impliedVolatility": 0.25,
        "bid": 2.2,
        "ask": 2.4,
        "expiry": "2026-08-21",
    }])
    puts = pd.DataFrame([{
        "strike": 340.0,
        "lastPrice": 2.1,
        "volume": 80.0,
        "openInterest": 40.0,
        "impliedVolatility": 0.25,
        "bid": 2.0,
        "ask": 2.2,
        "expiry": "2026-08-21",
    }])
    df = chain_frame_from_yahoo_legs(
        [calls], [puts], today_et=date(2026, 7, 28),
    )
    assert "delta" in df.columns
    d = float(df.loc[0, "delta"])
    assert math.isnan(d)
    assert d != 0.5


def _fake_option_chain(strike: float = 200.0):
    leg = pd.DataFrame([{
        "strike": strike,
        "lastPrice": 1.30,
        "volume": 10.0,
        "openInterest": 100.0,
        "impliedVolatility": 0.3,
        "bid": 1.25,
        "ask": 1.35,
        "lastTradeDate": pd.Timestamp.now(tz="UTC"),
    }])
    return SimpleNamespace(calls=leg.copy(), puts=leg.copy())


def test_yahoo_option_chain_cached_per_ticker_expiry(monkeypatch):
    """~500 contracts across few expiries → few HTTP option_chain calls."""
    src = YahooSource()
    calls: list[tuple[str, str]] = []

    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def option_chain(self, expiry):
            calls.append((str(self.ticker).upper(), str(expiry)[:10]))
            return _fake_option_chain()

    monkeypatch.setattr("sources.yahoo.yf.Ticker", FakeTicker)

    expiries = [f"2026-08-{d:02d}" for d in (10, 11, 14, 15, 21)]
    # Simulate a mark pass: 500 contracts, 2 tickers, 5 expiries shared.
    # Strike varies but chain HTTP is keyed only by (ticker, expiry).
    n_contracts = 0
    for ticker in ("AAPL", "NVDA"):
        for i in range(250):
            exp = expiries[i % len(expiries)]
            mid = src.fetch_option_mid(ticker, "CALL", 200.0, exp)
            exit_px, method = src.fetch_option_exit(ticker, "CALL", 200.0, exp)
            assert mid == pytest.approx(1.30)
            assert exit_px == pytest.approx(1.25) and method == "quote"
            n_contracts += 1

    assert n_contracts == 500
    # Before cache: 500 mid + 500 exit = 1000 HTTP calls.
    # After: one call per (ticker, expiry) = 2 * 5 = 10.
    assert len(calls) == 10
    assert src.option_chain_fetches == 10
    assert len(src._option_chain_cache) == 10


def test_yahoo_cache_is_per_instance_not_global(monkeypatch):
    class FakeTicker:
        def __init__(self, ticker):
            self.ticker = ticker

        def option_chain(self, expiry):
            return _fake_option_chain()

    monkeypatch.setattr("sources.yahoo.yf.Ticker", FakeTicker)
    a = YahooSource()
    b = YahooSource()
    a.fetch_option_mid("AAPL", "CALL", 200.0, "2026-08-15")
    assert a.option_chain_fetches == 1
    assert b.option_chain_fetches == 0
    b.fetch_option_mid("AAPL", "CALL", 200.0, "2026-08-15")
    assert b.option_chain_fetches == 1


def test_yf_retry_rate_limit_fail_fast(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(float(s)))

    class YFRateLimitError(Exception):
        pass

    n = {"i": 0}

    def boom():
        n["i"] += 1
        raise YFRateLimitError("Too Many Requests")

    with pytest.raises(YFRateLimitError):
        _yf_retry(boom, label="mid 2026-08-15", attempts=3, base_sleep=2.0)

    assert n["i"] == _YF_RATE_LIMIT_ATTEMPTS
    assert sleeps == [_YF_RATE_LIMIT_SLEEP_SEC]
    assert sum(sleeps) <= 2.0  # was 15+30 under the old backoff


def test_yf_retry_transient_keeps_exponential(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: sleeps.append(float(s)))

    n = {"i": 0}

    def boom():
        n["i"] += 1
        raise ConnectionError("reset")

    with pytest.raises(ConnectionError):
        _yf_retry(boom, label="mid x", attempts=3, base_sleep=2.0)

    assert n["i"] == 3
    assert sleeps == [2.0, 4.0]
