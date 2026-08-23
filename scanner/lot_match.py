"""
FIFO lot matching for journal fills.

Pure functions. Never touches disk. Never writes P&L back onto event rows.

closed_trades() is the source of truth for attribution (one row per
matched entry/exit lot). exit_event_rollup() averages lots that share
an exit print — not a full-day adjusted cost base.

PnL_Pct is a fraction (0.4286), never whole percent (42.86).
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any

import pandas as pd

log = logging.getLogger("scanner.lot_match")
_UNPARSEABLE_TS = datetime.min.replace(tzinfo=timezone.utc)

CONTRACT_MULT = 100
PNL_PCT_DECIMALS = 4

CLOSED_COLS = [
    "Ticker", "Side", "Strike", "Expiry", "Quantity",
    "Entry_Price", "Exit_Price", "Entry_At", "Exit_At",
    "PnL_Pct", "PnL_Dollars", "Source", "Timestamp_Quality",
]
OPEN_COLS = [
    "Ticker", "Side", "Strike", "Expiry", "Quantity",
    "Entry_Price", "Entry_At", "Source", "Timestamp_Quality",
]


def _empty(cols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(columns=cols)


def _f(x) -> float | None:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v


def _contract_key(row: Any) -> tuple:
    try:
        strike = round(float(row.get("Strike") or 0), 4)
    except (TypeError, ValueError):
        strike = 0.0
    return (
        str(row.get("Ticker") or "").upper(),
        str(row.get("Side") or "").upper(),
        strike,
        str(row.get("Expiry") or ""),
    )


def _unit_qtys(qty: float) -> list[float]:
    """Split an integer-valued quantity into 1-lot units."""
    n = int(round(qty))
    if n >= 1 and abs(qty - n) < 1e-9:
        return [1.0] * n
    return [float(qty)] if qty > 0 else []


def _row_label(row: dict[str, Any] | None) -> str:
    if not row:
        return "?"
    return (
        f"{row.get('Action')} {row.get('Ticker')} {row.get('Side')} "
        f"{row.get('Strike')} {row.get('Expiry')} qty={row.get('Quantity')} "
        f"px={row.get('Price')} at={row.get('At')}"
    )


def _ts_sort_key(at, seq: int, row: dict[str, Any] | None = None) -> tuple:
    """Always (aware datetime, seq). Unparseable At → datetime.min UTC."""
    s = "" if at is None or (isinstance(at, float) and pd.isna(at)) else str(at)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (dt, seq)
    except Exception:
        log.warning("unparseable At %r on fill %s", at, _row_label(row))
        return (_UNPARSEABLE_TS, seq)


def _pnl(entry: float, exit_px: float, qty: float) -> tuple[float, float]:
    pct = round((exit_px - entry) / entry, PNL_PCT_DECIMALS) if entry else 0.0
    dollars = round((exit_px - entry) * qty * CONTRACT_MULT, 2)
    return pct, dollars


def _iter_fills(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    raw = [dict(r) for _, r in df.reset_index(drop=True).iterrows()]
    indexed = list(enumerate(raw))
    indexed.sort(
        key=lambda pair: _ts_sort_key(pair[1].get("At"), pair[0], pair[1]),
    )
    return [r for _, r in indexed]


def _match(df: pd.DataFrame) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """FIFO match. Raises ValueError on oversell. Returns (closed, open)."""
    lots: dict[tuple, deque] = defaultdict(deque)
    closed: list[dict[str, Any]] = []

    for row in _iter_fills(df):
        action = str(row.get("Action") or "").upper()
        qty = _f(row.get("Quantity")) or 0.0
        px = _f(row.get("Price"))
        if px is None or qty <= 0:
            log.warning(
                "skipping fill: Price=%r Quantity=%r (%s)",
                row.get("Price"), row.get("Quantity"), _row_label(row),
            )
            continue
        key = _contract_key(row)
        src = row.get("Source")
        tq = row.get("Timestamp_Quality")
        if action == "BUY":
            for u in _unit_qtys(qty):
                lots[key].append({
                    "qty": u,
                    "price": px,
                    "at": row.get("At"),
                    "source": src,
                    "tq": tq,
                    "ticker": key[0],
                    "side": key[1],
                    "strike": key[2],
                    "expiry": key[3],
                })
            continue
        if action != "SELL":
            log.warning(
                "skipping fill: Action=%r is not BUY or SELL (%s)",
                row.get("Action"), _row_label(row),
            )
            continue
        remaining = list(_unit_qtys(qty))
        if not remaining:
            remaining = [qty]
        for take in remaining:
            q = lots[key]
            if not q:
                raise ValueError(
                    f"oversell {key[0]} {key[1]} {key[2]} {key[3]}: "
                    f"no open lot for sell {take} @ {px}"
                )
            lot = q[0]
            if take > float(lot["qty"]) + 1e-12:
                raise ValueError(
                    f"oversell {key[0]} {key[1]} {key[2]} {key[3]}: "
                    f"need {take}, open lot has {lot['qty']}"
                )
            use = min(float(lot["qty"]), take)
            entry = float(lot["price"])
            pct, dollars = _pnl(entry, px, use)
            closed.append({
                "Ticker": lot["ticker"],
                "Side": lot["side"],
                "Strike": lot["strike"],
                "Expiry": lot["expiry"],
                "Quantity": use,
                "Entry_Price": entry,
                "Exit_Price": px,
                "Entry_At": lot["at"],
                "Exit_At": row.get("At"),
                "PnL_Pct": pct,
                "PnL_Dollars": dollars,
                "Source": lot["source"] if lot["source"] is not None else src,
                "Timestamp_Quality": lot["tq"] if lot["tq"] is not None else tq,
            })
            lot["qty"] = float(lot["qty"]) - use
            if lot["qty"] <= 1e-12:
                q.popleft()

    open_rows: list[dict[str, Any]] = []
    for q in lots.values():
        for lot in q:
            if float(lot["qty"]) <= 1e-12:
                continue
            open_rows.append({
                "Ticker": lot["ticker"],
                "Side": lot["side"],
                "Strike": lot["strike"],
                "Expiry": lot["expiry"],
                "Quantity": lot["qty"],
                "Entry_Price": lot["price"],
                "Entry_At": lot["at"],
                "Source": lot["source"],
                "Timestamp_Quality": lot["tq"],
            })
    return closed, open_rows


def closed_trades(df: pd.DataFrame) -> pd.DataFrame:
    """One row per matched entry/exit lot. FIFO. Raises on oversell."""
    closed, _ = _match(df)
    if not closed:
        return _empty(CLOSED_COLS)
    return pd.DataFrame(closed)[CLOSED_COLS]


def open_inventory(df: pd.DataFrame) -> pd.DataFrame:
    """Unmatched long lots remaining after FIFO. Raises on oversell."""
    _, open_rows = _match(df)
    if not open_rows:
        return _empty(OPEN_COLS)
    return pd.DataFrame(open_rows)[OPEN_COLS]


def exit_event_rollup(trades: pd.DataFrame) -> pd.DataFrame:
    """
    Average entry across lots that share the same exit print
    (Ticker, Side, Strike, Expiry, Exit_At, Exit_Price).

    This is per-exit-event averaging, not a full-day adjusted cost base.
    Dollar totals still match FIFO; attribution across those lots is blended.
    """
    if trades is None or getattr(trades, "empty", True):
        return _empty(CLOSED_COLS)
    keys = ["Ticker", "Side", "Strike", "Expiry", "Exit_At", "Exit_Price"]
    extra = [c for c in ("Source", "Timestamp_Quality") if c in trades.columns]
    rows: list[dict[str, Any]] = []
    for _, g in trades.groupby(keys + extra, dropna=False, sort=False):
        qty = float(g["Quantity"].sum())
        cost = float((g["Entry_Price"] * g["Quantity"]).sum())
        entry = cost / qty if qty else 0.0
        exit_px = float(g["Exit_Price"].iloc[0])
        dollars = round(float(g["PnL_Dollars"].sum()), 2)
        pct, _ = _pnl(entry, exit_px, qty) if entry else (None, dollars)
        first = g.iloc[0]
        rows.append({
            "Ticker": first["Ticker"],
            "Side": first["Side"],
            "Strike": first["Strike"],
            "Expiry": first["Expiry"],
            "Quantity": qty,
            "Entry_Price": entry,
            "Exit_Price": exit_px,
            "Entry_At": first.get("Entry_At"),
            "Exit_At": first["Exit_At"],
            "PnL_Pct": pct,
            "PnL_Dollars": dollars,
            "Source": first.get("Source") if "Source" in g.columns else None,
            "Timestamp_Quality": (
                first.get("Timestamp_Quality")
                if "Timestamp_Quality" in g.columns else None
            ),
        })
    return pd.DataFrame(rows)[CLOSED_COLS]
