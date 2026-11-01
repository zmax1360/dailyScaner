"""ticker_rules: store (append-only), rule library, market context, paper recorder."""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from ticker_rules import market, recorder, rules, store

DAY = "2026-11-04"


def _ctx(closes, *, end="2026-11-04 11:30", vwap=None, first5=None, rvol=None, now=None):
    idx = pd.date_range(end=pd.Timestamp(end) - timedelta(minutes=15), periods=len(closes),
                        freq="15min")
    bars = pd.DataFrame({"Close": closes, "VWAP": vwap if vwap is not None else closes},
                        index=idx)
    return market.Context(now=now or pd.Timestamp(end).to_pydatetime(), bars=bars,
                          first5=first5, rvol=rvol)


# ------------------------------------------------------------------ store

def test_settings_are_append_only_and_newest_wins():
    a = store.add_setting("aapl", "normal", "ema_stack", {"fast": 9}, "paper", "first")
    b = store.add_setting("AAPL", "normal", "fade_midday_stack", {}, "live", "second")
    cur = store.current("AAPL")
    assert cur["normal"]["id"] == b and cur["normal"]["rule"] == "fade_midday_stack"
    h = store.history("AAPL")
    assert list(h["id"]) == [b, a]          # nothing overwritten
    with pytest.raises(ValueError):
        store.add_setting("AAPL", "weird", "x", {}, "paper")


def test_seed_only_once():
    assert store.seed_if_empty() == len(store.SEED)
    assert store.seed_if_empty() == 0
    assert store.current("SPY")["normal"]["params"]["slow"] == 89


# ------------------------------------------------------------------ rules

def test_stack_follow_and_fade():
    up = list(np.linspace(100, 110, 60))
    s = rules.evaluate("ema_stack", {"gap": 0.0}, _ctx(up))
    assert s["side"] == "CALL"
    assert rules.evaluate("fade_midday_stack", {}, _ctx(up))["side"] == "PUT"
    down = list(np.linspace(110, 100, 60))
    assert rules.evaluate("ema_stack", {"gap": 0.0}, _ctx(down))["side"] == "PUT"


def test_rules_respect_window():
    up = list(np.linspace(100, 110, 60))
    assert rules.evaluate("ema_stack", {}, _ctx(up, end="2026-11-04 10:30")) is None
    assert rules.evaluate("ema_stack", {}, _ctx(up, end="2026-11-04 13:00")) is None


def test_ema_pair_needs_price_beyond_slow():
    up = list(np.linspace(100, 110, 120))
    assert rules.evaluate("ema_pair", {"fast": 9, "slow": 89}, _ctx(up))["side"] == "CALL"
    dip = up[:-1] + [104.0]                 # close below EMA 89 while EMA 9 stays above
    assert rules.evaluate("ema_pair", {"fast": 9, "slow": 89}, _ctx(dip)) is None


def test_vwap_side():
    c = [100.0] * 10
    assert rules.evaluate("vwap_first_side", {}, _ctx(c, vwap=[99.5] * 10))["side"] == "CALL"
    assert rules.evaluate("vwap_first_side", {}, _ctx(c, vwap=[100.02] * 10)) is None


def test_orb_only_when_in_play():
    f = pd.Series({"Open": 100.0, "High": 101.0, "Low": 99.5, "Close": 100.8})
    now = datetime(2026, 11, 4, 9, 40)
    ctx = _ctx([100.0] * 5, end="2026-11-04 09:45", first5=f, rvol=2.0, now=now)
    s = rules.evaluate("orb_in_play", {"rvol_min": 1.5}, ctx)
    assert s["side"] == "CALL" and s["stop_spot"] == 99.5
    ctx.rvol = 1.2
    assert rules.evaluate("orb_in_play", {"rvol_min": 1.5}, ctx) is None


# ------------------------------------------------------------------ market

def test_market_build_vwap_rvol_and_completeness():
    rows = []
    for d in pd.bdate_range("2026-10-01", "2026-11-04"):
        for i in range(78):
            t = d + timedelta(hours=9, minutes=30 + 5 * i)
            v = 3000.0 if (i == 0 and d.date().isoformat() == DAY) else 1000.0
            rows.append((t, 100 + i * 0.01, 100.1 + i * 0.01, 99.9 + i * 0.01, 100 + i * 0.01, v))
    five = pd.DataFrame(rows, columns=["t", "Open", "High", "Low", "Close", "Volume"]).set_index("t")
    ctx = market.build(five, datetime(2026, 11, 4, 11, 7))
    assert ctx.rvol == pytest.approx(3.0)
    assert ctx.first5 is not None
    last_end = ctx.bars.index[-1] + timedelta(minutes=15)
    assert last_end <= pd.Timestamp("2026-11-04 11:07")   # only complete bars
    assert ctx.bars["VWAP"].notna().all()


# ------------------------------------------------------------------ recorder

def _chain(long_bid=1.00, long_ask=1.02, short_bid=0.40, short_ask=0.42, exp=DAY):
    return pd.DataFrame([
        {"side": "PUT", "strike": 345.0, "expiry": exp, "bid": long_bid, "ask": long_ask},
        {"side": "PUT", "strike": 342.5, "expiry": exp, "bid": short_bid, "ask": short_ask},
        {"side": "CALL", "strike": 345.0, "expiry": exp, "bid": 0.9, "ask": 0.92},
        {"side": "CALL", "strike": 347.5, "expiry": exp, "bid": 0.3, "ask": 0.31},
    ])


def test_build_spread_prices_at_ask_and_bid():
    sp = recorder.build_spread(_chain(), "PUT", 345.2, DAY, store.DEFAULT_EXIT)
    assert (sp["long_strike"], sp["short_strike"]) == (345.0, 342.5)
    assert sp["entry_cost"] == pytest.approx(1.02 - 0.40)
    assert sp["target_value"] == pytest.approx(0.62 + 0.5 * (2.5 - 0.62))
    assert sp["stop_value"] == pytest.approx(0.31)
    wide = _chain(long_bid=0.80)
    assert recorder.build_spread(wide, "PUT", 345.2, DAY, store.DEFAULT_EXIT) is None


def test_on_scan_opens_once_then_resolves():
    store.add_setting("AAPL", "normal", "ema_stack", {"gap": 0.0}, "paper")
    down = list(np.linspace(350, 345, 60))
    now = datetime(2026, 11, 4, 11, 31)
    ev = recorder.on_scan(_chain(), ticker="AAPL", now=now, spot=345.2,
                          ctx=_ctx(down, end="2026-11-04 11:30", now=now))
    assert ev["event"] == "OPEN" and ev["trade"]["side"] == "PUT"
    assert "PAPER" in recorder.message(ev)
    # same day: no second entry; the trade is marked instead
    later = now + timedelta(minutes=15)
    assert recorder.on_scan(_chain(), ticker="AAPL", now=later, spot=345.0,
                            ctx=_ctx(down, now=later)) is None
    # spread worth 1.70 -> above the 1.56 target
    ev2 = recorder.on_scan(_chain(long_bid=2.10, long_ask=2.12, short_ask=0.40), ticker="AAPL",
                           now=later + timedelta(minutes=15), spot=343.0)
    assert ev2["event"] == "CLOSE" and ev2["trade"]["status"] == store.TARGET
    assert ev2["trade"]["pnl"] == pytest.approx((1.70 - 0.62) * 100)


def test_time_stop_and_off_status():
    t = {"side": "PUT", "long_strike": 345.0, "short_strike": 342.5, "expiry": DAY,
         "entry_cost": 0.62, "target_value": 1.56, "stop_value": 0.31, "stop_spot": None,
         "time_stop": "15:30"}
    ch = recorder.resolve(t, _chain(), 345.0, datetime(2026, 11, 4, 15, 35))
    assert ch["status"] == store.TIME and ch["pnl"] == pytest.approx((0.58 - 0.62) * 100)
    store.add_setting("SPY", "normal", "ema_stack", {"gap": 0.0}, "off")
    up = list(np.linspace(100, 110, 60))
    now = datetime(2026, 11, 4, 11, 31)
    assert recorder.on_scan(_chain(), ticker="SPY", now=now, spot=345.2,
                            ctx=_ctx(up, now=now)) is None


def test_breakout_stop_on_underlying():
    t = {"side": "CALL", "long_strike": 345.0, "short_strike": 347.5, "expiry": DAY,
         "entry_cost": 0.61, "target_value": 1.56, "stop_value": 0.30, "stop_spot": 344.0,
         "time_stop": "15:30"}
    ch = recorder.resolve(t, _chain(), 343.9, datetime(2026, 11, 4, 10, 5))
    assert ch["status"] == store.STOP
