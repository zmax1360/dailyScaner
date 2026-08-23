"""
Daily journal fill I/O. Disk only — never computes P&L.

Writes store Action, Ticker, Side, Strike, Expiry, Quantity, Price, At,
plus Source and Timestamp_Quality. Entry_Price / PnL_* are not written.

Reads tolerate legacy files that still have those columns: they are
ignored, never migrated, never used as P&L.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import date
from typing import Any

import pandas as pd

log = logging.getLogger("scanner.journal_io")

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_DIR = os.path.join(_BASE, "data", "journal")

DAY_FILE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")

FILL_COLS = [
    "Action", "Ticker", "Side", "Strike", "Expiry",
    "Quantity", "Price", "At", "Source", "Timestamp_Quality",
]
WRITE_COLS = tuple(FILL_COLS)
LEGACY_DROP = frozenset({"Entry_Price", "PnL_Pct", "PnL_Dollars",
                         "entry_price", "pnl_pct", "pnl_dollars"})


def journal_path_for_day(day: str | date) -> str:
    if isinstance(day, date):
        day_s = day.isoformat()
    else:
        day_s = str(day).strip()[:10]
    return os.path.join(JOURNAL_DIR, f"{day_s}.json")


def _pick(row: dict[str, Any], *names):
    for name in names:
        if name in row and row[name] is not None:
            return row[name]
    lower = {str(k).lower(): v for k, v in row.items()}
    for name in names:
        val = lower.get(str(name).lower())
        if val is not None:
            return val
    return None


def _is_closed_trade_record(row: dict[str, Any]) -> bool:
    if _pick(row, "Action", "action"):
        return False
    return _pick(row, "trade_id", "entry_price", "exit_price") is not None


def _fill_dict(
    *,
    action: str,
    ticker,
    side,
    strike,
    expiry,
    quantity,
    price,
    at,
    source=None,
    timestamp_quality=None,
) -> dict[str, Any]:
    return {
        "Action": str(action or "").upper(),
        "Ticker": str(ticker or "").upper(),
        "Side": str(side or "").upper(),
        "Strike": strike,
        "Expiry": expiry,
        "Quantity": quantity,
        "Price": price,
        "At": at,
        "Source": source,
        "Timestamp_Quality": timestamp_quality,
    }


def _records_to_fills(rows: list[Any]) -> list[dict[str, Any]]:
    """Map on-disk objects to fill rows. No price arithmetic."""
    out: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        source = _pick(raw, "Source", "source")
        tq = _pick(raw, "Timestamp_Quality", "timestamp_quality")
        if _is_closed_trade_record(raw):
            ticker = _pick(raw, "Ticker", "ticker")
            side = _pick(raw, "Side", "side")
            strike = _pick(raw, "Strike", "strike")
            expiry = _pick(raw, "Expiry", "expiry")
            qty = _pick(raw, "Quantity", "quantity")
            entry = _pick(raw, "entry_price")
            exit_px = _pick(raw, "exit_price")
            entry_at = _pick(raw, "entry_at", "Entry_At", "At")
            exit_at = _pick(raw, "exit_at", "Exit_At") or entry_at
            if entry is not None:
                out.append(_fill_dict(
                    action="BUY", ticker=ticker, side=side, strike=strike,
                    expiry=expiry, quantity=qty, price=entry, at=entry_at,
                    source=source, timestamp_quality=tq,
                ))
            if exit_px is not None:
                out.append(_fill_dict(
                    action="SELL", ticker=ticker, side=side, strike=strike,
                    expiry=expiry, quantity=qty, price=exit_px, at=exit_at,
                    source=source, timestamp_quality=tq,
                ))
            continue
        out.append(_fill_dict(
            action=_pick(raw, "Action", "action") or "",
            ticker=_pick(raw, "Ticker", "ticker"),
            side=_pick(raw, "Side", "side"),
            strike=_pick(raw, "Strike", "strike"),
            expiry=_pick(raw, "Expiry", "expiry"),
            quantity=_pick(raw, "Quantity", "quantity"),
            price=_pick(raw, "Price", "price"),
            at=_pick(raw, "At", "at"),
            source=source,
            timestamp_quality=tq,
        ))
    return out


def _read_raw(day: str) -> list[Any]:
    path = journal_path_for_day(day)
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        rows = json.load(fh)
    return rows if isinstance(rows, list) else []


def _write_raw(day: str, rows: list[Any]) -> None:
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    path = journal_path_for_day(day)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(rows, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _fill_for_write(fill: dict[str, Any]) -> dict[str, Any]:
    src = _pick(fill, "Source", "source")
    tq = _pick(fill, "Timestamp_Quality", "timestamp_quality")
    out = _fill_dict(
        action=_pick(fill, "Action", "action") or "",
        ticker=_pick(fill, "Ticker", "ticker"),
        side=_pick(fill, "Side", "side"),
        strike=_pick(fill, "Strike", "strike"),
        expiry=_pick(fill, "Expiry", "expiry"),
        quantity=_pick(fill, "Quantity", "quantity"),
        price=_pick(fill, "Price", "price"),
        at=_pick(fill, "At", "at"),
        source=src,
        timestamp_quality=tq,
    )
    return {k: out[k] for k in WRITE_COLS}


def list_journal_days() -> list[str]:
    """YYYY-MM-DD days that have a journal file, newest first."""
    if not os.path.isdir(JOURNAL_DIR):
        return []
    days: list[str] = []
    for name in os.listdir(JOURNAL_DIR):
        if not name.endswith(".json"):
            continue
        if DAY_FILE_RE.match(name):
            days.append(name[:10])
        else:
            log.warning("skipping non-day journal file: %s", name)
    return sorted(days, reverse=True)


def load_journal_day(day: str) -> pd.DataFrame:
    """Raw fills for one day. No P&L columns."""
    try:
        raw = _read_raw(day)
    except Exception:
        log.warning("failed to read journal day %s", day, exc_info=True)
        return pd.DataFrame(columns=FILL_COLS)
    fills = _records_to_fills(raw)
    if not fills:
        return pd.DataFrame(columns=FILL_COLS)
    df = pd.DataFrame(fills)
    for col in FILL_COLS:
        if col not in df.columns:
            df[col] = None
    # Never leak legacy P&L / entry columns into the fill frame.
    drop = [c for c in df.columns if c in LEGACY_DROP or c not in FILL_COLS]
    if drop:
        df = df.drop(columns=drop)
    return df[FILL_COLS]


def append_fills(day: str, fills: list[dict[str, Any]]) -> str:
    """
    Append fill dicts. Creates the day file if absent.
    Does not rewrite existing rows (no migration).
    Warns if lot_match.open_inventory() is non-empty after the write.
    """
    day_s = str(day).strip()[:10]
    existing = _read_raw(day_s)
    for fill in fills:
        existing.append(_fill_for_write(fill))
    _write_raw(day_s, existing)

    from scanner.lot_match import open_inventory

    inv = open_inventory(load_journal_day(day_s))
    if inv is not None and not inv.empty:
        log.warning(
            "open inventory after append_fills(%s): %s lot(s) unmatched",
            day_s, len(inv),
        )
    return day_s
