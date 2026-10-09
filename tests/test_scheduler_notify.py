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
