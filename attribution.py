"""
attribution.py — Append-only SQLite log of every scored contract + controls.

Marks (T+1h / T+1d / expiry) are write-once. Decision fields (score, rank,
multipliers, mid) are never UPDATEd after insert.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator, Literal
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
log = logging.getLogger("attribution")

Horizon = Literal["t1h", "t1d", "expiry"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    ts_et       TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    n_scored    INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    engine_sha  TEXT,
    daily_bias  TEXT,
    market_state TEXT,
    news_bias   TEXT,
    spot        REAL,
    vwap_state  TEXT,
    notes       TEXT
);

CREATE TABLE IF NOT EXISTS flags (
    flag_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT NOT NULL REFERENCES runs(run_id),
    ts_et       TEXT NOT NULL,
    ticker      TEXT NOT NULL,
    side        TEXT NOT NULL,
    strike      REAL NOT NULL,
    expiry      TEXT NOT NULL,
    score       REAL,
    rank        INTEGER,
    nlev        REAL,
    nflow       REAL,
    base_score  REAL,
    multipliers TEXT NOT NULL DEFAULT '{}',
    mid         REAL,
    bid         REAL,
    ask         REAL,
    spot        REAL,
    is_control  INTEGER NOT NULL DEFAULT 0,
    mark_t1h    REAL,
    mark_t1d    REAL,
    mark_expiry REAL,
    notes       TEXT,
    CHECK (is_control IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_flags_run ON flags(run_id);
CREATE INDEX IF NOT EXISTS idx_flags_due_t1h ON flags(mark_t1h, ts_et);
CREATE INDEX IF NOT EXISTS idx_flags_due_t1d ON flags(mark_t1d, ts_et);
"""

_VIEW_SQL = """
DROP VIEW IF EXISTS v_outcomes;
CREATE VIEW v_outcomes AS
SELECT
    f.flag_id,
    f.run_id,
    f.ts_et,
    f.ticker,
    f.side,
    f.strike,
    f.expiry,
    f.score,
    f.rank,
    f.nlev,
    f.nflow,
    f.base_score,
    f.multipliers,
    f.mid,
    f.is_control,
    f.mark_t1h,
    f.mark_t1d,
    f.mark_expiry,
    r.config_hash,
    r.engine_sha,
    r.daily_bias,
    r.market_state,
    r.news_bias,
    CASE
        WHEN f.mid IS NOT NULL AND f.mid > 0 AND f.mark_t1h IS NOT NULL
        THEN (f.mark_t1h - f.mid) / f.mid
    END AS ret_t1h,
    CASE
        WHEN f.mid IS NOT NULL AND f.mid > 0 AND f.mark_t1d IS NOT NULL
        THEN (f.mark_t1d - f.mid) / f.mid
    END AS ret_t1d,
    CASE
        WHEN f.mid IS NOT NULL AND f.mid > 0 AND f.mark_expiry IS NOT NULL
        THEN (f.mark_expiry - f.mid) / f.mid
    END AS ret_expiry,
    CASE
        WHEN f.is_control = 1 THEN 'CONTROL'
        WHEN f.rank IS NULL THEN 'UNRANKED'
        WHEN f.rank <= 3 THEN '01-03'
        WHEN f.rank <= 10 THEN '04-10'
        WHEN f.rank <= 20 THEN '11-20'
        ELSE '21+'
    END AS rank_bucket
FROM flags f
JOIN runs r USING (run_id);
"""

_FLAG_BASE_COLS = ("nlev", "nflow", "base_score")


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(flags)")}
    for col in _FLAG_BASE_COLS:
        if col not in cols:
            conn.execute(f"ALTER TABLE flags ADD COLUMN {col} REAL")
    conn.executescript(_VIEW_SQL)


def default_db_path() -> str:
    env = os.environ.get("SCANNER_DB")
    if env:
        return env
    base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, "data", "attribution.db")


def now_et() -> datetime:
    return datetime.now(ET)


def config_hash(cfg: dict[str, Any]) -> str:
    """Stable fingerprint of the scoring config dict."""
    payload = json.dumps(cfg, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def engine_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return out.strip()
    except Exception:
        return "unknown"


@contextmanager
def _db(path: str | None = None) -> Iterator[sqlite3.Connection]:
    db_path = path or default_db_path()
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(f"WAL required, got journal_mode={mode!r}")
        conn.execute("PRAGMA busy_timeout=5000")
        _ensure_schema(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def _load_env_file() -> None:
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(env_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except FileNotFoundError:
        pass


def alert_attribution_failure(message: str) -> bool:
    """
    Telegram alert when attribution logging fails.
    Never raises. Returns True if a send was attempted successfully.
    """
    _load_env_file()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    chat = (chat_raw.split(",")[0] or "").strip()
    if not token or not chat:
        log.warning("attribution alert skipped — Telegram not configured")
        return False
    try:
        import urllib.parse
        import urllib.request

        text = f"attribution FAIL\n{message}"[:3500]
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(url, data=data, timeout=15)
        return True
    except Exception as exc:
        log.warning("attribution alert send failed: %s", exc)
        return False


def score_from_flag_parts(
    nlev: float,
    nflow: float,
    multipliers: dict[str, float] | str,
    *,
    w_lev: float | None = None,
    w_flow: float | None = None,
    base_score: float | None = None,
) -> float:
    """Reconstruct Value_Score from persisted base parts + multiplier JSON."""
    if base_score is None:
        if w_lev is None or w_flow is None:
            from config import SCORING
            w_lev = float(SCORING["w_lev"])
            w_flow = float(SCORING["w_flow"])
        base = float(nlev) * float(w_lev) + float(nflow) * float(w_flow)
    else:
        base = float(base_score)
    if isinstance(multipliers, str):
        multipliers = json.loads(multipliers)
    prod = base
    for v in (multipliers or {}).values():
        prod *= float(v)
    return round(prod, 4)


def build_control_rows(
    chain_df: pd.DataFrame,
    spot: float,
    expiry: str,
) -> pd.DataFrame:
    """
    Nearest-to-ATM strike at `expiry`, both CALL and PUT.
    Selected by rule from the chain — never from scored ranks.
    """
    cols = ["side", "strike", "expiry", "last", "bid", "ask", "volume", "openInterest"]
    empty = pd.DataFrame(columns=cols)
    if chain_df is None or getattr(chain_df, "empty", True) or spot <= 0 or not expiry:
        return empty

    df = chain_df.copy()
    # Normalise column names
    rename = {}
    for src, dst in [
        ("Side", "side"),
        ("Strike", "strike"),
        ("Expiry", "expiry"),
        ("lastPrice", "last"),
        ("Last", "last"),
        ("Bid", "bid"),
        ("Ask", "ask"),
        ("Volume", "volume"),
        ("openInterest", "openInterest"),
        ("OpenInterest", "openInterest"),
    ]:
        if src in df.columns and dst not in df.columns:
            rename[src] = dst
    if rename:
        df = df.rename(columns=rename)

    if "side" not in df.columns or "strike" not in df.columns:
        return empty

    df["side"] = df["side"].astype(str).str.upper()
    df["strike"] = pd.to_numeric(df["strike"], errors="coerce")
    df["expiry"] = df["expiry"].astype(str)
    exp = str(expiry)
    sub = df[df["expiry"] == exp].dropna(subset=["strike"])
    if sub.empty:
        return empty

    strikes = sub["strike"].drop_duplicates()
    atm = float(strikes.iloc[(strikes - float(spot)).abs().argmin()])
    rows: list[dict[str, Any]] = []
    for side in ("CALL", "PUT"):
        hit = sub[(sub["side"] == side) & (sub["strike"] == atm)]
        if hit.empty:
            # synthesise a control shell so the rule still produces both sides
            rows.append({
                "side": side,
                "strike": atm,
                "expiry": exp,
                "last": float("nan"),
                "bid": float("nan"),
                "ask": float("nan"),
                "volume": 0,
                "openInterest": 0,
            })
            continue
        r = hit.iloc[0]
        last = r.get("last")
        if pd.isna(last) and "lastPrice" in hit.columns:
            last = r.get("lastPrice")
        rows.append({
            "side": side,
            "strike": atm,
            "expiry": exp,
            "last": float(last) if pd.notna(last) else float("nan"),
            "bid": float(r["bid"]) if "bid" in r.index and pd.notna(r["bid"]) else float("nan"),
            "ask": float(r["ask"]) if "ask" in r.index and pd.notna(r["ask"]) else float("nan"),
            "volume": int(r["volume"]) if "volume" in r.index and pd.notna(r["volume"]) else 0,
            "openInterest": int(r["openInterest"]) if "openInterest" in r.index and pd.notna(r.get("openInterest")) else 0,
        })
    return pd.DataFrame(rows)


def modal_flagged_expiry(scored: pd.DataFrame) -> str | None:
    """Modal expiry among scored (non-null Value_Score) contracts."""
    if scored is None or scored.empty or "Value_Score" not in scored.columns:
        return None
    sub = scored[scored["Value_Score"].notna()]
    if sub.empty or "expiry" not in sub.columns:
        return None
    mode = sub["expiry"].astype(str).mode()
    if mode.empty:
        return None
    return str(mode.iloc[0])


def _mid_from_row(r: pd.Series) -> float | None:
    bid = r.get("bid")
    ask = r.get("ask")
    last = r.get("last", r.get("lastPrice"))
    try:
        b = float(bid) if bid is not None and pd.notna(bid) else 0.0
        a = float(ask) if ask is not None and pd.notna(ask) else 0.0
        if b > 0 and a > 0:
            return (b + a) / 2.0
        if last is not None and pd.notna(last) and float(last) > 0:
            return float(last)
    except (TypeError, ValueError):
        return None
    return None


def log_run(
    *,
    ticker: str,
    scored_df: pd.DataFrame,
    cfg: dict[str, Any],
    spot: float,
    daily_bias: str | None = None,
    market_state: str | None = None,
    news_bias: str | None = None,
    vwap_state: str | None = None,
    control_rows: pd.DataFrame | None = None,
    engine_sha_val: str | None = None,
    db_path: str | None = None,
    ts_et: datetime | None = None,
) -> str:
    """
    Append one run + every scored contract + control rows.
    Returns run_id. Raises on DB errors (callers should fail-soft).
    """
    ts = ts_et or now_et()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    ts_iso = ts.astimezone(ET).isoformat(timespec="seconds")

    scored = scored_df
    if scored is not None and not scored.empty and "Value_Score" in scored.columns:
        scored = scored[scored["Value_Score"].notna()].copy()
        scored = scored.sort_values("Value_Score", ascending=False)
        scored["_rank"] = range(1, len(scored) + 1)
    else:
        scored = pd.DataFrame()

    n_scored = int(len(scored))
    run_id = str(uuid.uuid4())
    ch = config_hash(cfg)
    sha = engine_sha_val if engine_sha_val is not None else engine_sha()

    with _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, ts_et, ticker, n_scored, config_hash, engine_sha,
                daily_bias, market_state, news_bias, spot, vwap_state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, ts_iso, ticker.upper(), n_scored, ch, sha,
                daily_bias, market_state, news_bias, float(spot) if spot else None,
                vwap_state,
            ),
        )

        w_lev = float(cfg.get("w_lev", 0.4))
        w_flow = float(cfg.get("w_flow", 0.6))

        flag_rows: list[tuple] = []
        for _, r in scored.iterrows():
            mults = r.get("_multipliers")
            if isinstance(mults, dict):
                mult_json = json.dumps(mults, sort_keys=True)
            elif isinstance(mults, str) and mults:
                mult_json = mults
            else:
                mult_json = json.dumps({"_base": 1.0})
            if mult_json in ("{}", "null"):
                mult_json = json.dumps({"_base": 1.0})

            side = str(r.get("side") or r.get("Side") or "").upper()
            strike = float(r.get("strike") if "strike" in r.index else r.get("Strike") or 0)
            expiry = str(r.get("expiry") or r.get("Expiry") or "")
            mid = _mid_from_row(r)
            bid = r.get("bid")
            ask = r.get("ask")
            try:
                bid_f = float(bid) if bid is not None and pd.notna(bid) else None
            except (TypeError, ValueError):
                bid_f = None
            try:
                ask_f = float(ask) if ask is not None and pd.notna(ask) else None
            except (TypeError, ValueError):
                ask_f = None

            try:
                nlev_f = float(r["_nlev"]) if pd.notna(r.get("_nlev")) else None
            except (TypeError, ValueError, KeyError):
                nlev_f = None
            try:
                nflow_f = float(r["_nflow"]) if pd.notna(r.get("_nflow")) else None
            except (TypeError, ValueError, KeyError):
                nflow_f = None
            base_f = None
            if nlev_f is not None and nflow_f is not None:
                base_f = float(nlev_f) * w_lev + float(nflow_f) * w_flow

            flag_rows.append((
                run_id, ts_iso, ticker.upper(), side, strike, expiry,
                float(r["Value_Score"]), int(r["_rank"]),
                nlev_f, nflow_f, base_f, mult_json,
                mid, bid_f, ask_f, float(spot) if spot else None, 0, None,
            ))

        ctrl = control_rows if control_rows is not None else pd.DataFrame()
        for _, r in ctrl.iterrows():
            mid = _mid_from_row(r)
            bid = r.get("bid")
            ask = r.get("ask")
            try:
                bid_f = float(bid) if bid is not None and pd.notna(bid) else None
            except (TypeError, ValueError):
                bid_f = None
            try:
                ask_f = float(ask) if ask is not None and pd.notna(ask) else None
            except (TypeError, ValueError):
                ask_f = None
            flag_rows.append((
                run_id, ts_iso, ticker.upper(),
                str(r.get("side", "")).upper(),
                float(r.get("strike") or 0),
                str(r.get("expiry") or ""),
                None,  # score
                None,  # rank
                None,  # nlev
                None,  # nflow
                None,  # base_score
                json.dumps({"control": 1.0}),
                mid, bid_f, ask_f, float(spot) if spot else None, 1, None,
            ))

        conn.executemany(
            """
            INSERT INTO flags (
                run_id, ts_et, ticker, side, strike, expiry,
                score, rank, nlev, nflow, base_score, multipliers,
                mid, bid, ask, spot, is_control, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            flag_rows,
        )

    return run_id


def due_for_marking(
    horizon: Horizon,
    *,
    db_path: str | None = None,
    as_of: datetime | None = None,
) -> list[sqlite3.Row]:
    """Rows whose mark for `horizon` is still NULL and past the due time."""
    as_of = as_of or now_et()
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=ET)
    as_of = as_of.astimezone(ET)

    if horizon == "t1h":
        col = "mark_t1h"
        cutoff = as_of - timedelta(hours=1)
    elif horizon == "t1d":
        col = "mark_t1d"
        cutoff = as_of - timedelta(days=1)
    elif horizon == "expiry":
        col = "mark_expiry"
        cutoff = as_of  # expiry < today handled in SQL
    else:
        raise ValueError(f"unknown horizon: {horizon}")

    cutoff_iso = cutoff.isoformat(timespec="seconds")
    today = as_of.date().isoformat()

    with _db(db_path) as conn:
        if horizon == "expiry":
            rows = conn.execute(
                f"""
                SELECT flag_id, ticker, side, strike, expiry, mid, ts_et
                FROM flags
                WHERE {col} IS NULL
                  AND expiry < ?
                """,
                (today,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT flag_id, ticker, side, strike, expiry, mid, ts_et
                FROM flags
                WHERE {col} IS NULL
                  AND ts_et <= ?
                """,
                (cutoff_iso,),
            ).fetchall()
    return list(rows)


def write_mark(
    flag_id: int,
    horizon: Horizon,
    value: float | None,
    *,
    db_path: str | None = None,
) -> bool:
    """
    Write-once mark. Returns True if written, False if already set or value is None.
    Never writes 0.0 as a stand-in for a failed fetch — callers must pass None.
    """
    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    # Explicit reject of non-positive marks (failed/garbage quotes)
    if v <= 0.0:
        log.warning("refusing non-positive mark for flag_id=%s: %s", flag_id, v)
        return False

    col = {"t1h": "mark_t1h", "t1d": "mark_t1d", "expiry": "mark_expiry"}[horizon]
    with _db(db_path) as conn:
        cur = conn.execute(
            f"""
            UPDATE flags
            SET {col} = ?
            WHERE flag_id = ?
              AND {col} IS NULL
            """,
            (v, int(flag_id)),
        )
        return cur.rowcount == 1


def fetch_option_mid(
    ticker: str,
    side: str,
    strike: float,
    expiry: str,
) -> float | None:
    """
    Best-effort live mid. Returns None on any failure — never 0.0, never raises.
    """
    try:
        import yfinance as yf

        t = yf.Ticker(ticker)
        chain = t.option_chain(expiry)
        book = chain.calls if str(side).upper() == "CALL" else chain.puts
        row = book[abs(book["strike"] - float(strike)) < 1e-6]
        if row.empty:
            return None
        r = row.iloc[0]
        bid = float(r.get("bid") or 0)
        ask = float(r.get("ask") or 0)
        last = float(r.get("lastPrice") or 0)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
            return mid if mid > 0 else None
        if last > 0:
            return last
        return None
    except Exception as exc:
        log.debug("fetch_option_mid failed: %s", exc)
        return None
