"""regime_strategy: today's options structure from gamma regime, EMA trend and move."""

from __future__ import annotations

import math

import pandas as pd
import pytest

import regime_strategy as rs

STRIKES = [340.0 + 2.5 * i for i in range(13)]          # 340 … 370
GAMMA = {"expiry": "2026-11-18", "support": 352.5, "resistance": 357.5,
         "by_strike": [[k, 1.0] for k in STRIKES]}


def _quotes():
    rows = []
    for k in STRIKES:
        ext = 2.0 * math.exp(-abs(k - 355) / 5)          # time value fades away from spot
        for side, intr in (("CALL", max(0.0, 355 - k)), ("PUT", max(0.0, k - 355))):
            mid = intr + ext
            rows.append({"side": side, "strike": k, "expiry": "2026-11-18",
                         "bid": round(mid - 0.05, 2), "ask": round(mid + 0.05, 2)})
    return pd.DataFrame(rows)


def pick(**kw):
    base = dict(ticker="AAPL", regime=rs.POSITIVE, trend_side="CALLS", spot=355.0,
                session_open=354.0, gamma=GAMMA)
    return rs.choose(**{**base, **kw})


def test_positive_gamma_between_walls_sells_the_range():
    p = pick()
    assert p["code"] == rs.IRON_CONDOR
    assert p["legs"] == [["PUT", 347.5, 1], ["PUT", 352.5, -1],
                         ["CALL", 357.5, -1], ["CALL", 362.5, 1]]      # AAPL wings: 2 steps
    assert p["status"] == "PAPER"


def test_positive_gamma_without_walls_around_price_buys_a_fly():
    p = pick(spot=353.0, gamma={**GAMMA, "resistance": None})
    assert p["code"] == rs.CALL_FLY
    assert p["legs"][1] == ["CALL", 352.5, -2]                         # support is near


def test_big_move_blocks_range_trades():
    p = pick(session_open=349.0)                                        # +$6 ≈ 1.7%
    assert p["code"] == rs.NO_TRADE
    assert p["big_move"] is True and "already moved" in p["reason"]


def test_unsettled_and_unknown_are_no_trade():
    assert pick(regime=rs.UNSETTLED)["code"] == rs.NO_TRADE
    assert pick(regime=rs.UNKNOWN, gamma=None)["code"] == rs.NO_TRADE


@pytest.mark.parametrize("ticker,code", [("AAPL", rs.NO_TRADE), ("SPY", rs.BULL_CALL_SPREAD)])
def test_negative_gamma_trend_spread_only_where_it_held_up(ticker, code):
    assert pick(ticker=ticker, regime=rs.NEGATIVE)["code"] == code


def test_negative_gamma_without_trend_is_no_trade():
    assert pick(ticker="SPY", regime=rs.NEGATIVE, trend_side="STAND_ASIDE")["code"] == rs.NO_TRADE


def test_prices_the_legs_from_quotes():
    p = pick(quotes=_quotes())
    assert p["cost_mid"] < 0                      # a condor is a credit
    assert p["max_loss"] == pytest.approx(500 + p["cost_mid"])   # $5 wings minus credit
    assert p["max_profit"] == pytest.approx(-p["cost_mid"])


def test_missing_leg_quote_leaves_price_out():
    q = _quotes()
    q = q[~((q["side"] == "CALL") & (q["strike"] == 362.5))]
    assert "cost_mid" not in pick(quotes=q)


def test_lines_show_label_legs_and_reason():
    text = "\n".join(rs.lines(pick(quotes=_quotes())))
    assert "Short iron condor" in text and "Sell 1 $352.50 put" in text and "credit" in text


def test_max_loss_over_the_limit_is_flagged():
    p = pick(quotes=_quotes())
    assert p["max_loss"] > rs.MAX_RISK and p["over_budget"] is True
    assert any("over your $40 limit" in x for x in rs.lines(p))
