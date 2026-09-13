"""
Journal-tab derivations from fill files.

FIFO via scanner.lot_match. Never reads stored PnL_Dollars / PnL_Pct.
Does not touch scoring.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from scanner.journal_io import FILL_COLS, list_journal_days, load_journal_day
from scanner.lot_match import (
    CLOSED_COLS,
    OPEN_COLS,
    closed_trades,
    exit_event_rollup,
    open_inventory,
)

ET = ZoneInfo("America/New_York")

POSITION_COLS = [
    "Status", "Ticker", "Side", "Strike", "Expiry", "Quantity",
    "Bought_At", "Bought_Price", "Sold_At", "Sold_Price",
    "PnL_Pct", "PnL_Dollars",
]


def today_et() -> str:
    return datetime.now(ET).date().isoformat()


def load_all_day_frames() -> dict[str, pd.DataFrame]:
    """YYYY-MM-DD → fill frame. Uses scanner.journal_io only."""
    return {d: load_journal_day(d) for d in list_journal_days()}


def concat_all_fills(frames: list[pd.DataFrame] | dict[str, pd.DataFrame]) -> pd.DataFrame:
    if isinstance(frames, dict):
        seq = list(frames.values())
    else:
        seq = list(frames)
    nonempty = [f for f in seq if f is not None and not getattr(f, "empty", True)]
    if not nonempty:
        return pd.DataFrame(columns=FILL_COLS)
    return pd.concat(nonempty, ignore_index=True)


def try_match(
    fills: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, str | None]:
    """
    FIFO match over the given fill frame (caller concatenates every day).
    On oversell, return empty frames plus the error string.
    Does not swallow — caller must render the error.
    """
    empty_c = pd.DataFrame(columns=CLOSED_COLS)
    empty_o = pd.DataFrame(columns=OPEN_COLS)
    if fills is None or getattr(fills, "empty", True):
        return empty_c, empty_o, None
    try:
        return closed_trades(fills), open_inventory(fills), None
    except ValueError as exc:
        return empty_c, empty_o, str(exc)


def closed_on_day(closed: pd.DataFrame, day: str) -> pd.DataFrame:
    """Lots whose Exit_At calendar day is ``day``. No new FIFO match."""
    if closed is None or getattr(closed, "empty", True) or "Exit_At" not in closed.columns:
        return pd.DataFrame(columns=CLOSED_COLS)
    day_s = str(day).strip()[:10]
    key = closed["Exit_At"].astype(str).str[:10]
    return closed.loc[key == day_s].reset_index(drop=True)


def metrics_from_match(
    closed: pd.DataFrame,
    open_: pd.DataFrame,
) -> dict[str, Any]:
    """
    Metrics for the Journal tab.

    Displayed counts, win rate, and avg PnL % come from exit_event_rollup
    (one row per position). Dollar totals match 1-lot FIFO.
    PnL_Pct is a fraction (0.4286), not whole percent.
    """
    rolled = exit_event_rollup(closed)
    n_lots = int(len(closed)) if closed is not None and not closed.empty else 0
    n_closed = int(len(rolled))
    n_open = int(len(open_)) if open_ is not None and not open_.empty else 0

    wins = losses = 0
    total_pnl = 0.0
    pcts: list[float] = []
    if n_closed:
        for _, r in rolled.iterrows():
            d = r.get("PnL_Dollars")
            p = r.get("PnL_Pct")
            if d is not None and not (isinstance(d, float) and pd.isna(d)):
                total_pnl += float(d)
                if float(d) > 0:
                    wins += 1
                elif float(d) < 0:
                    losses += 1
            if p is not None and not (isinstance(p, float) and pd.isna(p)):
                pcts.append(float(p))
    elif n_lots:
        total_pnl = round(float(closed["PnL_Dollars"].sum()), 2)

    return {
        "n_closed": n_closed,
        "n_lots": n_lots,
        "n_open": n_open,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / n_closed) if n_closed else None,
        "total_realized_pnl": round(total_pnl, 2),
        "avg_pnl_pct": round(sum(pcts) / len(pcts), 6) if pcts else None,
        "unrealized_pnl": 0.0,
    }


def positions_frame(
    closed: pd.DataFrame,
    open_: pd.DataFrame,
) -> pd.DataFrame:
    """CLOSED rows from exit_event_rollup; OPEN rows from open_inventory."""
    rows: list[dict[str, Any]] = []
    rolled = exit_event_rollup(closed)
    if rolled is not None and not rolled.empty:
        for _, r in rolled.iterrows():
            rows.append({
                "Status": "CLOSED",
                "Ticker": r.get("Ticker"),
                "Side": r.get("Side"),
                "Strike": r.get("Strike"),
                "Expiry": r.get("Expiry"),
                "Quantity": r.get("Quantity"),
                "Bought_At": r.get("Entry_At"),
                "Bought_Price": r.get("Entry_Price"),
                "Sold_At": r.get("Exit_At"),
                "Sold_Price": r.get("Exit_Price"),
                "PnL_Pct": r.get("PnL_Pct"),
                "PnL_Dollars": r.get("PnL_Dollars"),
            })
    if open_ is not None and not open_.empty:
        for _, r in open_.iterrows():
            rows.append({
                "Status": "OPEN",
                "Ticker": r.get("Ticker"),
                "Side": r.get("Side"),
                "Strike": r.get("Strike"),
                "Expiry": r.get("Expiry"),
                "Quantity": r.get("Quantity"),
                "Bought_At": r.get("Entry_At"),
                "Bought_Price": r.get("Entry_Price"),
                "Sold_At": None,
                "Sold_Price": None,
                "PnL_Pct": None,
                "PnL_Dollars": None,
            })
    if not rows:
        return pd.DataFrame(columns=POSITION_COLS)
    out = pd.DataFrame(rows)[POSITION_COLS]
    sort_key = out["Sold_At"].fillna(out["Bought_At"]).fillna("")
    return (
        out.assign(_sk=sort_key)
        .sort_values("_sk", ascending=False)
        .drop(columns=["_sk"])
        .reset_index(drop=True)
    )


def day_fill_counts(df: pd.DataFrame) -> tuple[int, int]:
    if df is None or getattr(df, "empty", True):
        return 0, 0
    action = df["Action"].astype(str).str.upper()
    return int((action == "BUY").sum()), int((action == "SELL").sum())
