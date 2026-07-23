"""
test_dailyScaner_regressions.py — regression fixtures from 2026-07-15.

That day the scanner produced three observable failures (nine archived
runs, 10:01 → 16:32):

  R1. The "15M opening range" was computed from 3 bars of the 15-MINUTE
      dataframe → a 45-minute (9:30–10:15) window mislabeled 9:30–9:45.
      The still-forming third bar made OR-high track live price:
      321.82 (10:01) → 323.76 (10:10) → 324.98 (10:14), so the scanner
      printed INSIDE RANGE during a genuine breakout, then froze the
      wrong value ($324.98) for the rest of the day.

  R2. Magnet selection let 0DTE contracts win every run because
      (a) OI <= 0 auto-passed the conviction filter and (b) stale
      overnight OI made vol/OI meaningless (vol 72,603 / OI 1).

  R3. The 16:32 run recommended buying a 0DTE contract that had
      expired at 16:00.

These tests pin the fixes. Run: pytest test_dailyScaner_regressions.py
"""

from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from dailyScaner import (
    opening_range,
    proximity_filter,
    market_is_open,
    MIN_OI_FOR_MAGNET,
)

ET = ZoneInfo("America/New_York")
DAY = date(2026, 7, 15)


def _bars_5m(rows):
    """rows: list of (HH:MM, open, high, low, close). Builds an ET-naive
    5m dataframe the way fetch_data()'s clean() produces it."""
    idx, data = [], {"Open": [], "High": [], "Low": [], "Close": [], "Volume": []}
    for hhmm, o, h, l, c in rows:
        hh, mm = map(int, hhmm.split(":"))
        idx.append(pd.Timestamp(datetime.combine(DAY, time(hh, mm))))
        data["Open"].append(o); data["High"].append(h)
        data["Low"].append(l);  data["Close"].append(c)
        data["Volume"].append(1_000_000)
    return pd.DataFrame(data, index=pd.DatetimeIndex(idx))


# Approximate 2026-07-15 open per the logs: open 317.62, 15M OR true
# high 321.82 (the value the buggy code showed at 10:01, i.e. the last
# reading taken from genuinely-in-window data), rally continuing after.
FIVE_MIN_BARS = _bars_5m([
    ("09:30", 317.62, 319.13, 317.32, 318.90),   # 5M OR bar — high 319.13 per logs
    ("09:35", 318.90, 320.75, 318.60, 320.50),
    ("09:40", 320.50, 321.82, 320.10, 321.60),   # 15M OR true high = 321.82
    ("09:45", 321.60, 322.90, 321.30, 322.70),   # post-window — must be excluded
    ("09:50", 322.70, 323.76, 322.40, 323.60),
    ("09:55", 323.60, 324.50, 323.30, 324.30),
    ("10:00", 324.30, 324.98, 324.00, 324.80),   # the wrong frozen value's source
])


def _at(hhmm):
    hh, mm = map(int, hhmm.split(":"))
    return datetime.combine(DAY, time(hh, mm), tzinfo=ET)


# ── R1: opening range window ─────────────────────────────────────────────────

def test_15m_or_uses_only_930_to_945_bars():
    res = opening_range(FIVE_MIN_BARS, pd.DataFrame(), spot=324.82, now_et=_at("10:14"))
    d = res["15M"]
    assert d is not None
    assert d["high"] == 321.82, f"15M OR high must be the 9:30–9:45 high, got {d['high']}"
    assert d["low"] == 317.32


def test_15m_or_is_immutable_after_945():
    """The buggy version returned different highs at 10:01/10:10/10:14."""
    highs = {
        opening_range(FIVE_MIN_BARS, pd.DataFrame(), spot=s, now_et=_at(t))["15M"]["high"]
        for t, s in [("10:01", 321.62), ("10:10", 323.76), ("10:14", 324.82), ("14:12", 327.54)]
    }
    assert highs == {321.82}, f"OR high changed across runs: {highs}"


def test_breakout_detected_at_1001():
    """At 10:01 spot 321.62 was fractionally below the true OR high — inside.
    By 10:10 spot 323.76 was above 321.82 — the scanner must say BREAKOUT
    (it printed INSIDE RANGE until 12:06)."""
    res = opening_range(FIVE_MIN_BARS, pd.DataFrame(), spot=323.76, now_et=_at("10:10"))
    assert res["15M"]["bias"] == "BULLISH BREAKOUT"


def test_or_forming_before_window_close():
    partial = FIVE_MIN_BARS.iloc[:2]  # 9:30 and 9:35 bars only
    res = opening_range(partial, pd.DataFrame(), spot=320.50, now_et=_at("09:41"))
    assert res["15M"]["bias_dir"] == "forming"
    # 5M window closed at 9:35 — it may already give a verdict
    assert res["5M"]["bias_dir"] != "forming"


def test_5m_or_matches_logs():
    res = opening_range(FIVE_MIN_BARS, pd.DataFrame(), spot=327.5, now_et=_at("14:32"))
    assert res["5M"]["high"] == 319.13
    assert res["5M"]["low"] == 317.32


# ── R2: magnet qualification ─────────────────────────────────────────────────

def _chain(rows):
    return pd.DataFrame(rows)

# Rows lifted from the 14:12 log: the 0DTE $327.5 put had OI 1 and vol
# 38,571; the 0DTE $327.5 call OI 2,833 vol 165,081; a legitimate
# 2d $330 call had OI 19,942 vol 38,702.
LOG_ROWS = _chain([
    dict(strike=327.5, expiry="2026-07-15", dte=0, lastPrice=0.77,
         bid=0.75, ask=0.80, volume=165081, openInterest=2833, impliedVolatility=0.25),
    dict(strike=327.5, expiry="2026-07-15", dte=0, lastPrice=0.80,
         bid=0.78, ask=0.83, volume=38571, openInterest=1, impliedVolatility=0.30),
    dict(strike=330.0, expiry="2026-07-17", dte=2, lastPrice=1.95,
         bid=1.90, ask=2.00, volume=38702, openInterest=19942, impliedVolatility=0.24),
    dict(strike=350.0, expiry="2026-08-07", dte=23, lastPrice=2.56,
         bid=2.45, ask=2.65, volume=16171, openInterest=670, impliedVolatility=0.26),
])


def test_0dte_never_qualifies_as_magnet():
    out = proximity_filter(LOG_ROWS, spot=327.54)
    assert (out["dte"] >= 1).all(), "0DTE rows must never reach magnet selection"


def test_oi_floor_enforced():
    out = proximity_filter(LOG_ROWS, spot=327.54)
    assert (out["openInterest"] >= MIN_OI_FOR_MAGNET).all()


def test_zero_oi_no_longer_auto_passes():
    rows = _chain([dict(strike=328.0, expiry="2026-07-17", dte=2, lastPrice=1.0,
                        bid=0.9, ask=1.1, volume=50000, openInterest=0,
                        impliedVolatility=0.3)])
    out = proximity_filter(rows, spot=327.54)
    assert out.empty, "OI=0 previously auto-passed the conviction filter"


def test_legitimate_contract_still_qualifies():
    out = proximity_filter(LOG_ROWS, spot=327.54)
    assert ((out["strike"] == 330.0) & (out["expiry"] == "2026-07-17")).any()


# ── R3: market-hours guard ───────────────────────────────────────────────────

def test_1632_is_closed():
    assert market_is_open(_at("16:32")) is False, \
        "16:32 run recommended an expired 0DTE — must be MARKET CLOSED"


def test_regular_session_is_open():
    assert market_is_open(_at("10:01")) is True
    assert market_is_open(_at("15:59")) is True


def test_weekend_is_closed():
    sat = datetime.combine(date(2026, 7, 18), time(11, 0), tzinfo=ET)
    assert market_is_open(sat) is False


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))


# ── R4 (2026-07-16 live run): diff must compare qualified magnets ────────────

from dailyScaner import diff_reports, save_archive
import json, os, tempfile


def _mk_chain(rows):
    return pd.DataFrame(rows)

CURR_CALLS = _mk_chain([
    dict(strike=390.0, expiry="2026-09-18", dte=64, lastPrice=1.14, bid=1.10, ask=1.20,
         volume=6430, openInterest=4814, impliedVolatility=0.30),
    dict(strike=337.5, expiry="2026-07-24", dte=8, lastPrice=1.76, bid=1.72, ask=1.86,
         volume=3872, openInterest=3089, impliedVolatility=0.26),
])
CURR_PUTS = _mk_chain([
    dict(strike=315.0, expiry="2026-07-17", dte=1, lastPrice=0.14, bid=0.13, ask=0.16,
         volume=2539, openInterest=17189, impliedVolatility=0.28),  # vol/OI 0.1 → unqualified
])


def test_diff_no_phantom_strike_change_from_raw_leader():
    """Live 2026-07-16 10:04 run printed 'CALL MAGNET shifted $390.0 → $337.5'
    because prev raw top ($390 LEAP) was compared to the current qualified
    magnet ($337.5). With like-with-like comparison there is no shift."""
    prev = {"spot": 329.88, "timestamp": "2026-07-16T10:03:00",
            "volume": {"pc_ratio": 0.48,
                       "top_calls": [dict(strike=390.0, expiry="2026-09-18", volume=6430,
                                          impliedVolatility=0.30)],
                       "top_puts":  [dict(strike=265.0, expiry="2026-08-21", volume=2000,
                                          impliedVolatility=0.35)]},
            "signal_magnets": {"call": dict(strike=337.5, expiry="2026-07-24", dte=8,
                                            volume=3800, openInterest=3089,
                                            impliedVolatility=0.26),
                               "put": None},
            "timeframes": {}}
    changes = diff_reports(prev, 330.02, {}, CURR_CALLS, CURR_PUTS, 0.46)
    joined = "\n".join(changes)
    assert "STRIKE CHANGE" not in joined, f"phantom shift: {joined}"
    assert "390.0" not in joined, "raw leader leaked into magnet diff"


def test_diff_reports_none_qualified_consistently():
    """When the put side has no qualified contract, the diff must not invent
    one (the live run said 'shifted $265.0 → $315.0' while SIGNAL said none)."""
    prev = {"spot": 329.88, "timestamp": "2026-07-16T10:03:00",
            "volume": {"pc_ratio": 0.48, "top_calls": [], "top_puts": []},
            "signal_magnets": {"call": dict(strike=337.5, expiry="2026-07-24", dte=8,
                                            volume=3800, openInterest=3089,
                                            impliedVolatility=0.26),
                               "put": dict(strike=320.0, expiry="2026-07-24", dte=8,
                                           volume=4000, openInterest=2000,
                                           impliedVolatility=0.30)},
            "timeframes": {}}
    changes = diff_reports(prev, 330.02, {}, CURR_CALLS, CURR_PUTS, 0.46)
    joined = "\n".join(changes)
    assert "none qualified" in joined
    assert "315.0" not in joined, "unqualified put leaked into magnet diff"


def test_archive_persists_signal_magnets(tmp_path):
    os.chdir(tmp_path)
    tf = {"1D": {"rsi": 70.0, "macd": 1.0, "sig": 0.5, "hist": 1.0,
                 "vs": 1.0, "support": 300.0, "resist": 331.0, "price": 330.0}}
    fjson, _ = save_archive(330.02, tf, CURR_CALLS, CURR_PUTS)
    payload = json.load(open(fjson))
    assert "signal_magnets" in payload
    assert payload["signal_magnets"]["call"]["strike"] == 337.5
    assert payload["signal_magnets"]["put"] is None
