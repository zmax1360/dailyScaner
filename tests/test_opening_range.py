"""20-minute opening range: the range, its width against recent days, the first
5-minute close outside it, the Game Plan line, the paper rule and the once-a-day alerts."""

from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from ticker_rules import opening_range as orng

ET = ZoneInfo("America/New_York")


def _day(day: str, opens: list[tuple[float, float, float, float]], *, fill: float = 100.0):
    """5-minute bars for one session: the given bars from 09:30, then flat bars at ``fill``
    up to 15:55. Index = bar start, naive ET wall clock (as ``market.build`` passes it)."""
    rows, t = [], pd.Timestamp(f"{day} 09:30")
    for o, h, l, c in opens:
        rows.append((t, o, h, l, c)); t += timedelta(minutes=5)
    while t.time() <= time(15, 55):
        rows.append((t, fill, fill + 0.05, fill - 0.05, fill)); t += timedelta(minutes=5)
    return pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close"]).set_index("ts")


def _history(width: float = 2.0, days: int = 10) -> pd.DataFrame:
    """``days`` past sessions whose first 20 minutes span exactly ``width``."""
    out = []
    for i in range(days):
        d = (pd.Timestamp("2026-10-01") + pd.offsets.BDay(i)).date().isoformat()
        out.append(_day(d, [(100, 100 + width / 2, 100 - width / 2, 100)] * 4))
    return pd.concat(out)


TODAY = "2026-11-06"
RANGE = [(100, 101, 99.5, 100.5), (100.5, 100.8, 99.2, 99.4),
         (99.4, 100.0, 99.0, 99.8), (99.8, 100.9, 99.7, 100.6)]    # 20 min: 99.0 - 101.0


def _bars(after: list[tuple[float, float, float, float]], *, hist_width: float = 2.0):
    return pd.concat([_history(hist_width), _day(TODAY, RANGE + after, fill=100.0)])


def _at(hm: str) -> datetime:
    h, m = (int(x) for x in hm.split(":"))
    return datetime(2026, 11, 6, h, m, tzinfo=ET)


def test_forming_until_the_twenty_minutes_are_complete():
    s = orng.summarize(_bars([]), _at("09:45"))
    assert s["status"] == orng.FORMING and s["ready_at"] == "09:50"
    assert s["width"] is None and s["high"] == 101.0
    assert "forming until 09:50" in orng.plan_line(s)
    assert orng.alert_events(s) == []


def test_range_width_and_the_wide_verdict():
    s = orng.summarize(_bars([]), _at("09:50"))
    assert s["status"] == orng.SET
    assert (s["high"], s["low"], s["width"]) == (101.0, 99.0, 2.0)
    assert s["avg_width"] == 2.0 and s["rel"] == 1.0 and s["wide"] is False
    wide = orng.summarize(_bars([], hist_width=1.0), _at("09:50"))
    assert wide["rel"] == 2.0 and wide["wide"] is True
    assert "Skip them" in orng.plan_line(wide)


def test_no_width_verdict_without_enough_history():
    bars = pd.concat([_history(2.0, days=3), _day(TODAY, RANGE)])
    s = orng.summarize(bars, _at("10:00"))
    assert s["rel"] is None and s["wide"] is None
    assert "not enough history" in orng.plan_line(s)


def test_first_close_outside_is_the_break_and_wicks_do_not_count():
    after = [(100.6, 101.4, 100.5, 100.9),      # 09:50 bar: wick above, close inside
             (100.9, 101.6, 100.8, 101.3)]      # 09:55 bar: closes above 101.0
    assert orng.summarize(_bars(after), _at("09:55"))["status"] == orng.SET
    s = orng.summarize(_bars(after), _at("10:00"))
    assert s["status"] == orng.BREAK_UP and s["break_at"] == "10:00"
    assert s["break_close"] == 101.3
    assert orng.alert_events(s) == ["set", "break"]
    down = orng.summarize(_bars([(99.5, 99.6, 98.5, 98.7)]), _at("09:55"))
    assert down["status"] == orng.BREAK_DOWN and down["break_at"] == "09:55"


def test_only_complete_bars_count():
    # the 09:50 bar would break, but at 09:54 it is still forming
    s = orng.summarize(_bars([(100.6, 102, 100.5, 101.8)]), _at("09:54"))
    assert s["status"] == orng.SET


def test_a_close_outside_after_the_cutoff_is_not_a_breakout():
    flat = [(100, 100.2, 99.8, 100)] * 20            # 09:50 .. 11:25 bars, inside
    late = flat + [(100, 102, 99.9, 101.8)]          # 11:30 bar closes outside at 11:35
    s = orng.summarize(_bars(late), _at("11:40"))
    assert s["status"] == orng.NO_BREAK and orng.alert_events(s) == ["set"]
    assert "no break by 11:30" in orng.plan_line(s)


def test_no_summary_before_the_open_or_without_bars():
    assert orng.summarize(_bars([]), _at("09:29")) is None
    assert orng.summarize(None, _at("10:00")) is None
    assert orng.summarize(pd.DataFrame(), _at("10:00")) is None


def test_alerts_go_once_a_day_each():
    s = orng.summarize(_bars([(100.6, 101.6, 100.5, 101.3)]), _at("10:00"))
    assert orng.due_events({}, "AAPL", s) == ["set", "break"]
    st = orng.mark_sent({}, "AAPL", s["day"], ["set"])
    assert orng.due_events(st, "AAPL", s) == ["break"]
    st = orng.mark_sent(st, "AAPL", s["day"], ["break"])
    assert orng.due_events(st, "AAPL", s) == []
    assert orng.due_events(st, "SPY", s) == ["set", "break"]          # per ticker
    assert orng.due_events({"AAPL": {"day": "2026-11-05", "sent": ["set", "break"]}},
                           "AAPL", s) == ["set", "break"]             # a new day resets


def test_messages_name_the_levels():
    s = orng.summarize(_bars([(100.6, 101.6, 100.5, 101.3)]), _at("10:00"))
    msg = orng.message("AAPL", s, "set")
    assert "$101.00" in msg and "$99.00" in msg and "1.0x normal width" in msg
    brk = orng.message("AAPL", s, "break")
    assert "broke UP" in brk and "calls" in brk and "$99.00" in brk
    wide = orng.summarize(_bars([(100.6, 101.6, 100.5, 101.3)], hist_width=1.0), _at("10:00"))
    assert "Plan:" not in orng.message("AAPL", wide, "break")


def test_market_context_carries_the_opening_range():
    from ticker_rules import market
    bars = _bars([(100.6, 101.6, 100.5, 101.3)])
    bars["Volume"] = 1000
    ctx = market.build(bars, _at("10:00"))
    assert ctx.orange["status"] == orng.BREAK_UP and ctx.orange["minutes"] == 20


class _Ctx:
    def __init__(self, orange, now):
        self.orange, self.now = orange, now


def test_paper_rule_follows_a_fresh_break_and_skips_wide_opens():
    from ticker_rules import rules
    s = orng.summarize(_bars([(100.6, 101.6, 100.5, 101.3)]), _at("10:00"))
    sig = rules.evaluate("orb_20", None, _Ctx(s, _at("10:05")))
    assert sig["side"] == rules.CALL and sig["stop_spot"] == 99.0
    assert rules.evaluate("orb_20", None, _Ctx(s, _at("10:15"))) is None     # stale break
    wide = orng.summarize(_bars([(100.6, 101.6, 100.5, 101.3)], hist_width=1.0), _at("10:00"))
    assert rules.evaluate("orb_20", None, _Ctx(wide, _at("10:00"))) is None
    assert rules.evaluate("orb_20", {"allow_wide": True}, _Ctx(wide, _at("10:00")))["side"] == "CALL"
    down = orng.summarize(_bars([(99.5, 99.6, 98.5, 98.7)]), _at("09:55"))
    assert rules.evaluate("orb_20", None, _Ctx(down, _at("09:55")))["side"] == rules.PUT
    assert rules.evaluate("orb_20", None, _Ctx(None, _at("10:00"))) is None
    assert "normal" in rules.RULES["orb_20"]["slots"]


def test_plan_report_reads_the_saved_range():
    import plan_report
    s = orng.summarize(_bars([]), _at("09:50"))
    payload = json.loads(json.dumps({"ticker_rules": {"opening_range": s}}))
    assert plan_report.saved_opening_range(payload)["high"] == 101.0
    assert plan_report.opening_range_line(payload).startswith("⏱ 20-min opening range")
    assert plan_report.saved_opening_range({}) is None
    assert plan_report.opening_range_line({"ticker_rules": None}) is None


def test_scheduler_sends_each_event_once(tmp_path, monkeypatch):
    import scheduler
    import telegram_bot
    s = orng.summarize(_bars([(100.6, 101.6, 100.5, 101.3)]), _at("10:00"))
    payload = json.loads(json.dumps({"ticker_rules": {"opening_range": s}}))
    sent = []
    monkeypatch.setattr(scheduler, "OPENING_RANGE_STATE", str(tmp_path / "or.json"))
    monkeypatch.setattr(telegram_bot, "_load_latest", lambda t: (payload, None))
    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    monkeypatch.setattr(scheduler, "_send_telegram",
                        lambda token, chat, text, **k: sent.append((k.get("kind"), text)) or True)
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "111"}
    scheduler._opening_range_alert("AAPL", env, send=True)
    assert [k for k, _ in sent] == ["opening_range", "opening_range"]
    assert "is set" in sent[0][1] and "broke UP" in sent[1][1]
    scheduler._opening_range_alert("AAPL", env, send=True)
    assert len(sent) == 2                                    # nothing twice
    assert json.loads((tmp_path / "or.json").read_text())["AAPL"]["sent"] == ["set", "break"]


def test_scheduler_does_not_mark_unsent_alerts(tmp_path, monkeypatch):
    import scheduler
    import telegram_bot
    s = orng.summarize(_bars([]), _at("09:50"))
    payload = json.loads(json.dumps({"ticker_rules": {"opening_range": s}}))
    monkeypatch.setattr(scheduler, "OPENING_RANGE_STATE", str(tmp_path / "or.json"))
    monkeypatch.setattr(telegram_bot, "_load_latest", lambda t: (payload, None))
    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    monkeypatch.setattr(scheduler, "_send_telegram", lambda *a, **k: False)
    scheduler._opening_range_alert("AAPL", {"TELEGRAM_BOT_TOKEN": "t",
                                            "TELEGRAM_CHAT_ID": "1"}, send=True)
    assert not (tmp_path / "or.json").exists()
    scheduler._opening_range_alert("AAPL", {}, send=False)        # muted: no error, no state
    assert not (tmp_path / "or.json").exists()


def test_opening_range_alert_can_be_turned_off():
    import scheduler
    assert scheduler.ticker_opening_range_alert({}, "AAPL") is True
    cfg = {"tickers": {"AAPL": {"opening_range_alert": False}}}
    assert scheduler.ticker_opening_range_alert(cfg, "AAPL") is False
