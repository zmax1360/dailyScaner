"""Step 2 — sources.yahoo parallel MarketDataSource (no live Yahoo in tests)."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from sources.base import CHAIN_COLUMNS, MarketDataSource, validate_chain
from sources.yahoo import YahooSource, chain_frame_from_yahoo_legs

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
