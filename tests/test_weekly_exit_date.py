"""Hard-exit date must never be on or before entry (optionlab start < target)."""

from datetime import datetime

import weekly

ASOF = datetime(2026, 8, 31)
EXP  = "2026-10-02"


def test_past_earnings_treated_as_unknown(monkeypatch):
    monkeypatch.setitem(weekly.EARNINGS, "AAPL", datetime(2026, 7, 30))
    date, days = weekly.earnings_check("AAPL", today=ASOF)
    assert date is None
    assert days is None


def test_future_earnings_still_counted(monkeypatch):
    monkeypatch.setitem(weekly.EARNINGS, "AAPL", datetime(2026, 10, 30))
    date, days = weekly.earnings_check("AAPL", today=ASOF)
    assert date == "2026-10-30"
    assert days == 60


def test_past_earnings_exit_is_expiration():
    assert weekly._compute_exit_date("2026-07-30", EXP, today=ASOF) == EXP


def test_none_earnings_exit_is_expiration():
    assert weekly._compute_exit_date(None, EXP, today=ASOF) == EXP


def test_earnings_during_option_life_uses_day_before():
    assert weekly._compute_exit_date("2026-09-18", EXP, today=ASOF) == "2026-09-17"


def test_earnings_after_expiration_uses_expiration():
    assert weekly._compute_exit_date("2026-10-30", EXP, today=ASOF) == EXP


def test_earnings_tomorrow_falls_back_to_expiration():
    # day-before-earnings would be entry day; optionlab requires start < target
    assert weekly._compute_exit_date("2026-09-01", EXP, today=ASOF) == EXP
