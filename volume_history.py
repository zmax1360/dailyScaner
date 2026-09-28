"""volume_history — per-scan record of every traded contract (volume, OI, quotes, IV).

Archives keep only the top 30 calls/puts per scan, so contracts that build quietly in later
expiries were never saved. The scanner already downloads the full chain; this module stores
every contract with volume > 0 after a scan passes its quality gates.

Rules:
  * Missing values are stored as NULL, never 0.
  * Recording is fail-soft: ``record_scan`` never raises into the scanner.
  * Display/analysis only. Not a scoring input.

Storage: SQLite at data/volume_history.db (WAL). One row per contract per scan.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any, Iterable

import pandas as pd

log = logging.getLogger(__name__)

_BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE, "data", "volume_history.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS contract_scans (
    ticker        TEXT NOT NULL,
    scan_id       TEXT NOT NULL,     -- archive file name of the scan
    ts_et         TEXT NOT NULL,     -- ISO, offset-aware
    session_date  TEXT NOT NULL,     -- YYYY-MM-DD (ET)
    side          TEXT NOT NULL,     -- CALL | PUT
    strike        REAL NOT NULL,
    expiry        TEXT NOT NULL,     -- YYYY-MM-DD
    volume        REAL,              -- cumulative for the session; NULL = unknown
    open_interest REAL,              -- as reported (updates once a day); NULL = unknown
    bid REAL, ask REAL, last REAL, iv REAL,
    source        TEXT,
    PRIMARY KEY (ticker, scan_id, side, strike, expiry)
);
CREATE INDEX IF NOT EXISTS ix_contract ON contract_scans (ticker, side, strike, expiry, ts_et);
CREATE INDEX IF NOT EXISTS ix_session  ON contract_scans (ticker, session_date);
"""

_COLS = {  # legacy scanner leg column -> table column
    "volume": "volume", "openInterest": "open_interest", "bid": "bid", "ask": "ask",
    "lastPrice": "last", "impliedVolatility": "iv",
}


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(_SCHEMA)
    return con


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _rows(leg: pd.DataFrame | None, side: str) -> Iterable[dict]:
    if leg is None or getattr(leg, "empty", True) or "volume" not in leg.columns:
        return []
    out = []
    for rec in leg.to_dict(orient="records"):
        vol = _num(rec.get("volume"))
        strike = _num(rec.get("strike"))
        expiry = str(rec.get("expiry") or "")[:10]
        if vol is None or vol <= 0 or strike is None or len(expiry) != 10:
            continue
        row = {"side": side, "strike": strike, "expiry": expiry}
        for src, dst in _COLS.items():
            row[dst] = _num(rec.get(src))
        out.append(row)
    return out


def record_scan(
    calls: pd.DataFrame | None,
    puts: pd.DataFrame | None,
    *,
    ticker: str,
    scan_id: str,
    ts: datetime,
    source: str | None = None,
    db_path: str | None = None,
) -> int:
    """Store every traded contract from one scan. Returns rows written; never raises."""
    try:
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware (ET)")
        rows = list(_rows(calls, "CALL")) + list(_rows(puts, "PUT"))
        if not rows:
            return 0
        ts_iso = ts.isoformat()
        base = (ticker.upper(), scan_id, ts_iso, ts_iso[:10])
        with closing(_connect(db_path or DB_PATH)) as con, con:
            con.executemany(
                "INSERT OR IGNORE INTO contract_scans (ticker, scan_id, ts_et, session_date, side,"
                " strike, expiry, volume, open_interest, bid, ask, last, iv, source)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [base + (r["side"], r["strike"], r["expiry"], r["volume"], r["open_interest"],
                         r["bid"], r["ask"], r["last"], r["iv"], source) for r in rows],
            )
        return len(rows)
    except Exception:
        log.exception("volume_history.record_scan failed (scan continues)")
        return 0


# ── read side (for the Volume page) ──────────────────────────────────────────

def contract_history(
    ticker: str, side: str, strike: float, expiry: str, *, db_path: str | None = None,
) -> pd.DataFrame:
    """Every recorded scan for one contract, oldest first."""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return pd.DataFrame()
    with closing(sqlite3.connect(path, timeout=30)) as con:
        return pd.read_sql_query(
            "SELECT ts_et, session_date, volume, open_interest, bid, ask, last, iv"
            " FROM contract_scans WHERE ticker=? AND side=? AND strike=? AND expiry=?"
            " ORDER BY ts_et",
            con, params=(ticker.upper(), side.upper(), float(strike), expiry),
        )


def daily_summary(ticker: str, *, db_path: str | None = None, days: int = 10) -> pd.DataFrame:
    """One row per contract per session: end-of-session volume and that day's OI.

    Uses the last scan of each session per contract (volume is cumulative within a day).
    """
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return pd.DataFrame()
    q = """
    WITH last_scan AS (
        SELECT ticker, side, strike, expiry, session_date, MAX(ts_et) AS ts_et
        FROM contract_scans
        WHERE ticker = ? AND session_date IN (
            SELECT DISTINCT session_date FROM contract_scans WHERE ticker = ?
            ORDER BY session_date DESC LIMIT ?)
        GROUP BY ticker, side, strike, expiry, session_date
    )
    SELECT c.side, c.strike, c.expiry, c.session_date, c.volume, c.open_interest, c.last, c.iv
    FROM contract_scans c JOIN last_scan l
      ON c.ticker=l.ticker AND c.side=l.side AND c.strike=l.strike
     AND c.expiry=l.expiry AND c.ts_et=l.ts_et
    ORDER BY c.side, c.expiry, c.strike, c.session_date
    """
    with closing(sqlite3.connect(path, timeout=30)) as con:
        return pd.read_sql_query(q, con, params=(ticker.upper(), ticker.upper(), int(days)))


def building_positions(daily: pd.DataFrame, *, min_days: int = 3) -> pd.DataFrame:
    """Contracts whose open interest rose on each of the last ``min_days - 1`` session steps.

    Returns one row per contract with the latest volume/OI, OI change over the window, and
    the number of consecutive OI increases. A missing OI on any day, or a session where
    the contract didn't trade, breaks the streak (unknown is not an increase).

    Note: the OI a chain reports during a session is the previous session's closing OI.
    """
    if daily is None or daily.empty:
        return pd.DataFrame(columns=["side", "strike", "expiry", "volume", "open_interest",
                                     "oi_change", "oi_up_days", "sessions"])
    sessions = sorted(daily["session_date"].unique())
    out = []
    for (side, strike, expiry), g in daily.sort_values("session_date").groupby(
            ["side", "strike", "expiry"], sort=False):
        # Align to every recorded session: a day the contract didn't trade is unknown.
        by_day = dict(zip(g["session_date"], g["open_interest"]))
        oi = [by_day.get(d) for d in sessions if d >= g["session_date"].iloc[0]]
        streak = 0
        for prev, cur in zip(reversed(oi[:-1]), reversed(oi[1:])):
            if prev is None or cur is None or pd.isna(prev) or pd.isna(cur) or cur <= prev:
                break
            streak += 1
        if streak + 1 < min_days:
            continue
        first_oi = oi[-(streak + 1)]
        last = g.iloc[-1]
        out.append({
            "side": side, "strike": strike, "expiry": expiry,
            "volume": last["volume"], "open_interest": last["open_interest"],
            "oi_change": float(last["open_interest"]) - float(first_oi),
            "oi_up_days": streak, "sessions": len(g),
        })
    return pd.DataFrame(out).sort_values("oi_change", ascending=False, ignore_index=True) \
        if out else building_positions(pd.DataFrame())
