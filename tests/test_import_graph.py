"""Architecture guard: yfinance may only be imported by allowlisted modules."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Until CURSOR_SOURCES_STEPS Step 5, these modules may still import yfinance.
_YF_ALLOWED = frozenset({
    "sources/yahoo.py",
    "weekly.py",
    "volume_analysis.py",
    "cost_distribution.py",
    "news_service.py",
})

_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "__pycache__", "backups", "archive",
    "archive_weekly", "data", "files", "node_modules",
})


def _iter_project_py() -> list[Path]:
    out: list[Path] = []
    for path in ROOT.rglob("*.py"):
        rel_parts = path.relative_to(ROOT).parts
        if any(p in _SKIP_DIRS for p in rel_parts):
            continue
        # Untracked scratch / demos
        if path.name in {"massive_test.py", "dashboard.py"}:
            continue
        out.append(path)
    return out


def _imports_yfinance(path: Path) -> bool:
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(src, filename=str(path))
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "yfinance":
                    return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "yfinance":
                return True
    return False


def test_yfinance_import_graph_allowlist():
    """
    yfinance must be reachable only from sources.yahoo plus Step-5 exceptions.

    Replaces the old app.py source-text grep, which missed transitive imports
    through dailyScaner / data_adapter.
    """
    offenders: list[str] = []
    for path in _iter_project_py():
        rel = path.relative_to(ROOT).as_posix()
        if not _imports_yfinance(path):
            continue
        if rel in _YF_ALLOWED:
            continue
        offenders.append(rel)
    assert not offenders, (
        "yfinance import outside allowlist "
        f"(sources/yahoo.py + Step-5 exceptions):\n"
        + "\n".join(sorted(offenders))
    )


def test_app_does_not_import_yfinance_directly():
    app = ROOT / "app.py"
    assert app.is_file()
    assert not _imports_yfinance(app)
