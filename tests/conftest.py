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
