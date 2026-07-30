"""Strike window + source-aware quality gate (Massive Starter)."""

from __future__ import annotations

import json
import logging
import math
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from chain_quality import (
    chain_fails_quality_gate,
    contract_is_usable,
)
from sources.massive import (
    MassiveChainTruncatedError,
    MassiveSource,
    map_snapshot_results_to_chain,
)


def test_strike_window_sent_server_side():
    src = MassiveSource(api_key="test-key-not-real", strike_window_pct=0.06)
    captured: dict = {}

    def fake_request(path_or_url, params=None, **_k):
        captured["params"] = dict(params or {})
        return {"status": "OK", "results": [], "next_url": None}

    with patch.object(src, "fetch_spot", return_value=340.0):
        with patch.object(src, "_request", side_effect=fake_request):
            src.fetch_chain("AAPL", max_dte=45)
    assert captured["params"]["strike_price.gte"] == pytest.approx(340.0 * 0.94)
    assert captured["params"]["strike_price.lte"] == pytest.approx(340.0 * 1.06)
    assert "expiration_date.gte" in captured["params"]
    assert "expiration_date.lte" in captured["params"]


def test_no_pagination_cap_hit_with_window():
    """Windowed fetch completes in one page; no truncation raise."""
    src = MassiveSource(api_key="test-key-not-real", max_pages=2, strike_window_pct=0.06)
    calls = {"n": 0}

    def one_page(path_or_url, params=None, **_k):
        calls["n"] += 1
        assert "strike_price.gte" in (params or {})
        return {
            "status": "OK",
            "results": [
                {
                    "details": {
                        "ticker": "O:AAPL260821C00340000",
                        "contract_type": "call",
                        "strike_price": 340,
                        "expiration_date": "2026-08-21",
                    },
                    "day": {"volume": 10, "close": 2.0},
                    "greeks": {"delta": 0.5},
                    "implied_volatility": 0.25,
                    "open_interest": 100,
                }
            ],
            "next_url": None,
        }

    with patch.object(src, "fetch_spot", return_value=340.0):
        with patch.object(src, "_request", side_effect=one_page):
            df = src.fetch_chain("AAPL", max_dte=45)
    assert not df.empty
    assert src.last_chain_pages == 1
    assert src.last_chain_used_strike_window is True
    assert calls["n"] == 1


def test_missing_spot_falls_back_to_full_chain_and_logs(caplog):
    src = MassiveSource(api_key="test-key-not-real")
    captured: dict = {}

    def fake_request(path_or_url, params=None, **_k):
        captured["params"] = dict(params or {})
        return {"status": "OK", "results": [], "next_url": None}

    with caplog.at_level(logging.WARNING, logger="sources.massive"):
        with patch.object(src, "fetch_spot", return_value=None):
            with patch.object(src, "_request", side_effect=fake_request):
                src.fetch_chain("AAPL", max_dte=45)
    assert "strike_price.gte" not in captured["params"]
    assert "strike_price.lte" not in captured["params"]
    assert src.last_chain_used_strike_window is False
    assert any("falling back to full chain" in r.message for r in caplog.records)


def test_massive_contracts_pass_gate_without_quotes():
    vol = {
        "top_calls": [
            {
                "strike": 340.0,
                "expiry": "2026-08-01",
                "dte": 3,
                "bid": float("nan"),
                "ask": float("nan"),
                "lastPrice": 4.15,
                "impliedVolatility": 0.30,
                "volume": 100,
            }
            for _ in range(10)
        ],
        "top_puts": [],
    }
    fails, detail = chain_fails_quality_gate(vol, provides_quotes=False)
    assert fails is False
    assert detail["provides_quotes"] is False
    assert contract_is_usable(
        bid=float("nan"), ask=float("nan"), iv=0.3, dte=3,
        provides_quotes=False, last=4.15,
    ) is True


def test_yahoo_still_requires_bid_ask():
    assert contract_is_usable(
        bid=float("nan"), ask=float("nan"), iv=0.3, dte=3,
        provides_quotes=True, last=4.15,
    ) is False
    assert contract_is_usable(
        bid=1.0, ask=1.1, iv=0.3, dte=3, provides_quotes=True, last=4.15,
    ) is True
    vol = {
        "top_calls": [
            {
                "strike": 340.0,
                "expiry": "2026-08-01",
                "dte": 3,
                "bid": 0,
                "ask": 0,
                "lastPrice": 4.15,
                "impliedVolatility": 0.30,
                "volume": 100,
            }
            for _ in range(10)
        ],
    }
    fails, _ = chain_fails_quality_gate(vol, provides_quotes=True)
    assert fails is True


def test_bid_ask_never_synthesised_from_close():
    # Inline minimal payload: no last_quote, has day.close
    results = [
        {
            "details": {
                "contract_type": "call",
                "expiration_date": "2026-08-21",
                "strike_price": 340,
                "ticker": "O:AAPL260821C00340000",
            },
            "day": {"volume": 100, "close": 4.15},
            "greeks": {},
            "implied_volatility": 0.28,
            "open_interest": 50,
        }
    ]
    df = map_snapshot_results_to_chain(
        results, today_et=date(2026, 7, 28), max_dte=45,
    )
    row = df.iloc[0]
    assert math.isnan(float(row["bid"]))
    assert math.isnan(float(row["ask"]))
    assert float(row["last"]) == pytest.approx(4.15)
    # Must not invent bid/ask from close
    assert float(row["bid"]) != pytest.approx(4.15)


def test_archive_records_quote_source(tmp_path, monkeypatch):
    import dailyScaner as ds

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ds, "TICKER", "AAPL")
    calls = pd.DataFrame(
        [
            {
                "strike": 200.0,
                "expiry": "2026-08-01",
                "dte": 3,
                "lastPrice": 1.0,
                "bid": float("nan"),
                "ask": float("nan"),
                "volume": 100,
                "openInterest": 50,
                "impliedVolatility": 0.3,
            }
        ]
    )
    puts = calls.copy()
    fname, _ = ds.save_archive(
        200.0, {}, calls, puts,
        source_name="massive",
        quote_source="daily_bar",
    )
    payload = json.loads(Path(fname).read_text(encoding="utf-8"))
    assert payload["source"] == "massive"
    assert payload["quote_source"] == "daily_bar"
