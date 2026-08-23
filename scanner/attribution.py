"""
Journal attribution on closed lots.

Input is always closed_trades(load_journal_day(day)) — never raw events
and never stored PnL_Dollars on a SELL row. Source is a groupable
dimension so discretionary fills can be dropped from scanner measurement.
"""

from __future__ import annotations

from typing import Iterable

import pandas as pd

from scanner.journal_io import load_journal_day
from scanner.lot_match import closed_trades, exit_event_rollup


def closed_for_day(day: str) -> pd.DataFrame:
    return closed_trades(load_journal_day(day))


def exclude_discretionary(trades: pd.DataFrame) -> pd.DataFrame:
    """Keep scanner-sourced lots only. Missing Source is kept (legacy)."""
    if trades is None or trades.empty or "Source" not in trades.columns:
        return trades.copy() if trades is not None else pd.DataFrame()
    src = trades["Source"].fillna("").astype(str).str.lower()
    return trades[src.isin(("", "scanner"))].copy()


def summarize(
    trades: pd.DataFrame,
    *,
    by: Iterable[str] = ("Source",),
) -> pd.DataFrame:
    """Aggregate closed lots. PnL_Pct stays a fraction."""
    cols = ["n", "qty", "pnl_dollars", "pnl_pct"]
    if trades is None or trades.empty:
        return pd.DataFrame(columns=list(by) + cols)
    keys = [k for k in by if k in trades.columns]
    if not keys:
        keys = [c for c in ("Source",) if c in trades.columns]
    if not keys:
        return pd.DataFrame([{
            "n": int(len(trades)),
            "qty": float(trades["Quantity"].sum()),
            "pnl_dollars": round(float(trades["PnL_Dollars"].sum()), 2),
            "pnl_pct": None,
        }])
    rows = []
    for key, g in trades.groupby(keys, dropna=False, sort=False):
        key_t = key if isinstance(key, tuple) else (key,)
        rec = dict(zip(keys, key_t))
        rec["n"] = int(len(g))
        rec["qty"] = float(g["Quantity"].sum())
        rec["pnl_dollars"] = round(float(g["PnL_Dollars"].sum()), 2)
        # Dollar-weighted fraction, not a mean of percents.
        notional = (g["Entry_Price"] * g["Quantity"] * 100.0).sum()
        rec["pnl_pct"] = (
            round(float(g["PnL_Dollars"].sum()) / float(notional), 4)
            if notional else None
        )
        rows.append(rec)
    return pd.DataFrame(rows)


def day_summary(day: str, *, scanner_only: bool = False) -> pd.DataFrame:
    trades = closed_for_day(day)
    if scanner_only:
        trades = exclude_discretionary(trades)
    return summarize(trades, by=("Source",))


def day_acb(day: str) -> pd.DataFrame:
    """Per-exit-event average of closed lots (not a full-day ACB)."""
    return exit_event_rollup(closed_for_day(day))
