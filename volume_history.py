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

# AAPL scans every 3 min; a full snapshot every scan is ~100 MB/day. History charts only
# need ~15-min resolution, so record at most one snapshot per ticker per interval, plus
# every end-of-day scan (force=True).
RECORD_EVERY_MIN = 15

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
    min_interval_min: float | None = None,
    force: bool = False,
) -> int:
    """Store every traded contract from one scan. Returns rows written; never raises.

    With ``min_interval_min``, a scan is skipped (returns 0) when the same ticker was
    recorded less than that many minutes earlier in the same session, unless ``force``.
    """
    try:
        if ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware (ET)")
        rows = list(_rows(calls, "CALL")) + list(_rows(puts, "PUT"))
        if not rows:
            return 0
        ts_iso = ts.isoformat()
        base = (ticker.upper(), scan_id, ts_iso, ts_iso[:10])
        with closing(_connect(db_path or DB_PATH)) as con, con:
            if min_interval_min and not force:
                last = con.execute(
                    "SELECT MAX(ts_et) FROM contract_scans WHERE ticker=? AND session_date=?",
                    (base[0], base[3])).fetchone()[0]
                if last and (ts - datetime.fromisoformat(last)).total_seconds() < min_interval_min * 60:
                    return 0
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


# ── Volume page data (pure) ──────────────────────────────────────────────────

def recording_status(ticker: str, *, db_path: str | None = None) -> dict[str, Any]:
    """First/last scan, number of scans and sessions recorded for ``ticker``."""
    path = db_path or DB_PATH
    empty = {"first_ts": None, "last_ts": None, "last_scan_id": None, "scans": 0, "sessions": 0}
    if not os.path.exists(path):
        return empty
    with closing(sqlite3.connect(path, timeout=30)) as con:
        row = con.execute(
            "SELECT MIN(ts_et), MAX(ts_et), COUNT(DISTINCT scan_id), COUNT(DISTINCT session_date)"
            " FROM contract_scans WHERE ticker=?", (ticker.upper(),)).fetchone()
        if not row or not row[2]:
            return empty
        last_id = con.execute(
            "SELECT scan_id FROM contract_scans WHERE ticker=? AND ts_et=? LIMIT 1",
            (ticker.upper(), row[1])).fetchone()[0]
    return {"first_ts": row[0], "last_ts": row[1], "last_scan_id": last_id,
            "scans": int(row[2]), "sessions": int(row[3])}


def latest_scan(ticker: str, *, db_path: str | None = None) -> pd.DataFrame:
    """Every contract recorded in the most recent scan."""
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return pd.DataFrame()
    with closing(sqlite3.connect(path, timeout=30)) as con:
        return pd.read_sql_query(
            "SELECT side, strike, expiry, volume, open_interest, bid, ask, last, iv, ts_et, scan_id"
            " FROM contract_scans WHERE ticker=? AND ts_et=(SELECT MAX(ts_et) FROM contract_scans"
            " WHERE ticker=?)", con, params=(ticker.upper(), ticker.upper()))


def explorer_table(latest: pd.DataFrame, daily: pd.DataFrame, *, today) -> pd.DataFrame:
    """Rows for the Volume page: latest scan + OI change vs the previous session + OI streak.

    ``today`` is the ET session date; DTE is counted from it (not from the scan date).
    OI change is NaN when the previous session's OI is unknown — never 0.
    """
    cols = ["side", "strike", "expiry", "dte", "volume", "open_interest", "oi_change",
            "oi_up_days", "bid", "ask", "last", "iv"]
    if latest is None or latest.empty:
        return pd.DataFrame(columns=cols)
    df = latest.copy()
    exp = pd.to_datetime(df["expiry"], errors="coerce")
    df["dte"] = (exp - pd.Timestamp(today)).dt.days
    df = df[df["dte"] >= 0]

    df["oi_change"] = float("nan")
    df["oi_up_days"] = 0
    if daily is not None and not daily.empty:
        sessions = sorted(daily["session_date"].unique())
        key = ["side", "strike", "expiry"]
        by_contract = {k: dict(zip(g["session_date"], g["open_interest"]))
                       for k, g in daily.groupby(key)}
        changes, streaks = [], []
        for r in df.itertuples(index=False):
            hist = by_contract.get((r.side, r.strike, r.expiry), {})
            seq = [hist.get(d) for d in sessions]
            cur = seq[-1] if seq else None
            prev = seq[-2] if len(seq) > 1 else None
            ok = lambda v: v is not None and not pd.isna(v)
            changes.append(float(cur) - float(prev) if ok(cur) and ok(prev) else float("nan"))
            streak = 0
            for a, b in zip(reversed(seq[:-1]), reversed(seq[1:])):
                if not (ok(a) and ok(b)) or b <= a:
                    break
                streak += 1
            streaks.append(streak)
        df["oi_change"] = changes
        df["oi_up_days"] = streaks
    return df[cols].sort_values("volume", ascending=False, ignore_index=True)
