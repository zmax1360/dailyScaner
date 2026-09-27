"""15-min EMA 9/21/50 trend rule (ema_stack) — display only."""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import ema_stack as es

ET = ZoneInfo("America/New_York")


# ── the rule ─────────────────────────────────────────────────────────────────

def test_bullish_stack_allows_calls_only():
    r = es.classify(231.0, 230.0, 229.0)
    assert r["state"] == es.BULL and r["allowed"] == "CALL"
    assert "calls only" in r["headline"].lower()
    assert "231.00" in r["reason"] and "229.00" in r["reason"]


def test_bearish_stack_allows_puts_only():
    r = es.classify(229.0, 230.0, 231.0)
    assert r["state"] == es.BEAR and r["allowed"] == "PUT"
    assert "puts only" in r["headline"].lower()


@pytest.mark.parametrize("e9,e21,e50,why", [
    (231.0, 229.0, 230.0, "9 is above 21, and 21 is below 50"),   # fast above, middle dipped
    (229.0, 231.0, 230.0, "9 is below 21, and 21 is above 50"),
    (230.0, 230.0, 229.0, "9 is equal to 21"),                     # tie → no trade
    (230.0, 229.0, 229.0, "21 is equal to 50"),
])
def test_everything_else_is_no_trade_and_says_why(e9, e21, e50, why):
    r = es.classify(e9, e21, e50)
    assert r["state"] == es.NO_TRADE and r["allowed"] is None
    assert why in r["reason"]


@pytest.mark.parametrize("vals", [
    (None, 230.0, 229.0), (231.0, float("nan"), 229.0), (231.0, 230.0, "bad"), (None, None, None),
])
def test_missing_data_is_unknown_not_a_signal(vals):
    r = es.classify(*vals)
    assert r["state"] == es.UNKNOWN and r["allowed"] is None


# ── the maths ────────────────────────────────────────────────────────────────

def _reference_ema(values: list[float], span: int) -> float:
    alpha = 2.0 / (span + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = alpha * v + (1 - alpha) * ema
    return ema


def test_compute_emas_matches_reference_recursion():
    rng = np.random.default_rng(7)
    closes = list(230 + np.cumsum(rng.normal(0, 0.4, 130)))  # ~5 sessions of 15-min bars
    got = es.compute_emas(pd.Series(closes))
    for p in es.PERIODS:
        assert math.isclose(got[f"ema{p}"], _reference_ema(closes, p), abs_tol=1e-3)


def test_compute_emas_returns_none_below_period():
    got = es.compute_emas(pd.Series([230.0 + i * 0.1 for i in range(30)]))
    assert got["ema9"] is not None and got["ema21"] is not None
    assert got["ema50"] is None  # not enough bars: never guess


def test_rising_series_is_bullish_and_falling_is_bearish():
    up = es.compute_emas(pd.Series(np.linspace(220, 240, 130)))
    down = es.compute_emas(pd.Series(np.linspace(240, 220, 130)))
    assert es.classify(**up)["state"] == es.BULL
    assert es.classify(**down)["state"] == es.BEAR


# ── banner ───────────────────────────────────────────────────────────────────

def _payload(ts: datetime, **emas):
    return {"timestamp": ts.isoformat(), "timeframes": {"15M": {"price": 230.0, **emas}}}


def test_banner_reads_latest_archive_and_flags_stale_scans():
    now = datetime(2026, 9, 28, 11, 0, tzinfo=ET)
    fresh = es.banner_for_archive(_payload(now - timedelta(minutes=10), ema9=231, ema21=230, ema50=229), now=now)
    assert fresh["state"] == es.BULL and fresh["stale"] is False and fresh["as_of"] is not None
    stale = es.banner_for_archive(_payload(now - timedelta(minutes=45), ema9=231, ema21=230, ema50=229), now=now)
    assert stale["stale"] is True


def test_banner_on_old_archive_without_emas_is_unknown():
    assert es.banner_for_archive({"timeframes": {"15M": {"price": 230.0}}})["state"] == es.UNKNOWN
    assert es.banner_for_archive(None)["state"] == es.UNKNOWN


# ── scanner integration ──────────────────────────────────────────────────────

def _bars(n: int, start: float, step: float) -> pd.DataFrame:
    c = start + step * np.arange(n)
    return pd.DataFrame({"Open": c, "High": c + 0.2, "Low": c - 0.2, "Close": c, "Volume": 1000.0})


def test_analyze_tf_adds_emas_to_15m_only():
    import dailyScaner

    out = dailyScaner.analyze_tf({"15M": _bars(130, 220, 0.1), "5M": _bars(130, 220, 0.1)})
    assert {"ema9", "ema21", "ema50"} <= set(out["15M"])
    assert "ema9" not in out["5M"]
    assert es.classify(out["15M"]["ema9"], out["15M"]["ema21"], out["15M"]["ema50"])["state"] == es.BULL


def test_ema_stack_is_not_a_scoring_input():
    """Display-only: scoring modules must not read it."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for mod in ("best_value.py", "strategy_engine.py", "scoring_pool.py", "config.py"):
        text = (root / mod).read_text()
        assert "ema_stack" not in text and "ema21" not in text, mod
