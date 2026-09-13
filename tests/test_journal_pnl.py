"""Journal fills load; P&L comes from lot_match, not stored columns."""

from __future__ import annotations

from scanner.attribution import closed_for_day, exclude_discretionary, summarize
from scanner.journal_io import load_journal_day


def test_aug21_trade_schema_loads_as_fills():
    df = load_journal_day("2026-08-21")
    assert not df.empty
    assert set(df["Action"]) == {"BUY", "SELL"}
    assert (df["Ticker"] == "AAPL").all()
    assert "PnL_Dollars" not in df.columns


def test_aug17_fills_have_no_stored_pnl():
    df = load_journal_day("2026-08-17")
    assert not df.empty
    assert "PnL_Dollars" not in df.columns
    assert "PnL_Pct" not in df.columns


def test_attribution_groups_source():
    trades = closed_for_day("2026-08-17")
    assert "Source" in trades.columns
    summary = summarize(trades, by=("Source",))
    assert "pnl_dollars" in summary.columns
    scanner_only = exclude_discretionary(trades)
    assert len(scanner_only) <= len(trades)
