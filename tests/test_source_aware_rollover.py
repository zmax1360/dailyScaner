"""CURSOR_SOURCE_AWARE_ROLLOVER — source-tagged archives + Massive page cap."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from best_value import attach_dvol
from chain_quality import (
    archive_source_name,
    chain_volume_rolled_over,
    should_apply_chain_rollover_check,
)
from sources.massive import MassiveChainTruncatedError, MassiveSource

ET = ZoneInfo("America/New_York")


def test_missing_source_key_treated_as_yahoo():
    assert archive_source_name({}) == "yahoo"
    assert archive_source_name({"spot": 1}) == "yahoo"
    assert archive_source_name({"source": None}) == "yahoo"
    assert archive_source_name({"source": "  "}) == "yahoo"
    assert archive_source_name({"source": "Massive"}) == "massive"


def test_rollover_check_skipped_on_source_mismatch(caplog):
    today = date(2026, 7, 29)
    prev = {
        "timestamp": "2026-07-29T19:58:51-04:00",
        "source": "yahoo",
        "volume": {"total_call_vol": 944496, "total_put_vol": 789904},
    }
    apply, why = should_apply_chain_rollover_check(prev, "massive", today)
    assert apply is False
    assert why.startswith("source_mismatch")
    # Lower curr would abort if applied:
    assert chain_volume_rolled_over(944496, 789904, 284582, 326120) is True
    with caplog.at_level(logging.WARNING, logger="dailyScaner"):
        logging.getLogger("dailyScaner").warning(
            "previous archive written by source yahoo, current source is "
            "massive — rollover check skipped"
        )
    assert any("rollover check skipped" in r.message for r in caplog.records)


def test_rollover_check_applied_when_source_matches():
    today = date(2026, 7, 29)
    prev = {
        "timestamp": "2026-07-29T10:00:00-04:00",
        "source": "yahoo",
        "volume": {"total_call_vol": 500_000, "total_put_vol": 400_000},
    }
    apply, why = should_apply_chain_rollover_check(prev, "yahoo", today)
    assert apply is True
    assert why == "ok"
    assert chain_volume_rolled_over(500_000, 400_000, 100_000, 90_000) is True


def test_decrease_detector_skipped_across_sources():
    prev = {
        "top_calls": [
            {"strike": 200.0, "expiry": "2026-08-01", "volume": 10_000},
        ],
        "top_puts": [],
    }
    df = pd.DataFrame(
        [
            {
                "side": "CALL",
                "strike": 200.0,
                "expiry": "2026-08-01",
                "volume": 1_000,
            }
        ]
    )
    out = attach_dvol(
        df,
        prev,
        current_source="massive",
        prev_archive_source="yahoo",
    )
    assert out.attrs.get("rollover_detectors") == "skipped_source_mismatch"
    assert int(out.attrs.get("n_decrease_suspect", -1)) == 0
    assert not bool(out["dvol_suspect"].iloc[0])


def test_eod_stale_detector_skipped_across_sources():
    eod = {("CALL", 200.0, "2026-08-01"): 9_000}
    df = pd.DataFrame(
        [
            {
                "side": "CALL",
                "strike": 200.0,
                "expiry": "2026-08-01",
                "volume": 9_500,
            }
        ]
    )
    now = datetime(2026, 7, 29, 10, 0, tzinfo=ET)
    out = attach_dvol(
        df,
        None,
        eod_vol_lookup=eod,
        now_et=now,
        current_source="massive",
        eod_archive_source="yahoo",
    )
    assert out.attrs.get("rollover_detectors") == "skipped_source_mismatch"
    assert int(out.attrs.get("n_eod_stale", -1)) == 0
    assert not bool(out["stale_volume"].iloc[0])


def test_archive_payload_includes_source(tmp_path, monkeypatch):
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
                "bid": 0.9,
                "ask": 1.1,
                "volume": 100,
                "openInterest": 50,
                "impliedVolatility": 0.3,
            }
        ]
    )
    puts = calls.copy()
    fname, _ = ds.save_archive(
        200.0,
        {},
        calls,
        puts,
        source_name="massive",
    )
    payload = json.loads(Path(fname).read_text(encoding="utf-8"))
    assert payload["source"] == "massive"


def test_page_cap_hit_does_not_return_partial_chain_silently():
    src = MassiveSource(api_key="test-key-not-real", max_pages=2)
    n = {"i": 0}

    def forever_next(path_or_url, params=None, **_k):
        n["i"] += 1
        return {
            "status": "OK",
            "results": [{"details": {"ticker": "O:AAPL200C00100000",
                                     "contract_type": "call",
                                     "strike_price": 100,
                                     "expiration_date": "2026-08-21"},
                         "day": {"volume": 1},
                         "greeks": {},
                         "last_quote": {"bid": 1, "ask": 1.1}}],
            "next_url": f"https://api.massive.com/next?page={n['i']}",
        }

    with patch.object(src, "_request", side_effect=forever_next):
        with pytest.raises(MassiveChainTruncatedError, match="page cap"):
            src.fetch_chain("AAPL", max_dte=45)


def test_expiration_date_gte_sent_server_side():
    src = MassiveSource(api_key="test-key-not-real")
    captured: dict = {}

    def fake_request(path_or_url, params=None, **_k):
        captured["params"] = dict(params or {})
        return {"status": "OK", "results": [], "next_url": None}

    with patch.object(src, "_request", side_effect=fake_request):
        src.fetch_chain("AAPL", max_dte=45)
    assert "expiration_date.gte" in captured["params"]
    assert "expiration_date.lte" in captured["params"]
    assert captured["params"]["limit"] == 250
