"""A ticker can be scanned and recorded without Telegram messages."""

from __future__ import annotations

from pathlib import Path

import scheduler

ROOT = Path(__file__).resolve().parents[1]


def test_per_ticker_notify_switch():
    cfg = {"notify_telegram": True,
           "tickers": {"AAPL": {"interval_min": 5}, "SPY": {"interval_min": 15, "notify": False}}}
    assert scheduler.ticker_notifies(cfg, "AAPL") is True
    assert scheduler.ticker_notifies(cfg, "SPY") is False
    assert scheduler.ticker_notifies(cfg, "QQQ") is True          # not listed: default on


def test_global_switch_still_wins():
    cfg = {"notify_telegram": False, "tickers": {"AAPL": {"notify": True}}}
    assert scheduler.ticker_notifies(cfg, "AAPL") is False
    assert scheduler.ticker_notifies({}, "AAPL") is True


def test_both_scan_paths_use_the_switch():
    src = (ROOT / "scheduler.py").read_text()
    assert src.count("notify=ticker_notifies(cfg, ticker)") == 2
    assert "notify=notify," not in src


# ── a Telegram chat per ticker ──────────────────────────────────────────────

def test_a_ticker_with_its_own_chat_uses_it():
    from scheduler import ticker_chat_id
    env = {"TELEGRAM_CHAT_ID": "111", "TELEGRAM_CHAT_ID_SPY": "-100222"}
    assert ticker_chat_id(env, "SPY") == "-100222"
    assert ticker_chat_id(env, "spy") == "-100222"
    assert ticker_chat_id(env, "AAPL") == "111"


def test_an_empty_own_chat_falls_back_to_the_shared_one():
    from scheduler import ticker_chat_id
    assert ticker_chat_id({"TELEGRAM_CHAT_ID": "111", "TELEGRAM_CHAT_ID_SPY": " "}, "SPY") == "111"
    assert ticker_chat_id({}, "SPY") == ""


def test_scan_message_goes_to_the_tickers_chat(monkeypatch):
    import scheduler
    import telegram_bot
    sent = []
    monkeypatch.setattr(scheduler, "_send_telegram",
                        lambda token, chat, text: sent.append(chat) or tokens.append(token) or True)
    tokens = []
    monkeypatch.setattr(telegram_bot, "_load_latest", lambda t: ({"spot": 1.0}, None))
    monkeypatch.setattr(telegram_bot, "_fmt_report", lambda *a, **k: "report")
    monkeypatch.setattr(scheduler, "load_config", lambda: {"tickers": {"SPY": {"own_chat": True}}})
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "111",
           "TELEGRAM_CHAT_ID_SPY": "-100222", "TELEGRAM_BOT_TOKEN_SPY": "spybot"}
    scheduler._notify_success("SPY", env, 1.0)
    scheduler._notify_success("AAPL", env, 1.0)
    assert sent == ["-100222", "111"] and tokens == ["spybot", "t"]
    env.pop("TELEGRAM_CHAT_ID_SPY")
    scheduler._notify_success("SPY", env, 1.0)                   # no own chat yet: skipped
    assert sent == ["-100222", "111"]


def test_settings_push_uses_the_same_per_ticker_chat(tmp_path, monkeypatch):
    import ui.telegram_push as tp
    env = tmp_path / ".env"
    env.write_text('TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=111\nTELEGRAM_CHAT_ID_SPY="-100222"\n'
                   'TELEGRAM_BOT_TOKEN_SPY=spybot\n')
    monkeypatch.setattr(tp, "_ENV_FILE", str(env))
    assert tp._load_telegram_config() == ("tok", "111")
    assert tp._load_telegram_config("SPY") == ("spybot", "-100222")
    assert tp._load_telegram_config("AAPL") == ("tok", "111")


def test_own_chat_ticker_never_falls_back_to_the_shared_chat():
    from scheduler import ticker_chat_id
    cfg = {"tickers": {"SPY": {"own_chat": True}}}
    assert ticker_chat_id({"TELEGRAM_CHAT_ID": "111"}, "SPY", cfg) == ""
    assert ticker_chat_id({"TELEGRAM_CHAT_ID": "111", "TELEGRAM_CHAT_ID_SPY": "-1"}, "SPY", cfg) == "-1"
    assert ticker_chat_id({"TELEGRAM_CHAT_ID": "111"}, "AAPL", cfg) == "111"


def test_shipped_config_sends_spy_only_to_its_own_channel():
    cfg = scheduler.load_config()
    assert cfg["tickers"]["SPY"] == {"interval_min": 15, "own_chat": True}
    assert scheduler.ticker_notifies(cfg, "SPY") is True


def test_a_ticker_with_its_own_bot_uses_it():
    from scheduler import ticker_bot_token
    env = {"TELEGRAM_BOT_TOKEN": "shared", "TELEGRAM_BOT_TOKEN_SPY": "spybot"}
    assert ticker_bot_token(env, "SPY") == "spybot"
    assert ticker_bot_token(env, "AAPL") == "shared"
    assert ticker_bot_token({"TELEGRAM_BOT_TOKEN": "shared", "TELEGRAM_BOT_TOKEN_SPY": ""}, "SPY") == "shared"
