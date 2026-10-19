"""setup_1100 — the 11:00 setup, recorded as a paper trade. Report only: it never places
an order and never changes scoring, ranking or the Game Plan.

Rule (from the 2026-10-19 replay of recorded quotes, 59 sessions Jul 27 - Oct 16):

    At the first scan between 11:00 and 11:10 ET, trade only when both agree:
      * the 15-minute EMAs are stacked (``ema_stack``: 9 > 21 > 50 calls, 9 < 21 < 50 puts)
      * price is on the same side of the day's morning reference (the first scan at or
        after 09:45): above it for calls, below it for puts.
    Then buy 1 at-the-money option of that side, nearest expiry, at the ask.
    No trade when the spread is wider than $0.06 or the ask is under $0.15.
    Exit on the bid: +$0.15 (target, filled at the target as a limit order would be),
    -$0.10 (stop, at the bid that hit it, so slippage is counted), or 30 minutes after
    entry at the bid.

One decision per session. Later scans resolve the open paper trade from the bid of the
same contract. The replay: 23 trades, 74% winners, +$6 per trade with stops slipped.
"""

from __future__ import annotations

import glob
import html
import json
import math
import os
import sqlite3
from contextlib import closing
from datetime import datetime, time, timedelta
from typing import Any

import pandas as pd

import ema_stack

_BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(_BASE, "data", "setup_1100.db")
ARCHIVE_DIR = os.path.join(_BASE, "archive")

TICKERS = frozenset({"AAPL"})          # the replay covered AAPL only
DECIDE_FROM = time(11, 0)
DECIDE_UNTIL = time(11, 10)
MORNING_REF = time(9, 45)
TARGET = 0.15
STOP = 0.10
HOLD_MIN = 30
MAX_SPREAD = 0.06
MIN_ASK = 0.15

TRADE, NO_TRADE, MISSED = "TRADE", "NO_TRADE", "MISSED"
OPEN, WIN, LOSS_STOP, TIME_EXIT, NO_QUOTE = "OPEN", "WIN", "STOP", "TIME", "NO_QUOTE"
CLOSED = (WIN, LOSS_STOP, TIME_EXIT, NO_QUOTE)

REPLAY = {"trades": 23, "sessions": 59, "win_pct": 74, "avg_usd": 6.1}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS setup_trades (
    day          TEXT NOT NULL,
    ticker       TEXT NOT NULL,
    decided_at   TEXT NOT NULL,
    action       TEXT NOT NULL,     -- TRADE | NO_TRADE | MISSED
    reason       TEXT NOT NULL,
    side         TEXT,              -- CALL | PUT
    strike       REAL,
    expiry       TEXT,
    spot         REAL,
    morning_spot REAL,
    entry        REAL,              -- ask paid
    target       REAL,
    stop         REAL,
    exit_by      TEXT,
    status       TEXT,              -- OPEN | WIN | STOP | TIME | NO_QUOTE (TRADE only)
    last_bid     REAL,
    exit_at      TEXT,
    exit_price   REAL,
    pnl          REAL,              -- USD per contract
    PRIMARY KEY (day, ticker)
)
"""
_FIELDS = ("day", "ticker", "decided_at", "action", "reason", "side", "strike", "expiry",
           "spot", "morning_spot", "entry", "target", "stop", "exit_by", "status",
           "last_bid", "exit_at", "exit_price", "pnl")


# ── store ──────────────────────────────────────────────────────────────────────
def _connect(path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(_SCHEMA)
    return con


def get(day: str, ticker: str, *, db_path: str | None = None) -> dict | None:
    with closing(_connect(db_path or DB_PATH)) as con:
        r = con.execute("SELECT * FROM setup_trades WHERE day=? AND ticker=?",
                        (day, ticker.upper())).fetchone()
    return dict(r) if r else None


def save(row: dict, *, db_path: str | None = None) -> None:
    vals = [row.get(f) for f in _FIELDS]
    with closing(_connect(db_path or DB_PATH)) as con, con:
        con.execute(f"INSERT OR REPLACE INTO setup_trades ({','.join(_FIELDS)}) "
                    f"VALUES ({','.join('?' * len(_FIELDS))})", vals)


def history(ticker: str, *, db_path: str | None = None) -> pd.DataFrame:
    path = db_path or DB_PATH
    if not os.path.exists(path):
        return pd.DataFrame(columns=list(_FIELDS))
    with closing(_connect(path)) as con:
        return pd.read_sql("SELECT * FROM setup_trades WHERE ticker=? ORDER BY day DESC",
                           con, params=(ticker.upper(),))


def stats(hist: pd.DataFrame) -> dict[str, Any]:
    """Sessions recorded, trades closed, wins, total and average P&L per contract."""
    h = hist if hist is not None else pd.DataFrame()
    if h.empty:
        return {"sessions": 0, "trades": 0, "closed": 0, "wins": 0, "win_pct": None,
                "total": 0.0, "avg": None, "no_trade": 0, "missed": 0}
    closed = h[(h.action == TRADE) & h.status.isin(CLOSED) & h.pnl.notna()]
    wins = int((closed.pnl > 0).sum())
    return {
        "sessions": int(len(h)),
        "trades": int((h.action == TRADE).sum()),
        "closed": int(len(closed)),
        "wins": wins,
        "win_pct": round(100 * wins / len(closed)) if len(closed) else None,
        "total": round(float(closed.pnl.sum()), 2),
        "avg": round(float(closed.pnl.mean()), 2) if len(closed) else None,
        "no_trade": int((h.action == NO_TRADE).sum()),
        "missed": int((h.action == MISSED).sum()),
    }


# ── the rule ───────────────────────────────────────────────────────────────────
def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def morning_spot(ticker: str, day: str, *, archive_dir: str | None = None) -> float | None:
    """Spot of the day's first scan at or after 09:45 (the earliest scan when none is)."""
    d = archive_dir or ARCHIVE_DIR
    stamp = day.replace("-", "")
    files = sorted(glob.glob(os.path.join(d, f"{ticker.upper()}_{stamp}_*.json")))
    if not files:
        return None
    later = [f for f in files if os.path.basename(f)[-11:-5] >= MORNING_REF.strftime("%H%M%S")]
    for f in (later or files):
        try:
            with open(f) as fh:
                spot = _num(json.load(fh).get("spot"))
        except (OSError, ValueError):
            continue
        if spot:
            return spot
    return None


def pick_contract(chain: pd.DataFrame, side: str, spot: float, day: str) -> dict | None:
    """At-the-money ``side`` option of the nearest expiry on or after ``day``."""
    if chain is None or chain.empty:
        return None
    c = chain[(chain["side"] == side) & (chain["expiry"].astype(str).str[:10] >= day)]
    c = c[c["bid"].notna() & c["ask"].notna() & (c["ask"] > 0)]
    if c.empty:
        return None
    exp = c["expiry"].astype(str).str[:10].min()
    c = c[c["expiry"].astype(str).str[:10] == exp]
    i = (c["strike"] - spot).abs().sort_values(kind="stable").index[0]
    r = c.loc[i]
    return {"strike": float(r["strike"]), "expiry": exp, "bid": float(r["bid"]),
            "ask": float(r["ask"])}


def decide(payload: dict, chain: pd.DataFrame, *, ticker: str, now: datetime,
           morning: float | None) -> dict:
    """The day's decision as a row for the store."""
    day = now.date().isoformat()
    spot = _num((payload or {}).get("spot"))
    row = {f: None for f in _FIELDS}
    row.update(day=day, ticker=ticker.upper(), decided_at=now.isoformat(), spot=spot,
               morning_spot=morning)

    def no(reason: str) -> dict:
        row.update(action=NO_TRADE, reason=reason)
        return row

    trend = ema_stack.banner_for_archive(payload, now=now)
    state = trend.get("state")
    if state not in (ema_stack.BULL, ema_stack.BEAR):
        return no(f"EMAs not stacked. {trend.get('reason') or ''}".strip())
    if spot is None or morning is None:
        return no("No price or no morning reference (first scan at or after 09:45).")
    side = "CALL" if state == ema_stack.BULL else "PUT"
    if spot == morning or (side == "CALL") != (spot > morning):
        where = "above" if spot > morning else "below" if spot < morning else "at"
        return no(f"EMAs say {side.lower()}s but price ${spot:.2f} is {where} the morning "
                  f"reference ${morning:.2f}: they disagree.")
    k = pick_contract(chain, side, spot, day)
    if k is None:
        return no(f"No quoted at-the-money {side.lower()} in this scan.")
    spread = k["ask"] - k["bid"]
    if spread > MAX_SPREAD + 1e-9:
        return no(f"Spread ${spread:.2f} on the {k['strike']:g} {side.lower()} is wider "
                  f"than ${MAX_SPREAD:.2f}.")
    if k["ask"] < MIN_ASK:
        return no(f"Ask ${k['ask']:.2f} is under ${MIN_ASK:.2f}.")
    entry = k["ask"]
    row.update(action=TRADE, side=side, strike=k["strike"], expiry=k["expiry"], entry=entry,
               target=round(entry + TARGET, 2), stop=round(entry - STOP, 2),
               exit_by=(now + timedelta(minutes=HOLD_MIN)).isoformat(), status=OPEN,
               last_bid=k["bid"],
               reason=(f"EMAs stacked {'up' if side == 'CALL' else 'down'} and price "
                       f"${spot:.2f} is {'above' if side == 'CALL' else 'below'} the morning "
                       f"reference ${morning:.2f}."))
    return row


def quote_bid(chain: pd.DataFrame, row: dict) -> float | None:
    if chain is None or chain.empty:
        return None
    c = chain[(chain["side"] == row["side"]) & (chain["strike"] == row["strike"])
              & (chain["expiry"].astype(str).str[:10] == row["expiry"])]
    return _num(c["bid"].iloc[0]) if len(c) else None


def update(row: dict, bid: float | None, now: datetime) -> dict:
    """Resolve an OPEN paper trade with this scan's bid. Returns the (new) row."""
    if row.get("status") != OPEN:
        return row
    row = dict(row)
    entry = float(row["entry"])
    due = now >= datetime.fromisoformat(row["exit_by"])
    if bid is not None:
        row["last_bid"] = bid
        if bid >= entry + TARGET - 1e-9:
            row.update(status=WIN, exit_price=round(entry + TARGET, 2), pnl=round(TARGET * 100, 2))
        elif bid <= entry - STOP + 1e-9:
            row.update(status=LOSS_STOP, exit_price=bid, pnl=round((bid - entry) * 100, 2))
        elif due:
            row.update(status=TIME_EXIT, exit_price=bid, pnl=round((bid - entry) * 100, 2))
    elif due:
        last = _num(row.get("last_bid"))
        if last is None:
            row.update(status=NO_QUOTE)
        else:
            row.update(status=TIME_EXIT, exit_price=last, pnl=round((last - entry) * 100, 2))
    if row["status"] != OPEN:
        row["exit_at"] = now.isoformat()
    return row


def on_scan(payload: dict, chain: pd.DataFrame, *, ticker: str, now: datetime,
            archive_dir: str | None = None, db_path: str | None = None) -> dict | None:
    """Called once per intraday scan. Returns {"event": "decided" | "closed" | None,
    "trade": row} for the archive, or None when the setup does not apply."""
    ticker = ticker.upper()
    if ticker not in TICKERS or now.tzinfo is None:
        return None
    day = now.date().isoformat()
    row = get(day, ticker, db_path=db_path)
    t = now.time()
    if row is None:
        if t < DECIDE_FROM:
            return None
        if t < DECIDE_UNTIL:
            row = decide(payload, chain, ticker=ticker, now=now,
                         morning=morning_spot(ticker, day, archive_dir=archive_dir))
        else:
            row = {f: None for f in _FIELDS}
            row.update(day=day, ticker=ticker, decided_at=now.isoformat(), action=MISSED,
                       reason="No scan between 11:00 and 11:10, so no decision was made.")
        save(row, db_path=db_path)
        return {"event": "decided", "trade": row}
    if row.get("status") == OPEN:
        new = update(row, quote_bid(chain, row), now)
        save(new, db_path=db_path)
        return {"event": "closed" if new["status"] != OPEN else None, "trade": new}
    return {"event": None, "trade": row}


# ── messages ───────────────────────────────────────────────────────────────────
def _contract(row: dict) -> str:
    exp = datetime.fromisoformat(row["expiry"]).strftime("%b %d")
    return f"{row['ticker']} {row['strike']:g} {row['side'].lower()} exp {exp}"


def message(ev: dict | None) -> str | None:
    """Telegram text (HTML) for a decided or closed event; None otherwise."""
    if not ev or ev.get("event") not in ("decided", "closed"):
        return None
    r = ev["trade"]
    head = f"⏰ <b>{r['ticker']} 11:00 setup</b> (paper)"
    why = html.escape(str(r.get("reason") or ""))
    if ev["event"] == "decided":
        if r["action"] == MISSED:
            return f"{head}\nNo decision today: {why}"
        if r["action"] == NO_TRADE:
            return f"{head}\n<b>No trade today.</b> {why}"
        by = datetime.fromisoformat(r["exit_by"]).strftime("%H:%M")
        return (f"{head}\n<b>Buy 1 {html.escape(_contract(r))}</b> at ${r['entry']:.2f} (ask)\n"
                f"Target ${r['target']:.2f} (+${TARGET * 100:.0f}) · stop ${r['stop']:.2f} "
                f"(−${STOP * 100:.0f}) · out by {by} ET\n{why}")
    label = {WIN: "✅ target hit", LOSS_STOP: "🛑 stopped out", TIME_EXIT: "⌛ time exit",
             NO_QUOTE: "❔ no quote at the exit time"}.get(r["status"], r["status"])
    pnl = "" if r.get("pnl") is None else f" {r['pnl']:+.0f} USD"
    return f"{head}\n{label}{pnl} on the {html.escape(_contract(r))}"
