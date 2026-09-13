"""Journal-tab metrics from FIFO fills — never stored PnL."""

from __future__ import annotations

import pandas as pd
import pytest

from scanner.journal_io import FILL_COLS, list_journal_days
from scanner.journal_view import (
    closed_on_day,
    concat_all_fills,
    load_all_day_frames,
    metrics_from_match,
    positions_frame,
    try_match,
)
from scanner.lot_match import closed_trades, exit_event_rollup, open_inventory


def _fills(*rows) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=FILL_COLS)


def _row(action, qty, price, at, *, side="CALL", strike=310.0, ticker="AAPL"):
    return {
        "Action": action,
        "Ticker": ticker,
        "Side": side,
        "Strike": strike,
        "Expiry": "2026-08-21",
        "Quantity": qty,
        "Price": price,
        "At": at,
        "Source": "discretionary",
        "Timestamp_Quality": "approximate",
    }


def test_closed_count_is_rollup_not_lot_count():
    df = _fills(
        _row("BUY", 3, 0.13, "2026-08-21T10:39:21-04:00", side="PUT", strike=307.5),
        _row("SELL", 3, 0.15, "2026-08-21T10:44:00-04:00", side="PUT", strike=307.5),
    )
    closed = closed_trades(df)
    open_ = open_inventory(df)
    assert len(closed) == 3
    rolled = exit_event_rollup(closed)
    assert len(rolled) == 1
    stats = metrics_from_match(closed, open_)
    assert stats["n_closed"] == 1
    assert stats["n_lots"] == 3
    assert stats["n_open"] == 0
    assert stats["total_realized_pnl"] == pytest.approx(6.0)
    # PnL_Pct is a fraction, not whole percent (15.38).
    assert stats["avg_pnl_pct"] == pytest.approx(0.1538)
    assert abs(stats["avg_pnl_pct"]) < 2
    assert stats["win_rate"] == pytest.approx(1.0)


def test_pnl_pct_fraction_not_whole_percent():
    df = _fills(
        _row("BUY", 1, 0.21, "2026-08-21T15:40:00-04:00"),
        _row("SELL", 1, 0.30, "2026-08-21T15:50:21-04:00"),
    )
    stats = metrics_from_match(closed_trades(df), open_inventory(df))
    assert stats["avg_pnl_pct"] == pytest.approx(0.4286)
    assert stats["total_realized_pnl"] == pytest.approx(9.0)
    pos = positions_frame(closed_trades(df), open_inventory(df))
    assert len(pos) == 1
    assert float(pos.iloc[0]["PnL_Pct"]) == pytest.approx(0.4286)
    assert pos.iloc[0]["Status"] == "CLOSED"


def test_positions_open_and_closed_from_fifo():
    df = _fills(
        _row("BUY", 1, 0.25, "2026-08-21T15:39:00-04:00"),
        _row("BUY", 1, 0.15, "2026-08-21T15:41:00-04:00"),
        _row("SELL", 1, 0.21, "2026-08-21T15:45:00-04:00"),
    )
    closed = closed_trades(df)
    open_ = open_inventory(df)
    pos = positions_frame(closed, open_)
    assert set(pos["Status"]) == {"CLOSED", "OPEN"}
    assert int((pos["Status"] == "OPEN").sum()) == 1
    open_row = pos[pos["Status"] == "OPEN"].iloc[0]
    assert pd.isna(open_row["PnL_Dollars"]) or open_row["PnL_Dollars"] is None
    assert open_row["Sold_Price"] is None or pd.isna(open_row["Sold_Price"])
    stats = metrics_from_match(closed, open_)
    assert stats["n_open"] == 1
    assert stats["n_closed"] == 1
    assert stats["unrealized_pnl"] == 0.0


def test_oversell_is_not_swallowed():
    df = _fills(
        _row("BUY", 1, 0.25, "2026-08-21T15:39:00-04:00"),
        _row("SELL", 1, 0.21, "2026-08-21T15:45:00-04:00"),
        _row("SELL", 1, 0.30, "2026-08-21T15:50:00-04:00"),
    )
    with pytest.raises(ValueError, match="oversell"):
        closed_trades(df)
    closed, open_, err = try_match(df)
    assert err is not None
    assert "oversell" in err
    assert "AAPL" in err
    assert "CALL" in err
    assert closed.empty
    assert open_.empty


def test_overnight_position_slices_by_exit_date_not_per_file_match():
    """Buy Mon / sell Tue is a false oversell if FIFO runs on Tuesday alone."""
    monday = _fills(
        _row("BUY", 1, 1.00, "2026-08-24T15:00:00-04:00"),
    )
    tuesday = _fills(
        _row("SELL", 1, 1.50, "2026-08-25T10:00:00-04:00"),
    )
    with pytest.raises(ValueError, match="oversell"):
        closed_trades(tuesday)

    all_fills = concat_all_fills([monday, tuesday])
    closed, open_, err = try_match(all_fills)
    assert err is None
    assert open_.empty
    stats = metrics_from_match(closed, open_)
    assert stats["n_closed"] == 1
    assert stats["n_open"] == 0
    assert stats["total_realized_pnl"] == pytest.approx(50.0)
    assert stats["wins"] == 1

    tue_lots = closed_on_day(closed, "2026-08-25")
    mon_lots = closed_on_day(closed, "2026-08-24")
    assert len(tue_lots) == 1
    assert tue_lots.iloc[0]["PnL_Dollars"] == pytest.approx(50.0)
    assert mon_lots.empty


def test_concat_metrics_ignore_stored_pnl_columns():
    """Fills must not carry stored PnL into the metrics path."""
    df = _fills(
        _row("BUY", 1, 1.00, "2026-08-25T10:00:00-04:00"),
        _row("SELL", 1, 0.50, "2026-08-25T11:00:00-04:00"),
    )
    poisoned = df.copy()
    poisoned["PnL_Dollars"] = 9999.0
    poisoned["PnL_Pct"] = 99.0
    closed = closed_trades(poisoned[FILL_COLS])
    stats = metrics_from_match(closed, open_inventory(poisoned[FILL_COLS]))
    assert stats["total_realized_pnl"] == pytest.approx(-50.0)
    assert stats["avg_pnl_pct"] == pytest.approx(-0.5)


def test_live_journal_concat_fifo_expected_totals():
    days = list_journal_days()
    if not days:
        pytest.skip("no journal day files")
    frames = load_all_day_frames()
    fills = concat_all_fills(frames)
    closed, open_, err = try_match(fills)
    assert err is None, err
    stats = metrics_from_match(closed, open_)
    pos = positions_frame(closed, open_)
    if stats["n_closed"] < 20:
        pytest.skip("journal too small for rebuilt-broker expected totals")
    assert stats["n_open"] == 0
    assert int((pos["Status"] == "OPEN").sum()) == 0
    assert stats["n_closed"] == 289
    assert stats["wins"] == 102
    assert stats["total_realized_pnl"] == pytest.approx(-1591.29)
    assert stats["n_closed"] == len(exit_event_rollup(closed))
    assert stats["n_closed"] != stats["n_lots"]
    assert abs(stats["avg_pnl_pct"] or 0) < 2  # fraction, not whole percent
    # Per-day view is a slice of the concat match, not a second FIFO.
    sliced_pnl = 0.0
    for d in days:
        day_lots = closed_on_day(closed, d)
        if not day_lots.empty:
            sliced_pnl += float(day_lots["PnL_Dollars"].sum())
    assert sliced_pnl == pytest.approx(stats["total_realized_pnl"])
