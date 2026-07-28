"""Backstop for F-17: no naive datetime.now() / date.today() in scan paths."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_MODULES = (
    "dailyScaner.py",
    "weekly.py",
    "snapshot_store.py",
    "data_adapter.py",
)
_NAIVE = re.compile(r"datetime\.now\(\s*\)|date\.today\(\s*\)")


@pytest.mark.parametrize("filename", _MODULES)
def test_no_naive_local_now_or_today(filename: str):
    text = (_ROOT / filename).read_text(encoding="utf-8")
    hits = [
        (i + 1, line.rstrip())
        for i, line in enumerate(text.splitlines())
        if _NAIVE.search(line) and not line.lstrip().startswith("#")
    ]
    assert hits == [], f"{filename} still has naive local time:\n" + "\n".join(
        f"  L{n}: {line}" for n, line in hits
    )
