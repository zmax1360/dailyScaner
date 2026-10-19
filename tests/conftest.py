"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

GOLDEN_JOURNAL = Path(__file__).parent / "golden" / "journal"


@pytest.fixture
def golden_journal(monkeypatch) -> Path:
    """Point the journal reader at synthetic fixtures instead of data/journal/."""
    import scanner.journal_io as journal_io

    monkeypatch.setattr(journal_io, "JOURNAL_DIR", str(GOLDEN_JOURNAL))
    return GOLDEN_JOURNAL


@pytest.fixture(autouse=True)
def _no_real_send_records(tmp_path, monkeypatch):
    """Tests never write the real Telegram send record, gamma wall state or 11:00 setup
    record in data/."""
    import gamma_shift
    import setup_1100
    import telegram_log

    monkeypatch.setattr(telegram_log, "PATH", str(tmp_path / "telegram_sent.jsonl"))
    monkeypatch.setattr(gamma_shift, "STATE_PATH", str(tmp_path / "gamma_walls.json"))
    monkeypatch.setattr(setup_1100, "DB_PATH", str(tmp_path / "setup_1100.db"))
