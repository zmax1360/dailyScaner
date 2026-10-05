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

def test_candidate_is_the_best_ranked_1dte_pick_on_the_allowed_side():
    c = _plan().candidate
    assert (c["side"], c["strike"], c["score"]) == ("CALL", 337.5, 0.35)
    p = _plan(trend=BEAR).candidate
    assert (p["side"], p["strike"]) == ("PUT", 330.0)


def test_zero_dte_and_unranked_contracts_are_never_the_candidate():
    only_0dte = PICKS[PICKS["pool"] == "0DTE"]
    plan = _plan(picks=only_0dte)
    assert plan.candidate is None
    assert plan.candidate_note == "No ranked 1DTE+ call in this scan."


@pytest.mark.parametrize("trend", [MIXED, NO_DATA])
def test_no_candidate_while_no_side_is_allowed(trend):
    plan = _plan(trend=trend)
    assert plan.candidate is None
    assert plan.candidate_note == "No candidate while no side is allowed."


def test_candidate_handles_missing_or_malformed_picks():
    assert _plan(picks=None).candidate is None
    assert _plan(picks=pd.DataFrame()).candidate is None
    assert _plan(picks=pd.DataFrame([{"side": "CALL"}])).candidate is None


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


# ── Telegram lines ──────────────────────────────────────────────────────────

def test_plan_lines_for_telegram():
    lines = gp.plan_lines(_plan())
    assert lines[:4] == [
        "🎯 <b>GAME PLAN · AAPL</b>",
        "Side: <b>Calls only</b>",
        "Day: <b>Positive gamma</b>",
        "Candidate: CALL $337.5 2026-10-09 (5d) · $0.9 · score 0.35",
    ]
    assert lines[4].startswith("Levels: Expected range high $342.75 &gt; Call resistance $335")
    assert all(line.startswith("• ") for line in lines[5:]) and len(lines) > 5
    stale = gp.plan_lines(_plan(trend=_trend(333.44, 333.01, 332.62, stale=True)))
    assert stale[1] == "Side: <b>Calls only</b> (scan is stale)"
    assert gp.plan_lines(_plan(trend=MIXED))[3] == "Candidate: None"


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
    assert metrics["Candidate"] == r"CALL \$337.5 2026-10-09 (5d)"
    text = " ".join(m.value for m in at.markdown)
    assert "What to expect" in text and "What changes the plan" in text
    table = next(m.value for m in at.markdown if "<table" in m.value)
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
    assert metrics["Type of day"] == "Unknown" and metrics["Candidate"] == "None"
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
