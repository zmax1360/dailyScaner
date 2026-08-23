"""FIFO lot match and journal I/O boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scanner.journal_io import FILL_COLS, load_journal_day
from scanner.lot_match import closed_trades, exit_event_rollup, open_inventory


def _fills(*rows) -> pd.DataFrame:
    cols = [
        "Action", "Ticker", "Side", "Strike", "Expiry",
        "Quantity", "Price", "At", "Source", "Timestamp_Quality",
    ]
    return pd.DataFrame(list(rows), columns=cols)


def _row(action, qty, price, at, *, side="CALL", strike=310.0):
    return {
        "Action": action,
        "Ticker": "AAPL",
        "Side": side,
        "Strike": strike,
        "Expiry": "2026-08-21",
        "Quantity": qty,
        "Price": price,
        "At": at,
        "Source": "discretionary",
        "Timestamp_Quality": "approximate",
    }


def test_multi_lot_exit():
    df = _fills(
        _row("BUY", 1, 0.25, "2026-08-21T15:39:21-04:00"),
        _row("BUY", 1, 0.21, "2026-08-21T15:40:00-04:00"),
        _row("BUY", 1, 0.15, "2026-08-21T15:41:00-04:00"),
        _row("SELL", 1, 0.21, "2026-08-21T15:45:21-04:00"),
        _row("SELL", 2, 0.30, "2026-08-21T15:50:21-04:00"),
    )
    trades = closed_trades(df)
    assert len(trades) == 3
    assert list(trades["PnL_Dollars"]) == [-4.0, 9.0, 15.0]
    assert trades["PnL_Dollars"].sum() == 20.0
    assert list(trades["PnL_Pct"]) == pytest.approx([-0.16, 0.4286, 1.0])
    assert all(abs(p) <= 2 for p in trades["PnL_Pct"])  # fraction, not 42.86
    roll = exit_event_rollup(trades)
    assert roll["PnL_Dollars"].sum() == pytest.approx(20.0)


def test_uniform_lots():
    df = _fills(
        _row("BUY", 3, 0.13, "2026-08-21T10:39:21-04:00", side="PUT", strike=307.5),
        _row("SELL", 3, 0.15, "2026-08-21T10:44:00-04:00", side="PUT", strike=307.5),
    )
    trades = closed_trades(df)
    assert len(trades) == 3
    assert list(trades["PnL_Dollars"]) == [2.0, 2.0, 2.0]
    assert trades["PnL_Dollars"].sum() == 6.0
    assert list(trades["PnL_Pct"]) == pytest.approx([0.1538, 0.1538, 0.1538])


def test_phantom_detection():
    df = _fills(
        _row("BUY", 1, 0.25, "2026-08-21T15:39:00-04:00"),
        _row("BUY", 1, 0.21, "2026-08-21T15:40:00-04:00"),
        _row("BUY", 1, 0.15, "2026-08-21T15:41:00-04:00"),
        _row("SELL", 1, 0.21, "2026-08-21T15:45:00-04:00"),
        _row("SELL", 1, 0.30, "2026-08-21T15:50:00-04:00"),
    )
    inv = open_inventory(df)
    assert len(inv) == 1
    assert float(inv.iloc[0]["Entry_Price"]) == pytest.approx(0.15)


def test_oversell_raises():
    df = _fills(
        _row("BUY", 1, 0.25, "2026-08-21T15:39:00-04:00"),
        _row("SELL", 1, 0.21, "2026-08-21T15:45:00-04:00"),
        _row("SELL", 1, 0.30, "2026-08-21T15:50:00-04:00"),
    )
    with pytest.raises(ValueError, match="oversell"):
        closed_trades(df)


def test_legacy_file_drops_stored_pnl():
    raw = json.loads(Path("data/journal/2026-07-28.json").read_text())
    assert any("PnL_Dollars" in row for row in raw)
    df = load_journal_day("2026-07-28")
    assert not df.empty
    for col in ("PnL_Pct", "PnL_Dollars", "Entry_Price"):
        assert col not in df.columns
    assert list(df.columns) == FILL_COLS
    assert set(df["Action"]) == {"BUY", "SELL"}


def test_unparseable_at_does_not_raise():
    df = _fills(
        _row("BUY", 1, 0.25, "2026-08-21T15:39:21-04:00"),
        _row("BUY", 1, 0.21, "unknown"),
        _row("SELL", 1, 0.30, "2026-08-21T15:50:21-04:00"),
    )
    trades = closed_trades(df)
    assert len(trades) == 1
