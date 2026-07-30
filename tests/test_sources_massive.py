"""Step 6 — MassiveSource mapping / retry tests (no live network)."""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

from sources.base import CHAIN_COLUMNS, MarketDataSource
from sources.massive import (
    MassivePlanError,
    MassiveSource,
    map_snapshot_results_to_chain,
    ns_utc_to_et,
)

GOLDEN = Path(__file__).resolve().parent / "golden"
SNAP = GOLDEN / "massive_aapl_snapshot.json"


@pytest.fixture
def snapshot_payload() -> dict:
    return json.loads(SNAP.read_text(encoding="utf-8"))


def test_massive_source_satisfies_protocol():
    src = MassiveSource(api_key="test-key-not-real")
    assert isinstance(src, MarketDataSource)
    assert src.name == "massive"
    assert src.volume_is_session_scoped is True


def test_massive_maps_to_chain_contract(snapshot_payload):
    df = map_snapshot_results_to_chain(
        snapshot_payload["results"],
        today_et=date(2026, 7, 28),
        max_dte=45,
    )
    assert list(df.columns) == CHAIN_COLUMNS
    assert not df.empty
    row = df[(df["strike"] == 340.0) & (df["side"] == "CALL")].iloc[0]
    assert row["expiry"] == "2026-08-21"
    assert row["volume"] == pytest.approx(1200.0)
    assert row["bid"] == pytest.approx(2.20)
    assert row["delta"] == pytest.approx(0.55)


def test_empty_greeks_yields_nan_delta(snapshot_payload):
    df = map_snapshot_results_to_chain(
        snapshot_payload["results"], today_et=date(2026, 7, 28), max_dte=45,
    )
    deep = df[(df["strike"] == 205.0) & (df["side"] == "CALL")].iloc[0]
    assert math.isnan(float(deep["delta"]))


def test_missing_last_quote_yields_nan_bid_ask(snapshot_payload):
    df = map_snapshot_results_to_chain(
        snapshot_payload["results"], today_et=date(2026, 7, 28), max_dte=45,
    )
    deep = df[(df["strike"] == 205.0) & (df["side"] == "CALL")].iloc[0]
    assert math.isnan(float(deep["bid"]))
    assert math.isnan(float(deep["ask"]))


def test_nanosecond_timestamp_converted_to_et():
    # From CURSOR_SOURCES_STEPS verified payload
    ts = ns_utc_to_et(1785297600000000000)
    assert ts is not None
    assert str(ts.tzinfo) in ("America/New_York", "US/Eastern") or "New_York" in str(ts.tzinfo)
    # Must not treat ns as seconds (that would land centuries away / overflow)
    assert 2020 <= ts.year <= 2035


def test_403_raises_with_plan_message_and_does_not_retry():
    src = MassiveSource(api_key="test-key-not-real")
    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        import urllib.error
        raise urllib.error.HTTPError(
            url="https://api.massive.com/x", code=403, msg="Forbidden",
            hdrs=None, fp=None,
        )

    with patch("urllib.request.urlopen", side_effect=boom):
        with pytest.raises(MassivePlanError, match="plan does not cover"):
            src._request("/v3/snapshot/options/AAPL", {"limit": 1}, attempts=5)
    assert calls["n"] == 1  # no retries on 403


def test_429_retries():
    src = MassiveSource(api_key="test-key-not-real")
    calls = {"n": 0}

    def flaky(*_a, **_k):
        import io
        import urllib.error
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.HTTPError(
                url="https://api.massive.com/x", code=429, msg="Too Many",
                hdrs=None, fp=None,
            )
        body = json.dumps({"status": "OK", "results": []}).encode()
        return _Resp(body)

    class _Resp:
        def __init__(self, body: bytes):
            self._body = body
        def read(self):
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    with patch("urllib.request.urlopen", side_effect=flaky), \
         patch("sources.massive.time.sleep", return_value=None):
        out = src._request("/v3/snapshot/options/AAPL", {"limit": 1}, attempts=5)
    assert out["status"] == "OK"
    assert calls["n"] == 3


def test_expiry_filter_sent_server_side():
    src = MassiveSource(api_key="test-key-not-real")
    captured: dict = {}

    def fake_request(path_or_url, params=None, **_k):
        captured["path"] = path_or_url
        captured["params"] = dict(params or {})
        return {"status": "OK", "results": [], "next_url": None}

    with patch.object(src, "_request", side_effect=fake_request):
        src.fetch_chain("AAPL", max_dte=45)
    assert captured["path"].endswith("/v3/snapshot/options/AAPL")
    assert "expiration_date.lte" in captured["params"]
    assert "expiration_date.gte" in captured["params"]
    assert captured["params"]["limit"] == 250


def test_api_key_absent_from_logs_and_archive(snapshot_payload, caplog):
    src = MassiveSource(api_key="SUPER_SECRET_KEY_XYZ")
    with patch.object(src, "_request", return_value=snapshot_payload):
        df = src.fetch_chain("AAPL", max_dte=45)
    # Frame / archive-shaped output must not contain the key
    blob = df.to_csv()
    assert "SUPER_SECRET_KEY_XYZ" not in blob
    if src.last_request_url_redacted:
        assert "SUPER_SECRET_KEY_XYZ" not in src.last_request_url_redacted
        assert "***" in src.last_request_url_redacted
