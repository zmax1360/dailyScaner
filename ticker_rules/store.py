"""store — data/ticker_rules.db.

``rule_settings`` is append-only: a change is a new row, the current setting of a
(ticker, slot) is its newest row, and nothing is ever updated or deleted, so the table is
its own change log. ``paper_trades`` links every trade to the settings row it ran under.

Slots: ``normal`` (ordinary days), ``in_play`` (opening volume above the rule's
threshold) and ``exit`` (how every paper trade of the ticker is closed).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any

import pandas as pd

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_BASE, "data", "ticker_rules.db")

SLOTS = ("normal", "in_play", "exit")
STATUSES = ("paper", "live", "off")
PAPER, LIVE, OFF = STATUSES
PROMOTE_MIN_TRADES = 20

DEFAULT_EXIT = {"width": 2.5, "take_profit": 0.5, "stop": 0.5, "time_stop": "15:30",
                "max_leg_spread": 0.10}

# First-run settings (from the 2026-10-31 research, all paper).
SEED = [
    ("AAPL", "normal", "fade_midday_stack", {"fast": 9, "mid": 21, "slow": 50,
                                             "window": "11:00-12:30"}, PAPER,
     "Seed: AAPL midday EMA stack faded (Aug-Oct test: 57% / 68% right at close)"),
    ("AAPL", "in_play", "orb_in_play", {"rvol_min": 1.5, "window": "09:35-10:30"}, PAPER,
     "Seed: 5-min opening range breakout on in-play days (9 trades, +0.64R)"),
    ("AAPL", "exit", "debit_spread", DEFAULT_EXIT, PAPER, "Seed: 50%/50% exit, hold to 15:30"),
    ("SPY", "normal", "ema_pair", {"fast": 9, "slow": 89, "window": "11:00-12:30"}, PAPER,
     "Seed: SPY EMA 9/89 follow (Aug-Sep 58%, Oct 59% right at close)"),
    ("SPY", "exit", "debit_spread", DEFAULT_EXIT, PAPER, "Seed: 50%/50% exit, hold to 15:30"),
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS rule_settings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    slot        TEXT NOT NULL,
    rule        TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    status      TEXT NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS ix_settings_ts ON rule_settings(ticker, slot, id);
CREATE TABLE IF NOT EXISTS paper_trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    day           TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    settings_id   INTEGER NOT NULL REFERENCES rule_settings(id),
    exit_id       INTEGER REFERENCES rule_settings(id),
    rule          TEXT NOT NULL,
    slot          TEXT NOT NULL,
    reason        TEXT,
    side          TEXT NOT NULL,          -- CALL | PUT
    expiry        TEXT NOT NULL,
    long_strike   REAL NOT NULL,
    short_strike  REAL NOT NULL,
    spot          REAL,
    entry_at      TEXT NOT NULL,
    entry_cost    REAL NOT NULL,          -- per share: long ask - short bid
    target_value  REAL NOT NULL,
    stop_value    REAL NOT NULL,
    stop_spot     REAL,                   -- underlying stop (opening range breakout)
    time_stop     TEXT NOT NULL,
    status        TEXT NOT NULL,          -- OPEN | TARGET | STOP | TIME | NO_QUOTE
    last_value    REAL,
    exit_at       TEXT,
    exit_value    REAL,
    pnl           REAL                    -- USD per spread
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_trade_day ON paper_trades(ticker, day);
"""


def connect(path: str | None = None) -> sqlite3.Connection:
    p = path or DB_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)
    con = sqlite3.connect(p, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def add_setting(ticker: str, slot: str, rule: str, params: dict | None, status: str,
                note: str | None = None, *, ts: str | None = None,
                db_path: str | None = None) -> int:
    """Append a setting (the newest row per ticker+slot is the current one). Returns id."""
    if slot not in SLOTS:
        raise ValueError(f"unknown slot {slot!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    with closing(connect(db_path)) as con, con:
        cur = con.execute(
            "INSERT INTO rule_settings (ts, ticker, slot, rule, params, status, note)"
            " VALUES (?,?,?,?,?,?,?)",
            (ts or _now(), ticker.upper(), slot, rule, json.dumps(params or {}, sort_keys=True),
             status, note))
        return int(cur.lastrowid)


def _row(r: sqlite3.Row | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    if "params" in d:
        d["params"] = json.loads(d["params"] or "{}")
    return d


def current(ticker: str, *, db_path: str | None = None) -> dict[str, dict]:
    """{slot: newest settings row} for ``ticker`` (slots without any row are absent)."""
    with closing(connect(db_path)) as con:
        rows = con.execute(
            "SELECT * FROM rule_settings WHERE id IN (SELECT MAX(id) FROM rule_settings"
            " WHERE ticker=? GROUP BY slot)", (ticker.upper(),)).fetchall()
    return {r["slot"]: _row(r) for r in rows}


def setting(settings_id: int, *, db_path: str | None = None) -> dict | None:
    with closing(connect(db_path)) as con:
        return _row(con.execute("SELECT * FROM rule_settings WHERE id=?",
                                (settings_id,)).fetchone())


def history(ticker: str | None = None, *, db_path: str | None = None) -> pd.DataFrame:
    """Every settings row, newest first: the change log."""
    with closing(connect(db_path)) as con:
        if ticker:
            return pd.read_sql("SELECT * FROM rule_settings WHERE ticker=? ORDER BY id DESC",
                               con, params=(ticker.upper(),))
        return pd.read_sql("SELECT * FROM rule_settings ORDER BY id DESC", con)


def tickers(*, db_path: str | None = None) -> list[str]:
    with closing(connect(db_path)) as con:
        return [r[0] for r in con.execute("SELECT DISTINCT ticker FROM rule_settings ORDER BY 1")]


def seed_if_empty(*, db_path: str | None = None) -> int:
    """Write ``SEED`` when the table has no rows at all. Returns the rows added."""
    with closing(connect(db_path)) as con:
        if con.execute("SELECT COUNT(*) FROM rule_settings").fetchone()[0]:
            return 0
    for t, slot, rule, params, status, note in SEED:
        add_setting(t, slot, rule, params, status, note, db_path=db_path)
    return len(SEED)


# ---------------------------------------------------------------- paper trades

TRADE_FIELDS = ("day", "ticker", "settings_id", "exit_id", "rule", "slot", "reason", "side",
                "expiry", "long_strike", "short_strike", "spot", "entry_at", "entry_cost",
                "target_value", "stop_value", "stop_spot", "time_stop", "status",
                "last_value", "exit_at", "exit_value", "pnl")
OPEN, TARGET, STOP, TIME, NO_QUOTE = "OPEN", "TARGET", "STOP", "TIME", "NO_QUOTE"


def trade_for_day(ticker: str, day: str, *, db_path: str | None = None) -> dict | None:
    with closing(connect(db_path)) as con:
        r = con.execute("SELECT * FROM paper_trades WHERE ticker=? AND day=?",
                        (ticker.upper(), day)).fetchone()
    return dict(r) if r else None


def open_trade(row: dict, *, db_path: str | None = None) -> int:
    cols = [f for f in TRADE_FIELDS if f in row]
    with closing(connect(db_path)) as con, con:
        cur = con.execute(f"INSERT INTO paper_trades ({','.join(cols)}) VALUES"
                          f" ({','.join('?' * len(cols))})", [row[c] for c in cols])
        return int(cur.lastrowid)


def update_trade(trade_id: int, changes: dict, *, db_path: str | None = None) -> None:
    cols = [c for c in changes if c in TRADE_FIELDS]
    if not cols:
        return
    with closing(connect(db_path)) as con, con:
        con.execute(f"UPDATE paper_trades SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                    [changes[c] for c in cols] + [trade_id])


def trades(ticker: str | None = None, *, db_path: str | None = None) -> pd.DataFrame:
    q = ("SELECT t.*, s.params AS settings_params, s.status AS settings_status"
         " FROM paper_trades t JOIN rule_settings s ON s.id = t.settings_id")
    with closing(connect(db_path)) as con:
        if ticker:
            return pd.read_sql(q + " WHERE t.ticker=? ORDER BY t.day DESC", con,
                               params=(ticker.upper(),))
        return pd.read_sql(q + " ORDER BY t.day DESC", con)


def record_for(rule: str, ticker: str, *, db_path: str | None = None) -> dict[str, Any]:
    """Closed paper trades of ``rule`` on ``ticker`` (every settings version)."""
    t = trades(ticker, db_path=db_path)
    t = t[(t["rule"] == rule) & t["pnl"].notna()] if not t.empty else t
    n = len(t)
    if not n:
        return {"trades": 0, "wins": 0, "win_pct": None, "avg": None, "net": 0.0}
    wins = int((t["pnl"] > 0).sum())
    return {"trades": n, "wins": wins, "win_pct": round(100 * wins / n, 1),
            "avg": round(float(t["pnl"].mean()), 2), "net": round(float(t["pnl"].sum()), 2)}
