"""Step 1 — sources.base Protocol schema (validate_chain only)."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from sources.base import CHAIN_COLUMNS, validate_chain


def _row(**overrides):
    base = {
        "side": "CALL",
        "strike": 340.0,
        "expiry": "2026-08-21",
        "dte": 28.0,
        "bid": 1.0,
        "ask": 1.1,
        "last": 1.05,
        "volume": 100.0,
        "openInterest": 500.0,
        "iv": 0.30,
        "delta": float("nan"),
    }
    base.update(overrides)
    return base


def _frame(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=CHAIN_COLUMNS)


def test_validate_chain_rejects_missing_column():
    df = _frame([_row()])
    df = df.drop(columns=["delta"])
    with pytest.raises(ValueError, match="columns must be exactly"):
        validate_chain(df)


def test_validate_chain_rejects_wrong_order():
    df = _frame([_row()])
    df = df[["delta"] + [c for c in CHAIN_COLUMNS if c != "delta"]]
    with pytest.raises(ValueError, match="columns must be exactly"):
        validate_chain(df)


def test_validate_chain_accepts_nan_delta():
    df = _frame([_row(delta=float("nan"))])
    out = validate_chain(df)
    assert list(out.columns) == CHAIN_COLUMNS
    assert math.isnan(float(out.loc[0, "delta"]))


def test_validate_chain_rejects_zero_substituted_for_missing_iv():
    df = _frame([_row(iv=0.0)])
    with pytest.raises(ValueError, match="iv must not use 0"):
        validate_chain(df)
