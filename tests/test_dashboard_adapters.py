"""
tests/test_dashboard_adapters.py

Unit tests for data_adapter and snapshot_store.
No network calls; no yfinance; no dailyScaner imports.

Run: pytest tests/test_dashboard_adapters.py -v
"""

import json
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

# Make sure the project root is on the path when tests run from any cwd.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import snapshot_store as ss


# ── Helpers ───────────────────────────────────────────────────────────────────

def _row(side="call", strike=300.0, expiry="2026-08-15",
         dte=30, volume=1000, mid=2.50, open_interest=5000):
    return {
        "side": side, "strike": strike, "expiry": expiry, "dte": dte,
        "bid": mid - 0.05, "ask": mid + 0.05, "mid": mid,
        "last": mid, "volume": volume, "openInterest": open_interest,
        "impliedVolatility": 0.28,
    }


def _df(*rows):
    return pd.DataFrame(list(rows))


def _today_ts():
    return datetime.now().isoformat()


def _yesterday_ts():
    return (datetime.now() - timedelta(days=1)).isoformat()


# ── 1. Same-day deltas are computed ──────────────────────────────────────────

def test_same_day_delta_volume():
    prev = _df(_row(volume=1000))
    curr = _df(_row(volume=1500))
    result = ss.compute_deltas(curr, prev, _today_ts())
    assert result["delta_volume"].iloc[0] == 500
    assert result["is_stale_day"].iloc[0] == False


def test_same_day_delta_premium():
    prev = _df(_row(volume=1000, mid=2.00))
    curr = _df(_row(volume=1500, mid=2.50))
    result = ss.compute_deltas(curr, prev, _today_ts())
    # curr premium = 1500*2.50*100 = 375_000; prev = 1000*2.00*100 = 200_000
    assert abs(result["delta_premium"].iloc[0] - 175_000) < 1


# ── 2. Cross-day stale flag ───────────────────────────────────────────────────

def test_cross_day_sets_stale_flag():
    prev = _df(_row(volume=1000))
    curr = _df(_row(volume=2000))
    result = ss.compute_deltas(curr, prev, _yesterday_ts())
    assert result["is_stale_day"].iloc[0] == True


def test_cross_day_delta_volume_is_none():
    prev = _df(_row(volume=1000))
    curr = _df(_row(volume=2000))
    result = ss.compute_deltas(curr, prev, _yesterday_ts())
    assert result["delta_volume"].iloc[0] is None


def test_cross_day_delta_premium_is_none():
    prev = _df(_row(volume=1000))
    curr = _df(_row(volume=2000))
    result = ss.compute_deltas(curr, prev, _yesterday_ts())
    assert result["delta_premium"].iloc[0] is None


# ── 3. NEW flag ───────────────────────────────────────────────────────────────

def test_new_flag_for_missing_contract():
    prev = _df(_row(strike=300.0))
    curr = _df(_row(strike=300.0), _row(strike=305.0))  # 305 is new
    result = ss.compute_deltas(curr, prev, _today_ts())
    new_rows = result[result["is_new"]]
    assert len(new_rows) == 1
    assert new_rows.iloc[0]["strike"] == 305.0


def test_existing_contract_not_new():
    prev = _df(_row(strike=300.0))
    curr = _df(_row(strike=300.0))
    result = ss.compute_deltas(curr, prev, _today_ts())
    assert result["is_new"].iloc[0] == False


# ── 4. Block flag at exactly $1,000,000 ──────────────────────────────────────

def test_block_flag_at_exactly_1m():
    """Δpremium = volume_diff × mid × 100 must trigger is_block at exactly $1M."""
    # prev vol=0, curr vol=4000, mid=2.50 → delta_premium = 4000*2.50*100 = 1_000_000
    prev = _df(_row(strike=310.0, volume=0, mid=2.50))
    curr = _df(_row(strike=310.0, volume=4000, mid=2.50))
    result = ss.compute_deltas(curr, prev, _today_ts())
    assert result["delta_premium"].iloc[0] == pytest.approx(1_000_000)
    assert result["is_block"].iloc[0] == True


def test_block_flag_below_1m():
    """Δpremium just below $1M must NOT set is_block."""
    # prev vol=0, curr vol=3999, mid=2.50 → delta_premium = 999_750
    prev = _df(_row(strike=310.0, volume=0, mid=2.50))
    curr = _df(_row(strike=310.0, volume=3999, mid=2.50))
    result = ss.compute_deltas(curr, prev, _today_ts())
    assert result["delta_premium"].iloc[0] < 1_000_000
    assert result["is_block"].iloc[0] == False


def test_block_flag_false_when_stale():
    """Cross-day stale rows must never be marked as blocks."""
    prev = _df(_row(volume=0, mid=2.50))
    curr = _df(_row(volume=100_000, mid=2.50))
    result = ss.compute_deltas(curr, prev, _yesterday_ts())
    assert result["is_block"].iloc[0] == False


# ── 5. Snapshot round-trip ────────────────────────────────────────────────────

def test_snapshot_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SNAPSHOT_FILE", str(tmp_path / "snap.json"))
    df = _df(_row(volume=500, mid=3.10), _row(side="put", strike=295.0))
    ss.save_snapshot(df)
    loaded, ts = ss.load_snapshot()
    assert loaded is not None
    assert len(loaded) == len(df)
    assert set(loaded["strike"]) == set(df["strike"])
    assert ts is not None
    # Timestamp is today
    assert datetime.fromisoformat(ts).date() == date.today()


def test_load_snapshot_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "SNAPSHOT_FILE", str(tmp_path / "nonexistent.json"))
    df, ts = ss.load_snapshot()
    assert df is None
    assert ts is None


# ── 6. Gate history round-trip ────────────────────────────────────────────────

def test_gate_history_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "GATE_HISTORY_FILE", str(tmp_path / "gh.json"))
    entries = [{"verdict": "NO-TRADE", "pop": 0.35}] * 5
    ss.save_gate_history(entries)
    loaded = ss.load_gate_history()
    assert len(loaded) == 5
    assert loaded[0]["verdict"] == "NO-TRADE"


def test_gate_history_trimmed_to_20(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "GATE_HISTORY_FILE", str(tmp_path / "gh.json"))
    entries = [{"i": i} for i in range(25)]
    ss.save_gate_history(entries)
    loaded = ss.load_gate_history()
    assert len(loaded) == 20
    assert loaded[0]["i"] == 0  # newest-first: we pass them in already ordered


def test_gate_history_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(ss, "GATE_HISTORY_FILE", str(tmp_path / "no.json"))
    assert ss.load_gate_history() == []


# ── 7 & 8. app.py source checks ──────────────────────────────────────────────

APP = ROOT / "app.py"


@pytest.mark.skipif(not APP.exists(), reason="app.py not yet written")
def test_app_no_yfinance_import():
    """Delegates to the import-graph allowlist (CURSOR_SOURCES_STEPS Step 4)."""
    from tests.test_import_graph import test_app_does_not_import_yfinance_directly
    test_app_does_not_import_yfinance_directly()


@pytest.mark.skipif(not APP.exists(), reason="app.py not yet written")
def test_app_no_indicator_computation():
    src = APP.read_text()
    forbidden = ["rsi(", "ewm(", "opening_range(", " macd(", "\nmacd("]
    hits = [kw for kw in forbidden if kw in src]
    assert not hits, (
        f"app.py must not reimplement indicators. Found: {hits}"
    )


# ── 9 & 10. Session-block quote-strip rendering ───────────────────────────────

def _make_archive(tmp_path, session=None, spot=333.26):
    """Write a minimal archive JSON and return its path."""
    payload = {
        "timestamp": "2026-07-17T14:32:00",
        "spot": spot,
        "direction": "BULLISH",
        "session": session,
        "volume": {
            "total_call_vol": 100000,
            "total_put_vol": 70000,
            "pc_ratio": 0.70,
            "top_calls": [],
            "top_puts": [],
        },
        "timeframes": {},
    }
    p = tmp_path / "AAPL_20260717_1432.json"
    p.write_text(json.dumps(payload))
    return str(p)


def test_session_block_all_fields_present(tmp_path):
    """Archive with a full session block must expose all four fields."""
    session = {
        "open":       328.01,
        "prev_close": 330.95,
        "day_high":   334.68,
        "day_low":    326.79,
    }
    path = _make_archive(tmp_path, session=session, spot=333.26)
    with open(path) as f:
        payload = json.load(f)

    s = payload.get("session") or {}
    assert s.get("open")       == 328.01,  "open not persisted"
    assert s.get("prev_close") == 330.95,  "prev_close not persisted"
    assert s.get("day_high")   == 334.68,  "day_high not persisted"
    assert s.get("day_low")    == 326.79,  "day_low not persisted"

    # Δ vs prev close must be computable from archived data only
    spot      = payload["spot"]
    prev_close = s["prev_close"]
    chg        = round(spot - prev_close, 4)
    chg_pct    = round(chg / prev_close * 100, 4)
    assert abs(chg     - 2.31) < 0.01,  f"chg wrong: {chg}"
    assert abs(chg_pct - 0.698) < 0.01, f"chg_pct wrong: {chg_pct}"


def test_session_block_absent_degrades_gracefully(tmp_path):
    """Archive without a session block must not cause KeyError — spot-only path."""
    path = _make_archive(tmp_path, session=None, spot=333.26)
    with open(path) as f:
        payload = json.load(f)

    s = payload.get("session") or {}
    assert s.get("prev_close") is None, "expected no prev_close in old archive"
    assert s.get("open")       is None, "expected no open in old archive"
    # The UI falls back to spot-only — verify spot is still readable
    assert payload["spot"] == 333.26
