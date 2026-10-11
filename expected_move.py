"""expected_move — the move option prices imply, for drawing on the chart.

    expected move = price of the at-the-money straddle for one expiry
                    (the call's mid plus the put's mid at the strike nearest the stock)

It is the distance the stock has to travel, by that expiry, before someone who bought
both options breaks even. Roughly 6 days in 10 the stock ends inside it (the straddle is
about 0.8 of one standard deviation).

The band is fixed for the day: it is taken from the last full-chain snapshot of the
previous weekday session at or before 16:00 ET, and centred on the stock price of that
scan. On an expiry day that makes it the range the market priced for today (0DTE). With
no earlier session on record, today's first snapshot is used and the label says so.

Display only; nothing here feeds scoring. No network: it reads the snapshots the scanner
already recorded and the spot stored in the matching scan archive.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import date, datetime
from typing import Any

import pandas as pd

import volume_history as vh

ARCHIVE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "archive")
NEAREST_STRIKES = 3          # strikes tried, nearest first, until both legs have a quote
SESSION_CLOSE = "16:00"
COLORS = ("#FFB74D", "#A1887F")     # nearest expiry, then the next


def _quote_mid(bid: Any, ask: Any) -> float | None:
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if not (b == b and a == a) or b <= 0 or a < b:
        return None
    return (b + a) / 2.0


def atm_straddle(chain: pd.DataFrame, spot: float, expiry: str) -> dict[str, Any] | None:
    """Call mid plus put mid at the strike nearest ``spot`` for ``expiry``. A strike
    without a two-sided quote on both legs is skipped; None after NEAREST_STRIKES."""
    if chain is None or chain.empty or not spot or spot <= 0:
        return None
    rows = chain[chain["expiry"].astype(str).str[:10] == str(expiry)[:10]]
    strikes = sorted({float(k) for k in rows["strike"].dropna()}, key=lambda k: (abs(k - spot), k))
    for strike in strikes[:NEAREST_STRIKES]:
        mids = {}
        for side in ("CALL", "PUT"):
            leg = rows[(rows["strike"] == strike) & (rows["side"].astype(str).str.upper() == side)]
            mids[side] = _quote_mid(leg["bid"].iloc[0], leg["ask"].iloc[0]) if len(leg) == 1 else None
        if mids["CALL"] is not None and mids["PUT"] is not None:
            return {"strike": strike, "call_mid": mids["CALL"], "put_mid": mids["PUT"],
                    "move": mids["CALL"] + mids["PUT"]}
    return None


def _reference_scan(con: sqlite3.Connection, ticker: str, today: date) -> tuple[str, str] | None:
    """(scan_id, ts_et) of the snapshot the band is set from."""
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT session_date FROM contract_scans WHERE ticker=? AND session_date<?"
        " ORDER BY session_date DESC LIMIT 10", (ticker, today.isoformat()))]
    for day in days:
        if date.fromisoformat(day).weekday() >= 5:          # a weekend re-scan: stale quotes
            continue
        row = con.execute(
            "SELECT scan_id, ts_et FROM contract_scans WHERE ticker=? AND session_date=?"
            " AND substr(ts_et, 12, 5) <= ? ORDER BY ts_et DESC LIMIT 1",
            (ticker, day, SESSION_CLOSE)).fetchone()
        if row:
            return row[0], row[1]
    row = con.execute(
        "SELECT scan_id, ts_et FROM contract_scans WHERE ticker=? AND session_date=?"
        " ORDER BY ts_et ASC LIMIT 1", (ticker, today.isoformat())).fetchone()
    return (row[0], row[1]) if row else None


def _archive_spot(scan_id: str, archive_dir: str | None) -> float | None:
    try:
        with open(os.path.join(archive_dir or ARCHIVE_DIR, f"{scan_id}.json"),
                  encoding="utf-8") as fh:
            spot = float(json.load(fh).get("spot"))
    except (OSError, ValueError, TypeError, AttributeError):
        return None
    return spot if spot > 0 else None


def expected_moves(ticker: str, today: date, *, db_path: str | None = None,
                   archive_dir: str | None = None, n: int = 2) -> list[dict[str, Any]]:
    """The band for the ``n`` nearest expiries on or after ``today``, nearest first.
    Empty when there is no snapshot, no stored spot for it, or no usable quotes."""
    path = db_path or vh.DB_PATH
    ticker = str(ticker or "").upper()
    if not os.path.exists(path):
        return []
    with closing(sqlite3.connect(path, timeout=30)) as con:
        ref = _reference_scan(con, ticker, today)
        if ref is None:
            return []
        chain = pd.read_sql_query(
            "SELECT side, strike, expiry, bid, ask FROM contract_scans"
            " WHERE ticker=? AND scan_id=? AND expiry>=?", con,
            params=(ticker, ref[0], today.isoformat()))
    spot = _archive_spot(ref[0], archive_dir)
    if spot is None or chain.empty:
        return []
    as_of = datetime.fromisoformat(ref[1])
    out: list[dict[str, Any]] = []
    for expiry in sorted(chain["expiry"].astype(str).str[:10].unique()):
        if len(out) >= n:
            break
        s = atm_straddle(chain, spot, expiry)
        if s is None:
            continue
        out.append({"expiry": expiry, "anchor": spot, "move": round(s["move"], 2),
                    "low": round(spot - s["move"], 2), "high": round(spot + s["move"], 2),
                    "strike": s["strike"], "as_of": as_of,
                    "same_day": as_of.date() == today})
    return out


def chart_levels(moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Two lines for each band, in the shape the price chart draws."""
    out = []
    for i, m in enumerate(moves):
        d = datetime.strptime(m["expiry"], "%Y-%m-%d")
        color = COLORS[min(i, len(COLORS) - 1)]
        for edge in ("high", "low"):
            out.append({"key": f"em_{edge}_{m['expiry']}",
                        "name": f"Exp move {d:%b} {d.day} {edge}", "price": float(m[edge]),
                        "color": color})
    return out


def caption(moves: list[dict[str, Any]]) -> str:
    """One plain line for under the chart. Dollar signs are not escaped here."""
    if not moves:
        return ""
    parts = []
    for m in moves:
        d = datetime.strptime(m["expiry"], "%Y-%m-%d")
        parts.append(f"{d:%b} {d.day} ±${m['move']:.2f} (${m['low']:.2f} to ${m['high']:.2f})")
    first = moves[0]
    when = (f"today's first snapshot at {first['as_of']:%H:%M}" if first["same_day"]
            else f"the {first['as_of']:%a %b} {first['as_of'].day} {first['as_of']:%H:%M} snapshot")
    return ("Expected move: " + " · ".join(parts) + f". At-the-money straddle from {when}, "
            f"centred on ${first['anchor']:.2f}. The stock ends inside it about 6 days in 10.")
