#!/usr/bin/env python3
"""Mirror .cursor/rules/*.mdc (source of truth) to .github/instructions/*.instructions.md.

Cursor reads .cursor/rules; GitHub Copilot reads .github/instructions. Same rules, two formats.
Edit the .mdc files only, then run:   python scripts/sync_agent_rules.py
Check without writing (used by tests): python scripts/sync_agent_rules.py --check
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / ".cursor" / "rules"
DST = ROOT / ".github" / "instructions"
HEADER = "<!-- GENERATED from .cursor/rules/{name}.mdc by scripts/sync_agent_rules.py — do not edit -->\n"
FRONT = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


def _field(front: str, key: str) -> str | None:
    m = re.search(rf"^{key}:\s*(.*)$", front, re.M)
    return m.group(1).strip() if m else None


def render(mdc: Path) -> tuple[Path, str] | None:
    m = FRONT.match(mdc.read_text())
    if not m:
        raise SystemExit(f"{mdc}: missing frontmatter")
    front, body = m.groups()
    globs = _field(front, "globs")
    if not globs:  # always-apply / agent-requested rules have no Copilot path equivalent
        return None
    desc = _field(front, "description") or mdc.stem
    out = f'---\napplyTo: "{globs}"\ndescription: {desc}\n---\n{HEADER.format(name=mdc.stem)}{body}'
    return DST / f"{mdc.stem}.instructions.md", out


def main(argv: list[str]) -> int:
    check = "--check" in argv
    wanted = {p: s for p, s in filter(None, (render(f) for f in sorted(SRC.glob("*.mdc"))))}
    stale = [p for p in DST.glob("*.instructions.md")
             if p not in wanted and "GENERATED from .cursor/rules" in p.read_text()]
    drift = [p for p, s in wanted.items() if not p.exists() or p.read_text() != s] + stale
    if check:
        for p in drift:
            print(f"out of sync: {p.relative_to(ROOT)}")
        return 1 if drift else 0
    DST.mkdir(parents=True, exist_ok=True)
    for p, s in wanted.items():
        p.write_text(s)
    for p in stale:
        p.unlink()
    print(f"synced {len(wanted)} rule(s), removed {len(stale)} stale")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
