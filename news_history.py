"""news_history — every headline seen, kept so price moves can be matched to news later.

SQLite at data/news_history.db. Headlines arrive from ``news_service`` (each fetch is
stored, fail-soft) and from ``backfill`` (Finnhub by date range). One row per
(ticker, publish time, headline); storing the same headline again changes nothing.
No network here except through ``backfill``'s fetch function.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE, "data", "news_history.db")
_SCHEMA = """
CREATE TABLE IF NOT EXISTS headlines (
    ticker   TEXT NOT NULL,
    ts       INTEGER NOT NULL,      -- publish time, unix seconds (UTC)
    headline TEXT NOT NULL,
    source   TEXT, url TEXT, summary TEXT,
    PRIMARY KEY (ticker, ts, headline)
);
CREATE INDEX IF NOT EXISTS ix_headlines_ts ON headlines (ticker, ts);
"""


def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.executescript(_SCHEMA)
    return con


def store(ticker: str, articles: Iterable[dict[str, Any]], *, db_path: str | None = None) -> int:
    """Save normalised articles (``news_service`` shape). Returns how many were new.
    Articles without a publish time are skipped: an unknown time cannot be matched."""
    rows = [(str(ticker).upper(), int(a["datetime"]), str(a["headline"]).strip(),
             a.get("source") or "", a.get("url") or "", a.get("summary") or "")
            for a in articles or [] if a and a.get("headline") and int(a.get("datetime") or 0) > 0]
    if not rows:
        return 0
    with closing(_connect(db_path or DB_PATH)) as con, con:
        before = con.total_changes
        con.executemany("INSERT OR IGNORE INTO headlines VALUES (?,?,?,?,?,?)", rows)
        return con.total_changes - before


def load(ticker: str, start: datetime, end: datetime, *, db_path: str | None = None) -> pd.DataFrame:
    """Headlines published in [start, end] (aware datetimes), oldest first, with ``ts_et``
    as New York wall-clock time (naive, to match the price bars)."""
    path = db_path or DB_PATH
    cols = ["ts_et", "headline", "source", "url", "summary"]
    if not os.path.exists(path):
        return pd.DataFrame(columns=cols)
    with closing(_connect(path)) as con:
        df = pd.read_sql_query(
            "SELECT ts, headline, source, url, summary FROM headlines"
            " WHERE ticker=? AND ts BETWEEN ? AND ? ORDER BY ts",
            con, params=(ticker.upper(), int(start.timestamp()), int(end.timestamp())))
    if df.empty:
        return pd.DataFrame(columns=cols)
    df["ts_et"] = (pd.to_datetime(df["ts"], unit="s", utc=True)
                   .dt.tz_convert(ET).dt.tz_localize(None))
    return df[cols]


def backfill(ticker: str, days: int, *, fetch: Callable[[str, date, date], list] | None = None,
             db_path: str | None = None, today: date | None = None) -> int:
    """Fetch and store the last ``days`` days of headlines. Returns how many were new."""
    if fetch is None:
        from news_service import fetch_news_range as fetch
    end = today or datetime.now(ET).date()
    start = end - timedelta(days=int(days))
    return store(ticker, fetch(ticker, start, end), db_path=db_path)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
