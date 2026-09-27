"""Cursor rules and their Copilot mirrors must not drift."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_copilot_instructions_match_cursor_rules():
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "sync_agent_rules.py"), "--check"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, (
        "Run `python scripts/sync_agent_rules.py` after editing .cursor/rules/:\n" + r.stdout
    )


def test_always_on_rules_stay_small():
    words = len((ROOT / "AGENTS.md").read_text().split())
    assert words < 900, f"AGENTS.md is {words} words; it loads into every request — move detail to .cursor/rules/"
