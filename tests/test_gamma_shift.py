"""Gamma-shift alert: the call / put wall moving to another strike (gamma_shift.py)."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import gamma_shift as gs
import scheduler as sched

ET = ZoneInfo("America/New_York")
EXP = "2026-10-16"


def _s(hhmm: str, call_wall, by_strike: dict, *, put_wall=None, expiry=EXP, spot=343.2,
       day="2026-10-16"):
    h, m = map(int, hhmm.split(":"))
    y, mo, d = map(int, day.split("-"))
    return {"expiry": expiry, "spot": spot, "call_wall": call_wall,
            "call_wall_gex": by_strike.get(call_wall), "put_wall": put_wall,
            "put_wall_gex": by_strike.get(put_wall), "by_strike": by_strike,
            "as_of": datetime(y, mo, d, h, m, tzinfo=ET)}


def _run(reads):
    state, out = None, []
    for r in reads:
        state, ev = gs.detect(state, r)
        out.append(ev)
    return state, out


def test_first_read_of_the_day_only_sets_the_baseline():
    state, ev = gs.detect(None, _s("09:45", 345.0, {345.0: 2e6}))
    assert ev == [] and state["call_wall"] == 345.0 and state["day"] == "2026-10-16"


def test_oct_15_path_alerts_each_move_and_the_return():
    reads = [_s("09:45", 345.0, {345.0: 2.0e6, 342.5: 1.0e6}),
             _s("10:30", 342.5, {345.0: 1.2e6, 342.5: 2.0e6}),
             _s("11:15", 340.0, {342.5: 1.1e6, 340.0: 2.2e6}),
             _s("14:00", 345.0, {340.0: 1.0e6, 345.0: 2.5e6})]
    state, out = _run(reads)
    assert [len(e) for e in out] == [0, 1, 1, 1]
    assert (out[1][0]["old"], out[1][0]["new"]) == (345.0, 342.5)
    assert (out[2][0]["old"], out[2][0]["new"]) == (342.5, 340.0)
    last = out[3][0]
    assert last["path"] == [345.0, 342.5, 340.0, 345.0] and last["returned"] is True
    assert out[1][0]["returned"] is False
    assert state["call_wall"] == 345.0


def test_near_tie_does_not_alert_and_keeps_the_old_wall():
    reads = [_s("09:45", 345.0, {345.0: 2.0e6, 342.5: 1.9e6}),
             _s("10:00", 342.5, {345.0: 2.0e6, 342.5: 2.1e6})]   # only 5% bigger
    state, out = _run(reads)
    assert out[1] == [] and state["call_wall"] == 345.0


def test_old_wall_gone_from_the_read_counts_as_a_move():
    reads = [_s("09:45", 345.0, {345.0: 2.0e6}), _s("10:00", 340.0, {340.0: 0.5e6})]
    _, out = _run(reads)
    assert out[1][0]["new"] == 340.0 and out[1][0]["old_gex_now"] is None


def test_put_wall_uses_size_not_sign():
    reads = [_s("09:45", 345.0, {345.0: 2e6, 335.0: -1.0e6}, put_wall=335.0),
             _s("10:00", 345.0, {345.0: 2e6, 335.0: -0.8e6, 332.5: -1.5e6}, put_wall=332.5)]
    _, out = _run(reads)
    assert [(e["key"], e["old"], e["new"]) for e in out[1]] == [("put_wall", 335.0, 332.5)]


def test_new_day_or_new_expiry_resets_without_alert():
    s1 = _s("15:50", 345.0, {345.0: 2e6})
    state, _ = gs.detect(None, s1)
    state, ev = gs.detect(state, _s("09:45", 340.0, {340.0: 3e6}, day="2026-10-19",
                                    expiry="2026-10-19"))
    assert ev == [] and state["call_wall"] == 340.0
    state, ev = gs.detect(state, _s("16:20", 350.0, {350.0: 3e6}, day="2026-10-19",
                                    expiry="2026-10-20"))
    assert ev == [] and state["expiry"] == "2026-10-20"


def test_missing_read_or_missing_wall_changes_nothing():
    state, _ = gs.detect(None, _s("09:45", 345.0, {345.0: 2e6}))
    assert gs.detect(state, None) == (state, [])
    new, ev = gs.detect(state, _s("10:00", None, {}))
    assert ev == [] and new["call_wall"] == 345.0


def test_update_saves_per_ticker_state(tmp_path):
    p = str(tmp_path / "walls.json")
    assert gs.update("aapl", _s("09:45", 345.0, {345.0: 2e6}), path=p) == []
    ev = gs.update("AAPL", _s("10:30", 342.5, {345.0: 1e6, 342.5: 2e6}), path=p)
    assert ev[0]["new"] == 342.5
    assert gs.load_state(p)["AAPL"]["path"]["call_wall"] == [["09:45", 345.0], ["10:30", 342.5]]
    assert gs.update("SPY", None, path=p) == [] and "SPY" not in gs.load_state(p)


def test_unreadable_state_starts_fresh(tmp_path):
    p = tmp_path / "walls.json"
    p.write_text("{not json")
    assert gs.load_state(str(p)) == {}


def test_message_names_the_move_the_path_and_the_return():
    s = _s("14:00", 345.0, {340.0: 1.0e6, 345.0: 2.5e6})
    ev = [{"key": "call_wall", "label": "Call wall", "old": 340.0, "new": 345.0,
           "new_gex": 2.5e6, "old_gex_now": 1.0e6, "path": [345.0, 342.5, 340.0, 345.0],
           "returned": True}]
    text = gs.message("aapl", ev, s)
    assert "AAPL gamma shift" in text and "14:00 ET" in text
    assert "$340 → $345" in text and "back to an earlier level" in text
    assert "Today: $345 → $342.5 → $340 → $345" in text
    assert "2.50M" in text and "1.00M" in text


def test_summary_carries_every_strike():
    import gex
    from tests.test_gex import AS_OF, MONDAY, SPOT, _chain, _latest
    s = gex.summary(_latest(_chain()), spot=SPOT, as_of=AS_OF)
    m = gex.gex_matrix(gex.gex_table(_latest(_chain()), spot=SPOT, as_of=AS_OF),
                       spot=SPOT, expiries=[MONDAY])
    assert s["by_strike"] == pytest.approx(m[MONDAY].dropna().to_dict())


# ── scheduler wiring ────────────────────────────────────────────────────────

ENV = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
EVENT = [{"key": "call_wall", "label": "Call wall", "old": 345.0, "new": 342.5,
          "new_gex": 2e6, "old_gex_now": 1e6, "path": [345.0, 342.5], "returned": False}]


def _check(events, *, send):
    summary = _s("10:30", 342.5, {342.5: 2e6, 345.0: 1e6})
    with patch("telegram_bot._load_latest", return_value=({"spot": 343.2}, None)), \
         patch("plan_report.gamma_summary_for", return_value=summary), \
         patch("gamma_shift.update", return_value=events) as upd, \
         patch.object(sched, "_send_telegram", return_value=True) as send_tg:
        sched._gamma_shift_check("AAPL", ENV, send=send)
    upd.assert_called_once_with("AAPL", summary)
    return send_tg


def test_scheduler_sends_the_alert_when_a_wall_moves():
    send_tg = _check(EVENT, send=True)
    send_tg.assert_called_once()
    assert "$345 → $342.5" in send_tg.call_args[0][2]


def test_scheduler_records_but_stays_quiet_when_not_sending_or_nothing_moved():
    _check(EVENT, send=False).assert_not_called()
    _check([], send=True).assert_not_called()


def test_scheduler_check_never_raises():
    with patch("telegram_bot._load_latest", side_effect=RuntimeError("boom")):
        sched._gamma_shift_check("AAPL", ENV, send=True)


def test_ok_scan_runs_the_check_and_respects_the_switches():
    cfg = {"tickers": {"SPY": {"gamma_alert": False}}}
    with patch.object(sched, "_notify_success"), \
         patch.object(sched, "load_config", return_value=cfg), \
         patch.object(sched, "_gamma_shift_check") as chk:
        sched.handle_scan_result("AAPL", 0, None, 1.0, notify=True, env=ENV)
        sched.handle_scan_result("SPY", 0, None, 1.0, notify=True, env=ENV)
        sched.handle_scan_result("AAPL", 0, None, 1.0, notify=False, env=ENV)
        sched.handle_scan_result("AAPL", 3, "quality_gate", 1.0, notify=True, env=ENV)
    assert [c.kwargs["send"] for c in chk.call_args_list] == [True, False, False]
