"""regime_strategy: today's 0DTE structure, gated by expiry/time/regime, chosen by simulation."""

from __future__ import annotations

import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import regime_strategy as rs

ET = ZoneInfo("America/New_York")
DAY = "2026-11-18"
STRIKES = [340.0 + 2.5 * i for i in range(13)]          # 340 … 370
GAMMA = {"expiry": DAY, "support": 352.5, "resistance": 357.5,
         "by_strike": [[k, 1.0] for k in STRIKES]}
NOON = datetime(2026, 11, 18, 12, 30, tzinfo=ET)


def _quotes(spot=355.0):
    rows = []
    for k in STRIKES:
        ext = 2.0 * math.exp(-abs(k - spot) / 5)          # time value fades away from spot
        for side, intr in (("CALL", max(0.0, spot - k)), ("PUT", max(0.0, k - spot))):
            mid = intr + ext
            rows.append({"side": side, "strike": k, "expiry": DAY,
                         "bid": round(mid - 0.05, 2), "ask": round(mid + 0.05, 2)})
    return pd.DataFrame(rows)


def _samples(zs, ticker="AAPL", regime="POSITIVE"):
    return [{"ticker": ticker, "day": f"d{i}", "z": z, "regime": regime}
            for i, z in enumerate(zs)]


QUIET = _samples([0.0, 0.05, -0.05, 0.1, -0.1, 0.0, 0.02, -0.02, 0.08, -0.08, 0.0, 0.03] * 2)
WILD = _samples([2.5, -2.5, 3.0, -3.0] * 6)


def pick(**kw):
    base = dict(ticker="AAPL", regime=rs.POSITIVE, trend_side="CALLS", spot=355.0,
                session_open=354.0, gamma=GAMMA, quotes=_quotes(), now=NOON, samples=QUIET)
    return rs.choose(**{**base, **kw})


def test_no_trade_when_the_nearest_expiry_is_not_today():
    p = pick(now=datetime(2026, 11, 17, 12, 30, tzinfo=ET))
    assert p["code"] == rs.NO_TRADE and "0DTE days only" in p["reason"]


@pytest.mark.parametrize("hh,mm,word", [(11, 0, "wait until 12:00"), (15, 0, "too late")])
def test_entry_window(hh, mm, word):
    p = pick(now=datetime(2026, 11, 18, hh, mm, tzinfo=ET))
    assert p["code"] == rs.NO_TRADE and word in p["reason"]


def test_quiet_history_picks_the_structure_that_made_most(monkeypatch):
    monkeypatch.setattr(rs, "MAX_RISK", 10_000.0)
    p = pick()
    assert p["code"] != rs.NO_TRADE and p["sim_avg"] > 0
    avgs = [a["sim_avg"] for a in p["alternatives"] if "sim_avg" in a]
    assert p["sim_avg"] == max(avgs) and len(avgs) >= 3
    assert p["status"] == "PAPER" and "Best of" in p["reason"]


def test_wild_history_picks_nothing():
    p = pick(samples=WILD)
    assert p["code"] == rs.NO_TRADE and "No structure made money" in p["reason"]
    assert p["alternatives"]


def test_too_little_history_lists_but_does_not_pick():
    p = pick(samples=QUIET[:5])
    assert p["code"] == rs.NO_TRADE and "Not enough history" in p["reason"]


def test_big_move_blocks_range_trades():
    p = pick(session_open=349.0)                                        # +$6 ≈ 1.7%
    assert p["code"] == rs.NO_TRADE and p["big_move"] is True and "already moved" in p["reason"]


def test_unsettled_and_unknown_are_no_trade():
    assert pick(regime=rs.UNSETTLED)["code"] == rs.NO_TRADE
    assert pick(regime=rs.UNKNOWN, gamma=None)["code"] == rs.NO_TRADE


@pytest.mark.parametrize("ticker,code", [("AAPL", rs.NO_TRADE), ("SPY", rs.BULL_CALL_SPREAD)])
def test_negative_gamma_trend_spread_only_where_it_held_up(ticker, code, monkeypatch):
    monkeypatch.setattr(rs, "MAX_RISK", 10_000.0)
    trending = _samples([0.9, 1.2, 0.8, 1.5] * 4, ticker=ticker, regime="NEGATIVE")
    assert pick(ticker=ticker, regime=rs.NEGATIVE, samples=trending)["code"] == code


def test_iron_condor_prices_and_max_loss():
    p = pick(samples=None)                        # no simulation: first candidate
    assert p["code"] == rs.IC_WALLS and p["cost_mid"] < 0
    assert p["max_loss"] == pytest.approx(500 + p["cost_fill"] + p["fees"])
    assert p["over_budget"] is True


def test_best_over_budget_is_not_picked():
    p = pick()                                    # $40 limit: condors risk ~$400
    assert p["code"] == rs.NO_TRADE and "over your $40 limit" in p["reason"]


def test_lines_show_pick_reason_and_others(monkeypatch):
    monkeypatch.setattr(rs, "MAX_RISK", 10_000.0)
    text = "\n".join(rs.lines(pick()))
    assert "Strategy (paper)" in text and "Best of" in text and "Others:" in text


def test_load_samples_reads_the_file(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"samples": QUIET}))
    assert len(rs.load_samples(str(f))) == len(QUIET)
    assert rs.load_samples(str(tmp_path / "missing.json")) == []


def test_no_quotes_with_history_is_no_trade():
    p = pick(quotes=None)
    assert p["code"] == rs.NO_TRADE and "No usable quote" in p["reason"]
