"""The 11:00 setup (setup_1100): decision, paper exit, store, messages, page table."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import setup_1100 as su

ET = ZoneInfo("America/New_York")
DAY = "2026-10-20"


def at(hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    return datetime(2026, 10, 20, h, m, tzinfo=ET)


def payload(spot=345.0, emas=(345.2, 344.9, 344.5), ts="11:02"):
    e9, e21, e50 = emas
    return {"spot": spot, "timestamp": at(ts).isoformat(),
            "timeframes": {"15M": {"ema9": e9, "ema21": e21, "ema50": e50}}}


def chain(rows=None):
    rows = rows or [
        ("CALL", 342.5, DAY, 2.60, 2.65), ("CALL", 345.0, DAY, 0.60, 0.63),
        ("CALL", 347.5, DAY, 0.10, 0.12), ("PUT", 345.0, DAY, 0.58, 0.61),
        ("PUT", 342.5, DAY, 0.08, 0.10), ("CALL", 345.0, "2026-10-21", 1.40, 1.45),
    ]
    return pd.DataFrame(rows, columns=["side", "strike", "expiry", "bid", "ask"])


BEAR = (344.5, 344.9, 345.2)
MIXED = (345.0, 344.9, 345.2)


# ── decide ─────────────────────────────────────────────────────────────────────
def test_trades_a_call_when_emas_and_morning_agree():
    r = su.decide(payload(), chain(), ticker="aapl", now=at("11:02"), morning=343.0)
    assert r["action"] == su.TRADE and r["side"] == "CALL"
    assert (r["strike"], r["expiry"], r["entry"]) == (345.0, DAY, 0.63)   # ATM, nearest, ask
    assert (r["target"], r["stop"]) == (0.78, 0.53)
    assert r["exit_by"] == at("11:32").isoformat() and r["status"] == su.OPEN


def test_trades_a_put_when_both_point_down():
    r = su.decide(payload(spot=344.0, emas=BEAR), chain(), ticker="AAPL", now=at("11:02"),
                  morning=346.0)
    assert r["action"] == su.TRADE and r["side"] == "PUT" and r["strike"] == 345.0


@pytest.mark.parametrize("emas,morning,why", [
    (MIXED, 343.0, "not stacked"),
    ((345.2, 344.9, 344.5), 346.0, "disagree"),     # EMAs up, price below the morning
    (BEAR, 343.0, "disagree"),                     # EMAs down, price above the morning
    ((345.2, 344.9, 344.5), 345.0, "disagree"),     # price exactly at the reference
    ((345.2, 344.9, 344.5), None, "morning reference"),
])
def test_no_trade_cases(emas, morning, why):
    r = su.decide(payload(emas=emas), chain(), ticker="AAPL", now=at("11:02"), morning=morning)
    assert r["action"] == su.NO_TRADE and why in r["reason"] and r["side"] is None


def test_no_trade_on_wide_spread_or_tiny_ask():
    wide = chain([("CALL", 345.0, DAY, 0.55, 0.65)])
    assert "Spread" in su.decide(payload(), wide, ticker="AAPL", now=at("11:02"),
                                 morning=343.0)["reason"]
    tiny = chain([("CALL", 345.0, DAY, 0.08, 0.10)])
    assert "under" in su.decide(payload(), tiny, ticker="AAPL", now=at("11:02"),
                                morning=343.0)["reason"]


def test_nearest_expiry_skips_expired_and_picks_atm():
    c = chain([("CALL", 345.0, "2026-10-19", 0.01, 0.02), ("CALL", 344.0, "2026-10-21", 1.9, 1.95),
               ("CALL", 346.0, "2026-10-21", 1.0, 1.04)])
    k = su.pick_contract(c, "CALL", 345.2, DAY)
    assert k["expiry"] == "2026-10-21" and k["strike"] == 346.0


# ── update ─────────────────────────────────────────────────────────────────────
def _open():
    return su.decide(payload(), chain(), ticker="AAPL", now=at("11:02"), morning=343.0)


def test_target_is_recorded_at_the_target():
    r = su.update(_open(), 0.85, at("11:12"))
    assert r["status"] == su.WIN and r["pnl"] == 15 and r["exit_price"] == 0.78


def test_stop_is_recorded_at_the_bid_that_hit_it():
    r = su.update(_open(), 0.45, at("11:12"))
    assert r["status"] == su.LOSS_STOP and r["pnl"] == pytest.approx(-18)


def test_time_exit_at_the_bid_after_30_minutes():
    r = su.update(_open(), 0.70, at("11:20"))
    assert r["status"] == su.OPEN and r["last_bid"] == 0.70
    r = su.update(r, 0.68, at("11:33"))
    assert r["status"] == su.TIME_EXIT and r["pnl"] == pytest.approx(5)


def test_missing_quote_at_exit_uses_the_last_bid():
    r = su.update(su.update(_open(), 0.66, at("11:10")), None, at("11:35"))
    assert r["status"] == su.TIME_EXIT and r["pnl"] == pytest.approx(3)


# ── on_scan and the store ──────────────────────────────────────────────────────
def _archive(tmp_path, hm, spot):
    d = tmp_path / "archive"
    d.mkdir(exist_ok=True)
    (d / f"AAPL_20261020_{hm}.json").write_text(json.dumps({"spot": spot}))
    return str(d)


def test_on_scan_decides_once_then_resolves(tmp_path):
    db = str(tmp_path / "s.db")
    _archive(tmp_path, "093500", 341.0)                 # before 09:45: not the reference
    arc = _archive(tmp_path, "094600", 343.0)
    kw = dict(ticker="AAPL", archive_dir=arc, db_path=db)
    assert su.on_scan(payload(), chain(), now=at("10:55"), **kw) is None
    ev = su.on_scan(payload(), chain(), now=at("11:02"), **kw)
    assert ev["event"] == "decided" and ev["trade"]["morning_spot"] == 343.0
    ev = su.on_scan(payload(), chain([("CALL", 345.0, DAY, 0.70, 0.72)]), now=at("11:07"), **kw)
    assert ev["event"] is None and ev["trade"]["status"] == su.OPEN
    ev = su.on_scan(payload(), chain([("CALL", 345.0, DAY, 0.80, 0.82)]), now=at("11:12"), **kw)
    assert ev["event"] == "closed" and ev["trade"]["status"] == su.WIN
    ev = su.on_scan(payload(), chain(), now=at("11:17"), **kw)
    assert ev["event"] is None and ev["trade"]["status"] == su.WIN
    h = su.history("AAPL", db_path=db)
    assert len(h) == 1 and su.stats(h)["total"] == 15


def test_on_scan_records_a_missed_day(tmp_path):
    db = str(tmp_path / "s.db")
    ev = su.on_scan(payload(), chain(), ticker="AAPL", now=at("11:15"), db_path=db,
                    archive_dir=str(tmp_path))
    assert ev["trade"]["action"] == su.MISSED
    assert su.stats(su.history("AAPL", db_path=db))["missed"] == 1


def test_on_scan_ignores_other_tickers_and_naive_times(tmp_path):
    db = str(tmp_path / "s.db")
    assert su.on_scan(payload(), chain(), ticker="SPY", now=at("11:02"), db_path=db) is None
    assert su.on_scan(payload(), chain(), ticker="AAPL", now=at("11:02").replace(tzinfo=None),
                      db_path=db) is None


def test_stats_empty_and_mixed():
    assert su.stats(pd.DataFrame())["closed"] == 0
    h = pd.DataFrame([
        {"action": su.TRADE, "status": su.WIN, "pnl": 15.0},
        {"action": su.TRADE, "status": su.LOSS_STOP, "pnl": -17.0},
        {"action": su.TRADE, "status": su.OPEN, "pnl": None},
        {"action": su.NO_TRADE, "status": None, "pnl": None},
    ])
    s = su.stats(h)
    assert (s["trades"], s["closed"], s["wins"], s["win_pct"], s["total"]) == (3, 2, 1, 50, -2)


# ── messages and the page ──────────────────────────────────────────────────────
def test_messages():
    r = _open()
    msg = su.message({"event": "decided", "trade": r})
    assert "Buy 1 AAPL 345 call exp Oct 20" in msg and "$0.63" in msg and "11:32" in msg
    closed = su.update(r, 0.85, at("11:12"))
    assert "target hit +15 USD" in su.message({"event": "closed", "trade": closed})
    no = su.decide(payload(emas=MIXED), chain(), ticker="AAPL", now=at("11:02"), morning=343.0)
    assert "No trade today" in su.message({"event": "decided", "trade": no})
    assert su.message({"event": None, "trade": r}) is None and su.message(None) is None


def test_page_table_reads_in_words():
    from ui.pages import setup_1100 as page

    r = su.update(_open(), 0.85, at("11:12"))
    no = su.decide(payload(emas=MIXED), chain(), ticker="AAPL", now=at("11:02"), morning=343.0)
    no["day"] = "2026-10-21"
    t = page.table(pd.DataFrame([no, r]))
    assert list(t["Decision"]) == ["No trade", "Call"]
    assert t.iloc[1]["Contract"] == "345 exp Oct 20" and t.iloc[1]["P&L"] == "+15"
    assert t.iloc[1]["Entry"] == "$0.63 at 11:02" and t.iloc[1]["Result"] == "✅ target"


def test_scheduler_sends_only_decisions_and_exits(monkeypatch):
    import scheduler

    sent = []
    monkeypatch.setattr(scheduler, "_send_telegram",
                        lambda tok, chat, text, **k: sent.append((k["kind"], text)) or True)
    monkeypatch.setattr(scheduler, "ticker_bot_token", lambda env, t: "tok")
    monkeypatch.setattr(scheduler, "ticker_chat_id", lambda env, t, cfg=None: "chat")
    monkeypatch.setattr(scheduler, "load_config", lambda: {})
    import telegram_bot

    ev = {"event": "decided", "trade": _open()}
    monkeypatch.setattr(telegram_bot, "_load_latest", lambda t: ({"setup_1100": ev}, None))
    scheduler._setup_alert("AAPL", {}, send=True)
    assert sent and sent[0][0] == "setup_1100" and "Buy 1" in sent[0][1]
    sent.clear()
    monkeypatch.setattr(telegram_bot, "_load_latest",
                        lambda t: ({"setup_1100": {"event": None, "trade": ev["trade"]}}, None))
    scheduler._setup_alert("AAPL", {}, send=True)
    assert not sent
    assert scheduler.ticker_setup_alert({"tickers": {"AAPL": {"setup_alert": False}}}, "AAPL") is False


def test_tests_never_write_the_real_record():
    assert su.DB_PATH != su.os.path.join(su._BASE, "data", "setup_1100.db")
