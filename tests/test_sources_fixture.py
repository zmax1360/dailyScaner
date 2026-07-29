"""Step 3 — FixtureSource + network-free test guard."""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from sources.base import CHAIN_COLUMNS, MarketDataSource
from sources.fixture import FixtureSource

GOLDEN = Path(__file__).resolve().parent / "golden"
ARCHIVE = GOLDEN / "AAPL_20260728_093102.json"
TESTS_DIR = Path(__file__).resolve().parent

# Direct imports forbidden in test modules (Step 3). Importing sources.yahoo
# is fine — that module may use yfinance; tests must not call the network.
_FORBIDDEN_MODULES = frozenset({"yfinance", "requests"})


def test_fixture_source_serves_recorded_chain():
    src = FixtureSource(ARCHIVE)
    assert isinstance(src, MarketDataSource)
    assert src.name == "fixture"
    assert src.volume_is_session_scoped is False
    df = src.fetch_chain("AAPL", max_dte=45)
    assert list(df.columns) == CHAIN_COLUMNS
    assert not df.empty
    assert set(df["side"].unique()) <= {"CALL", "PUT"}
    assert src.fetch_spot("AAPL") == pytest.approx(341.78)


def test_fixture_source_is_deterministic():
    src = FixtureSource(ARCHIVE)
    a = src.fetch_chain("AAPL", max_dte=30)
    b = src.fetch_chain("AAPL", max_dte=30)
    pd.testing.assert_frame_equal(a, b)

    # Same path again — new instance, identical frame
    src2 = FixtureSource(ARCHIVE)
    c = src2.fetch_chain("AAPL", max_dte=30)
    pd.testing.assert_frame_equal(a, c)


def test_fixture_accepts_dict_payload():
    payload = FixtureSource(ARCHIVE).payload
    src = FixtureSource(payload)
    df = src.fetch_chain("AAPL", max_dte=7)
    assert not df.empty
    assert (df["dte"] <= 7).all()


def test_no_test_reaches_network():
    """Test modules must not import yfinance or requests directly."""
    offenders: list[str] = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError as exc:
            offenders.append(f"{path.relative_to(TESTS_DIR)}: syntax {exc}")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in _FORBIDDEN_MODULES:
                        offenders.append(
                            f"{path.relative_to(TESTS_DIR)}: import {alias.name}"
                        )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    root = node.module.split(".")[0]
                    if root in _FORBIDDEN_MODULES:
                        offenders.append(
                            f"{path.relative_to(TESTS_DIR)}: from {node.module}"
                        )
    assert not offenders, (
        "tests must not import yfinance/requests directly:\n"
        + "\n".join(offenders)
    )
