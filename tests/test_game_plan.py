"""Game plan: the rule engine (game_plan.py), its view, and the Telegram lines."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import ema_stack
import game_plan as gp

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
AS_OF = datetime(2026, 10, 4, 16, 18, tzinfo=ET)

GAMMA = {"expiry": "2026-10-05", "net": 34.21e6, "support": 330.0, "resistance": 335.0,
         "put_wall": 320.0, "first_negative_below_spot": 325.0}
EXPECTED = {"Upper_1SD": 342.75, "Lower_1SD": 324.63}


def _trend(e9, e21, e50, *, stale=False):
    t = ema_stack.classify(e9, e21, e50)
    t.update(as_of=AS_OF, stale=stale)
    return t


BULL = _trend(333.44, 333.01, 332.62)
BEAR = _trend(331.0, 332.0, 333.0)
MIXED = _trend(333.0, 334.0, 332.0)
NO_DATA = _trend(None, None, None)


def _pick(side, score, *, pool="1DTE+", strike=335.0, expiry="2026-10-09", dte=5, last=1.47):
    return {"side": side, "pool": pool, "Value_Score": score, "strike": strike,
            "expiry": expiry, "dte": dte, "last": last, "volume": 14194,
            "openInterest": 11509, "delta": 0.31}


PICKS = pd.DataFrame([
    _pick("CALL", 0.21),
    _pick("CALL", 0.35, strike=337.5, last=0.90),          # best call
    _pick("PUT", 0.50, strike=330.0, last=1.00),           # best put
    _pick("CALL", 0.90, pool="0DTE", strike=335.0, expiry="2026-10-05", dte=0),   # 0DTE: never
    _pick("CALL", float("nan"), strike=340.0),             # not ranked
])


def _plan(trend=BULL, gamma=GAMMA, **kw):
    kw.setdefault("vwap", 332.91)
    kw.setdefault("expected", EXPECTED)
    kw.setdefault("picks", PICKS)
    kw.setdefault("session_high", 334.54)
    kw.setdefault("session_low", 330.60)
    return gp.build_plan(ticker="aapl", spot=333.69, trend=trend, gamma=gamma, **kw)


# ── allowed side ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("trend, side", [(BULL, gp.CALLS), (BEAR, gp.PUTS),
                                         (MIXED, gp.STAND_ASIDE), (NO_DATA, gp.UNKNOWN),
                                         (None, gp.UNKNOWN)])
def test_allowed_side_follows_the_trend_rule(trend, side):
    assert _plan(trend=trend).side == side


def test_side_reason_is_the_trend_rules_own_wording():
    assert _plan().side_reason == BULL["reason"]
    assert _plan(trend=None).side_reason == "No 15-minute EMA data in this scan."


# ── type of day ─────────────────────────────────────────────────────────────

def test_type_of_day_is_the_sign_of_net_gamma():
    assert _plan().regime == gp.POSITIVE and _plan().net_gex == pytest.approx(34.21e6)
    assert _plan(gamma={**GAMMA, "net": -2.5e6}).regime == gp.NEGATIVE
    assert _plan(gamma={**GAMMA, "net": 0.0}).regime == gp.POSITIVE
    unknown = _plan(gamma=None)
    assert unknown.regime == gp.UNKNOWN and unknown.net_gex is None
    assert "gamma" in unknown.missing


# ── candidate ───────────────────────────────────────────────────────────────

def test_there_is_one_candidate_per_pool_in_a_fixed_order():
    plan = _plan()
    assert [c.pool for c in plan.candidates] == ["1DTE+", "0DTE"]
    assert plan.candidate_for("1DTE+") is plan.candidates[0]
    assert plan.candidate_for("nope") is None


def test_each_candidate_is_the_best_ranked_pick_of_its_pool_on_the_allowed_side():
    plan = _plan()
    swing, day = plan.candidate_for("1DTE+").pick, plan.candidate_for("0DTE").pick
    assert (swing["side"], swing["strike"], swing["score"]) == ("CALL", 337.5, 0.35)
    assert (day["side"], day["strike"], day["score"], day["dte"]) == ("CALL", 335.0, 0.90, 0)
    assert plan.candidate_for("0DTE").note.startswith(
        "Top-ranked 0DTE Best Value pick on the allowed side.")
    puts = _plan(trend=BEAR)
    assert puts.candidate_for("1DTE+").pick["strike"] == 330.0
    assert puts.candidate_for("0DTE").pick is None                  # no 0DTE put was ranked
    assert puts.candidate_for("0DTE").note == "No ranked 0DTE put in this scan."


def test_pools_are_never_mixed_and_unranked_contracts_are_never_chosen():
    only_0dte = PICKS[PICKS["pool"] == "0DTE"]
    plan = _plan(picks=only_0dte)
    assert plan.candidate_for("1DTE+").pick is None
    assert plan.candidate_for("1DTE+").note == "No ranked 1DTE+ call in this scan."
    assert plan.candidate_for("0DTE").pick["strike"] == 335.0
    unranked = PICKS[PICKS["Value_Score"].isna()]
    assert all(c.pick is None for c in _plan(picks=unranked).candidates)


@pytest.mark.parametrize("trend", [MIXED, NO_DATA])
def test_no_candidate_while_no_side_is_allowed(trend):
    plan = _plan(trend=trend)
    assert all(c.pick is None for c in plan.candidates)
    assert {c.note for c in plan.candidates} == {"No candidate while no side is allowed."}


def test_candidate_handles_missing_or_malformed_picks():
    for picks in (None, pd.DataFrame(), pd.DataFrame([{"side": "CALL"}])):
        assert all(c.pick is None for c in _plan(picks=picks).candidates)


# ── levels ──────────────────────────────────────────────────────────────────

def test_levels_are_sorted_high_to_low_with_spot_among_them():
    names = [lv.name for lv in _plan().levels]
    assert names == ["Expected range high", "Call resistance", "Spot", "VWAP",
                     "Gamma support", "First negative strike", "Expected range low",
                     "Put wall"]
    prices = [lv.price for lv in _plan().levels]
    assert prices == sorted(prices, reverse=True)


def test_levels_sharing_a_price_are_merged_and_missing_ones_left_out():
    same = {**GAMMA, "first_negative_below_spot": 320.0}
    names = [lv.name for lv in _plan(gamma=same).levels]
    assert "Put wall / First negative strike" in names
    bare = _plan(gamma=None, vwap=None, expected=None)
    assert [lv.name for lv in bare.levels] == ["Spot"]
    assert set(bare.missing) == {"gamma", "VWAP", "expected range"}


def test_a_level_exactly_at_spot_keeps_spot_as_its_own_row():
    plan = gp.build_plan(ticker="AAPL", spot=330.0, trend=BULL, gamma=GAMMA)
    names = [lv.name for lv in plan.levels]
    assert "Spot" in names and "Gamma support" in names


# ── what to expect, and what changes the plan ───────────────────────────────

def test_expectations_for_each_combination():
    calls_pos = _plan().expect
    assert calls_pos[0] == ("The trend rule allows calls only, on a day when moves tend "
                            "to be contained.")
    assert "Dips toward gamma support at $330 tend to be bought." in calls_pos
    assert "Rallies tend to stall near call resistance at $335." in calls_pos

    puts_pos = _plan(trend=BEAR).expect
    assert "puts only" in puts_pos[0]
    assert "Declines tend to slow near gamma support at $330." in puts_pos

    calls_neg = _plan(gamma={**GAMMA, "net": -1.0}).expect
    assert "amplified" in calls_neg[0] and len(calls_neg) == 2

    assert "allows no trade" in _plan(trend=MIXED).expect[0]
    assert "no data" in _plan(trend=NO_DATA).expect[0]
    assert "type of day is unknown" in _plan(gamma=None).expect[0]


def test_what_changes_the_plan():
    changes = _plan().changes
    assert changes[0].startswith("The 15-minute EMAs lose their stack")
    assert any("$325 or below" in c for c in changes)
    assert any("line up 9 above 21 above 50" in c for c in _plan(trend=MIXED).changes)
    neg = _plan(gamma={**GAMMA, "net": -1.0}).changes
    assert any("turns positive" in c for c in neg) and not any("$325" in c for c in neg)
    stale = _plan(trend=_trend(333.44, 333.01, 332.62, stale=True))
    assert stale.stale and any("over 30 minutes old" in c for c in stale.changes)


def test_same_inputs_give_the_same_plan():
    assert _plan() == _plan()


def test_the_engine_uses_no_ai_no_network_and_no_app_code():
    src = (ROOT / "game_plan.py").read_text()
    for banned in ("anthropic", "openai", "requests", "urllib", "yfinance", "streamlit"):
        assert f"import {banned}" not in src and f"from {banned}" not in src, banned


# ── trade plan ──────────────────────────────────────────────────────────────

def _trade(side=gp.CALLS, **kw):
    kw.setdefault("spot", 333.69)
    kw.setdefault("gamma", GAMMA)
    kw.setdefault("vwap", 332.91)
    kw.setdefault("expected", EXPECTED)
    kw.setdefault("session_high", 334.54)
    kw.setdefault("session_low", 330.60)
    return gp.build_trade_plan(side, **kw)


def test_calls_stop_sits_under_the_nearest_support_below_price():
    t = _trade()
    assert t.ready and t.better_entry.name == "VWAP"                 # 332.91 beats 330
    assert t.stop == pytest.approx(332.91 * (1 - 0.10 / 100))
    assert t.risk == pytest.approx(333.69 - t.stop)
    assert "0.1% under VWAP at $332.91" in t.stop_note
    no_vwap = _trade(vwap=None)
    assert no_vwap.better_entry.name == "Gamma support"
    assert no_vwap.stop == pytest.approx(330.0 * 0.999)
    above = _trade(vwap=334.20)                                      # VWAP above price: not support
    assert above.better_entry.name == "Gamma support"


def test_stop_buffer_is_adjustable():
    assert _trade(stop_buffer_pct=0.25).stop == pytest.approx(332.91 * (1 - 0.0025))
    assert _trade(stop_buffer_pct=0.0).stop == pytest.approx(332.91)


def test_calls_targets_are_session_high_call_resistance_and_range_high_in_order():
    t = _trade()
    assert [(x.name, x.price) for x in t.targets] == [
        ("Session high", 334.54), ("Call resistance", 335.0), ("Expected range high", 342.75)]
    assert [x.share for x in t.targets] == [0.30, 0.40, 0.30]
    assert [x.reward for x in t.targets] == pytest.approx([0.85, 1.31, 9.06])


def test_targets_at_or_below_price_are_dropped_and_shares_rebalance():
    two = _trade(session_high=333.00)                    # already above today's high
    assert [x.name for x in two.targets] == ["Call resistance", "Expected range high"]
    assert [x.share for x in two.targets] == [0.30, 0.70]
    one = _trade(session_high=None, expected=None)
    assert [(x.name, x.share) for x in one.targets] == [("Call resistance", 1.0)]
    assert sum(x.share for x in _trade().targets) == pytest.approx(1.0)


def test_targets_that_sit_together_are_merged():
    t = _trade(session_high=335.05)                       # within 0.05% of call resistance
    assert t.targets[0].name == "Call resistance / Session high"
    assert len(t.targets) == 2 and t.targets[0].price == 335.0


def test_reward_to_risk_is_measured_from_the_current_price():
    t = _trade()
    assert t.reward_to_risk == pytest.approx(0.85 / (333.69 - 332.91 * 0.999))
    assert t.min_reward_to_risk == 1.5


def test_entry_limit_is_where_reward_equals_the_required_multiple_of_risk():
    t = _trade()
    stop, t1 = t.stop, t.targets[0].price
    assert t.entry_limit == pytest.approx((t1 + 1.5 * stop) / 2.5)
    assert (t1 - t.entry_limit) / (t.entry_limit - stop) == pytest.approx(1.5)
    assert round(t.entry_limit, 2) == 333.36
    assert _trade(min_reward_to_risk=1.0).entry_limit == pytest.approx((t1 + stop) / 2)
    assert _trade(min_reward_to_risk=3.0).entry_limit < t.entry_limit       # stricter: tighter


def test_price_above_the_entry_zone_means_wait():
    t = _trade()                                           # 333.69 against a limit of 333.36
    assert not t.in_zone and t.poor
    assert t.notes == ("Price is above the entry zone. At $333.69 the reward to target 1 is "
                       "0.8 times the risk to the stop, and the plan needs 1.5. Wait for "
                       "$333.36 or lower.",)


def test_price_inside_the_entry_zone_has_no_warning():
    t = _trade(spot=333.20)
    assert t.in_zone and not t.poor and t.notes == ()
    assert t.reward_to_risk >= 1.5
    assert t.better_entry.price < 333.20 <= t.entry_limit


def test_puts_entry_limit_is_a_floor():
    t = _trade(gp.PUTS, vwap=334.20)                       # stop above 334.20, target 330.60
    assert (t.entry_limit - t.targets[0].price) / (t.stop - t.entry_limit) == pytest.approx(1.5)
    assert t.in_zone                                       # 333.69 is above the floor
    low = _trade(gp.PUTS, vwap=334.20, spot=331.50)
    assert not low.in_zone and "below the entry zone" in low.notes[0]
    assert "or higher" in low.notes[0]


def test_unready_plans_have_no_entry_limit():
    assert _trade(vwap=None, gamma={**GAMMA, "support": None}).entry_limit is None
    assert _trade(session_high=None, expected=None,
                  gamma={**GAMMA, "resistance": None}).entry_limit is None


def test_candidate_outside_the_delta_band_is_kept_but_flagged():
    """Reported case: the top-ranked call was a $0.07 contract far out of the money."""
    import best_value_ui

    assert gp.DELTA_BAND == (best_value_ui.DISPLAY_DELTA_MIN, best_value_ui.DISPLAY_DELTA_MAX)
    penny = pd.DataFrame([_pick("CALL", 0.19, strike=342.5, expiry="2026-10-05", dte=1,
                                last=0.07) | {"delta": 0.03}])
    c = _plan(picks=penny).candidate_for("1DTE+")
    assert c.pick["strike"] == 342.5                                   # still the pick
    assert c.note.endswith("Its delta is 0.03, outside the 0.35 to 0.50 pre-trade band: "
                           "a low-probability contract.")
    inside = pd.DataFrame([_pick("CALL", 0.19) | {"delta": 0.42}])
    assert _plan(picks=inside).candidate_for("1DTE+").note == (
        "Top-ranked 1DTE+ Best Value pick on the allowed side.")
    deep = pd.DataFrame([_pick("PUT", 0.19) | {"delta": -0.80}])
    assert _plan(trend=BEAR, picks=deep).candidate_for("1DTE+").note.endswith(
        "outside the 0.35 to 0.50 pre-trade band.")
    unknown = pd.DataFrame([_pick("CALL", 0.19) | {"delta": None}])
    assert "has no delta" in _plan(picks=unknown).candidate_for("1DTE+").note


def test_puts_mirror_calls():
    t = _trade(gp.PUTS, vwap=334.20)
    assert t.better_entry.name == "VWAP"                              # nearest resistance above
    assert t.stop == pytest.approx(334.20 * 1.001) and t.stop > 333.69
    assert [x.name for x in t.targets] == ["Session low", "Gamma support",
                                           "Expected range low"]
    assert [x.price for x in t.targets] == [330.60, 330.0, 324.63]
    assert all(x.reward > 0 for x in t.targets)
    assert "above VWAP" in t.stop_note
    below = _trade(gp.PUTS)                                           # VWAP 332.91 is below
    assert below.better_entry.name == "Call resistance"


def test_no_trade_plan_without_an_allowed_side():
    assert gp.build_trade_plan(gp.STAND_ASIDE, spot=333.69, gamma=GAMMA) is None
    assert gp.build_trade_plan(gp.UNKNOWN, spot=333.69, gamma=GAMMA) is None
    assert _plan(trend=MIXED).trade is None and _plan().trade.side == gp.CALLS


def test_plan_is_not_ready_without_a_stop_level_or_a_target():
    no_stop = _trade(vwap=None, gamma={**GAMMA, "support": None})
    assert not no_stop.ready and no_stop.stop is None
    assert no_stop.notes == ("No support level below the current price to anchor a stop to.",)
    no_target = _trade(session_high=None, expected=None, gamma={**GAMMA, "resistance": None})
    assert not no_target.ready and no_target.stop is not None and no_target.targets == ()
    assert "No target above the current price" in no_target.notes[0]
    assert not _trade(spot=None).ready


def test_the_other_side_is_stated_as_not_allowed():
    assert _trade().other_side.startswith("Puts: not allowed today under the trend rule.")
    assert "9 below 21 below 50" in _trade().other_side
    assert _trade(gp.PUTS).other_side.startswith("Calls: not allowed today")


def test_time_exit_comes_from_the_existing_time_stop_rule():
    import time_stop

    text = _trade().time_exit
    assert f"0DTE: {time_stop.WINDOW_0DTE_MINUTES} minutes" in text
    assert f"1DTE+: {time_stop.WINDOW_MULTI_DTE_MINUTES} minutes" in text
    assert f"{time_stop.TIME_STOP_CAP:%H:%M} ET" in text
    assert _trade().single_contract == "With one contract, exit all of it at target 1."


def test_view_draws_the_trade_plan_with_a_warning_when_poor():
    at = _run()
    text = " ".join(m.value for m in at.markdown)
    assert "Trade plan · calls" in text
    table = next(m.value for m in at.markdown if "<table" in m.value and "Target 1" in m.value)
    assert table.index("Enter at or below") < table.index("Current price") \
        < table.index(">Stop<") < table.index("Target 1")
    assert "&#36;333.36" in table and "Outside the entry zone: wait." in table
    assert "Close 30%" in table and "Close 40%" in table and "$" not in table
    assert len(at.warning) == 1 and "above the entry zone" in at.warning[0].value
    assert any("Puts: not allowed today" in c.value for c in at.caption)
    assert any("With one contract, exit all of it at target 1." in c.value for c in at.caption)


def test_view_has_no_trade_plan_on_a_stand_aside_day():
    at = _run(trend="MIXED")
    assert "Trade plan" not in " ".join(m.value for m in at.markdown)
    assert len(at.warning) == 0


# ── Telegram lines ──────────────────────────────────────────────────────────

def test_plan_lines_for_telegram():
    lines = gp.plan_lines(_plan())
    assert lines[:5] == [
        "🎯 <b>GAME PLAN · AAPL</b>",
        "Side: <b>Calls only</b>",
        "Day: <b>Positive gamma</b>",
        "1DTE+: CALL $337.5 2026-10-09 (5d) · $0.9 · score 0.35",
        "0DTE: CALL $335 2026-10-05 (0d) · $1.47 · score 0.90",
    ]
    assert lines[5:9] == [
        "Calls plan: enter at or below <b>$333.36</b> (now $333.69: wait)",
        "Stop <b>$332.58</b> (VWAP $332.91)",
        "Targets: $334.54 Session high (30%) · $335 Call resistance (40%) · "
        "$342.75 Expected range high (30%)",
        "Reward to risk from here: <b>0.8</b> (needs 1.5)",
    ]
    assert lines[9].startswith("Levels: Expected range high $342.75 &gt; Call resistance $335")
    assert all(line.startswith("• ") for line in lines[10:]) and len(lines) > 10
    stale = gp.plan_lines(_plan(trend=_trend(333.44, 333.01, 332.62, stale=True)))
    assert stale[1] == "Side: <b>Calls only</b> (scan is stale)"
    assert gp.plan_lines(_plan(trend=MIXED))[3:5] == ["1DTE+: None", "0DTE: None"]


def test_scan_message_carries_the_plan_first_when_asked():
    import json

    import ui.telegram_push as tg

    payload = json.loads((ROOT / "tests" / "golden" / "AAPL_20260728_095049.json").read_text())
    off = {k: False for k in ("session", "mtf", "magnets", "volume_expiry", "orb", "deltas",
                              "best_value", "gamma")}
    without = tg._format_scan_message(payload=payload, prev_payload=None, ticker="AAPL",
                                      top_n=5, include=off, plan=_plan())
    assert "GAME PLAN" not in without
    msg = tg._format_scan_message(payload=payload, prev_payload=None, ticker="AAPL", top_n=5,
                                  include={**off, "game_plan": True, "gamma": True},
                                  plan=_plan(), gamma=None)
    assert "🎯 <b>GAME PLAN · AAPL</b>" in msg
    assert msg.index("GAME PLAN") < msg.index("GAMMA")          # the summary comes first
    assert len(msg) < 4096


def test_plan_for_message_builds_from_an_archive_without_vwap():
    import json

    import ui.telegram_push as tg

    payload = json.loads((ROOT / "tests" / "golden" / "AAPL_20260728_095049.json").read_text())
    plan = tg.plan_for_message(payload, None, "AAPL", gamma=GAMMA)
    assert plan.ticker == "AAPL" and plan.spot == float(payload["spot"])
    assert plan.regime == gp.POSITIVE and "VWAP" in plan.missing
    assert plan.side in (gp.CALLS, gp.PUTS, gp.STAND_ASIDE, gp.UNKNOWN)


# ── view ────────────────────────────────────────────────────────────────────

_SCRIPT = """
from tests.test_game_plan import _plan, {trend}
from ui import game_plan_view
game_plan_view.render(_plan(trend={trend}{extra}))
"""


def _run(trend="BULL", extra=""):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_SCRIPT.format(trend=trend, extra=extra), default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def test_view_shows_side_day_candidate_levels_and_changes():
    at = _run()
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Allowed side"] == "🟢 Calls only"
    assert metrics["Type of day"] == "Positive gamma"
    assert metrics["1DTE+ candidate"] == "CALL $337.5 2026-10-09 (5d)"
    assert metrics["0DTE candidate"] == "CALL $335 2026-10-05 (0d)"
    assert not any("\\" in str(m.value) for m in at.metric)          # no stray backslash
    text = " ".join(m.value for m in at.markdown)
    assert "What to expect" in text and "What changes the plan" in text
    table = next(m.value for m in at.markdown if "<table" in m.value and ">Spot<" in m.value)
    assert table.index("Call resistance") < table.index(">Spot<") < table.index("Gamma support")
    assert "&#36;333.69" in table and "+1.31 (+0.4%)" in table        # 335 against 333.69
    assert "$" not in table                                            # nothing for LaTeX to grab


def test_view_escapes_dollar_signs_in_text():
    at = _run()
    for m in at.markdown:
        if "<table" not in m.value:
            assert m.value.count("$") == m.value.count(r"\$"), m.value
    for c in at.caption:
        assert c.value.count("$") == c.value.count(r"\$"), c.value


def test_view_for_stand_aside_and_missing_inputs():
    at = _run(trend="MIXED", extra=", gamma=None, vwap=None, expected=None")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Allowed side"] == "⛔ Stand aside"
    assert metrics["Type of day"] == "Unknown"
    assert metrics["1DTE+ candidate"] == "None" and metrics["0DTE candidate"] == "None"
    assert not any("<table" in m.value for m in at.markdown)           # only Spot: no ladder
    assert any("not available: gamma, VWAP, expected range" in c.value for c in at.caption)


# ── wiring ──────────────────────────────────────────────────────────────────

def test_game_plan_is_the_landing_page_and_a_flow_section():
    from ui import shell
    from ui.pages import flow

    assert shell.default_page() == "game_plan"
    assert flow.SECTIONS[0] == "game_plan" and "game_plan" not in flow.OVERVIEW


def test_game_plan_page_draws_only_its_section(monkeypatch):
    from ui.pages import flow

    seen = {}
    monkeypatch.setattr(flow, "render", lambda cfg, sections=None: seen.update(s=sections))
    flow.render_game_plan({"ticker": "AAPL"})
    assert seen["s"] == frozenset({"game_plan"})
