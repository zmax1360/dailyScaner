"""Journal fills load; P&L comes from lot_match, not stored columns."""

from __future__ import annotations

from scanner.attribution import closed_for_day, exclude_discretionary, summarize
from scanner.journal_io import load_journal_day
from scanner.lot_match import closed_trades


def test_aug21_trade_schema_loads_as_fills():
    df = load_journal_day("2026-08-21")
    assert not df.empty
    assert set(df["Action"]) == {"BUY", "SELL"}
    assert (df["Ticker"] == "AAPL").all()
    assert "PnL_Dollars" not in df.columns
    trades = closed_trades(df)
    assert trades["PnL_Dollars"].sum() == 26.0


def test_aug17_fills_match_per_lot():
    df = load_journal_day("2026-08-17")
    trades = closed_trades(df)
    # PUT 3× +$2 and CALL -4 / +9 / +15
    assert trades["PnL_Dollars"].sum() == 26.0


def test_attribution_groups_source():
    trades = closed_for_day("2026-08-17")
    assert "Source" in trades.columns
    summary = summarize(trades, by=("Source",))
    assert "pnl_dollars" in summary.columns
    scanner_only = exclude_discretionary(trades)
    assert len(scanner_only) <= len(trades)
