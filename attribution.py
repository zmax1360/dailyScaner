"""
attribution.py — Append-only SQLite log of every scored contract + controls.

Marks (T+1h / T+1d / expiry) are write-once. Decision fields (score, rank,
multipliers, mid) are never UPDATEd after insert.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, time as dtime, timedelta
from typing import Any, Iterator, Literal
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")
log = logging.getLogger("attribution")

Horizon = Literal["t15m", "t30m", "t1h", "t1d", "close", "expiry"]
ShortHorizon = Literal["t15m", "t30m"]

# Session close (ET). Same-session flags become due for mark_close at/after this.
CLOSE_MARK_TIME = dtime(16, 15)
# Cash equity close — short-horizon exit marks due at/after this are unavailable
# (never clamped to the close print).
CASH_CLOSE_TIME = dtime(16, 0)

# Allowed method_* values for t15m / t30m exit marks (bid fill, not mid).
SHORT_MARK_METHODS = frozenset({"quote", "trade", "stale", "unavailable"})

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
    run_kind    TEXT NOT NULL DEFAULT 'intraday',
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
    mark_close  REAL,
    mark_expiry REAL,
    mark_t15m   REAL,
    mark_t30m   REAL,
    marked_t1h_at TEXT,
    marked_t1d_at TEXT,
    marked_close_at TEXT,
    marked_exp_at TEXT,
    marked_t15m_at TEXT,
    marked_t30m_at TEXT,
    close_method  TEXT,
    method_t15m   TEXT,
    method_t30m   TEXT,
    dte           INTEGER,
    volume        INTEGER,
    open_interest INTEGER,
    iv            REAL,
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
    f.dte,
    f.pool,
    f.score,
    f.rank,
    f.nlev,
    f.nflow,
    f.base_score,
    f.multipliers,
    f.mid,
    f.ask,
    f.is_control,
    f.mark_t1h,
    f.mark_t1d,
    f.mark_close,
    f.mark_expiry,
    f.mark_t15m,
    f.mark_t30m,
    f.marked_t1h_at,
    f.marked_t1d_at,
    f.marked_close_at,
    f.marked_exp_at,
    f.marked_t15m_at,
    f.marked_t30m_at,
    f.close_method,
    f.method_t15m,
    f.method_t30m,
    r.config_hash,
    r.engine_sha,
    r.daily_bias,
    r.market_state,
    r.news_bias,
    -- Short-horizon primary return: ask entry / bid exit (not mid).
    CASE
        WHEN f.ask IS NOT NULL AND f.ask > 0 AND f.mark_t15m IS NOT NULL
        THEN (f.mark_t15m - f.ask) / f.ask
    END AS ret_t15m,
    CASE
        WHEN f.ask IS NOT NULL AND f.ask > 0 AND f.mark_t30m IS NOT NULL
        THEN (f.mark_t30m - f.ask) / f.ask
    END AS ret_t30m,
    -- Mid-based short-horizon returns kept for comparison only.
    CASE
        WHEN f.mid IS NOT NULL AND f.mid > 0 AND f.mark_t15m IS NOT NULL
        THEN (f.mark_t15m - f.mid) / f.mid
    END AS ret_t15m_mid,
    CASE
        WHEN f.mid IS NOT NULL AND f.mid > 0 AND f.mark_t30m IS NOT NULL
        THEN (f.mark_t30m - f.mid) / f.mid
    END AS ret_t30m_mid,
    CASE
        WHEN f.mid IS NOT NULL AND f.mid > 0 AND f.mark_t1h IS NOT NULL
        THEN (f.mark_t1h - f.mid) / f.mid
    END AS ret_t1h,
    CASE
        WHEN f.mid IS NOT NULL AND f.mid > 0 AND f.mark_t1d IS NOT NULL
        THEN (f.mark_t1d - f.mid) / f.mid
    END AS ret_t1d,
    CASE
        WHEN f.mid IS NOT NULL AND f.mid > 0 AND f.mark_close IS NOT NULL
        THEN (f.mark_close - f.mid) / f.mid
    END AS ret_close,
    CASE
        WHEN f.mid IS NOT NULL AND f.mid > 0 AND f.mark_expiry IS NOT NULL
        THEN (f.mark_expiry - f.mid) / f.mid
    END AS ret_expiry,
    CASE
        WHEN f.marked_t15m_at IS NOT NULL
        THEN ROUND((julianday(f.marked_t15m_at) - julianday(f.ts_et)) * 24 * 60, 2)
    END AS minutes_t15m,
    CASE
        WHEN f.marked_t30m_at IS NOT NULL
        THEN ROUND((julianday(f.marked_t30m_at) - julianday(f.ts_et)) * 24 * 60, 2)
    END AS minutes_t30m,
    CASE
        WHEN f.marked_t1h_at IS NOT NULL
        THEN ROUND((julianday(f.marked_t1h_at) - julianday(f.ts_et)) * 24, 2)
    END AS hours_t1h,
    CASE
        WHEN f.marked_t1d_at IS NOT NULL
        THEN ROUND((julianday(f.marked_t1d_at) - julianday(f.ts_et)) * 24, 2)
    END AS hours_t1d,
    CASE
        WHEN f.marked_close_at IS NOT NULL
        THEN ROUND((julianday(f.marked_close_at) - julianday(f.ts_et)) * 24, 2)
    END AS hours_close,
    CASE
        WHEN f.marked_exp_at IS NOT NULL
        THEN ROUND((julianday(f.marked_exp_at) - julianday(f.ts_et)) * 24, 2)
    END AS hours_expiry,
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

# Additive migrations only — never DROP/recreate flags (live rows must survive).
_FLAG_MIGRATE_COLS: tuple[tuple[str, str], ...] = (
    ("nlev", "REAL"),
    ("nflow", "REAL"),
    ("base_score", "REAL"),
    ("marked_t1h_at", "TEXT"),
    ("marked_t1d_at", "TEXT"),
    ("marked_exp_at", "TEXT"),
    ("dte", "INTEGER"),
    ("volume", "INTEGER"),
    ("open_interest", "INTEGER"),
    ("iv", "REAL"),
    ("mark_close", "REAL"),
    ("marked_close_at", "TEXT"),
    ("close_method", "TEXT"),
    # Scoring-input pass-through (instrumentation — does not change ranking)
    ("delta", "REAL"),
    ("leverage_raw", "REAL"),
    ("flow_raw", "REAL"),
    ("leverage_norm", "REAL"),
    ("flow_norm", "REAL"),
    ("extrinsic", "REAL"),
    ("realized_vol_20d", "REAL"),
    ("iv_premium", "REAL"),
    ("pool", "TEXT"),  # '0DTE' | '1DTE+' — rank is within-pool (engine-v1.2)
    # Short-horizon exit marks (bid fill) — additive; does not change scoring
    ("mark_t15m", "REAL"),
    ("marked_t15m_at", "TEXT"),
    ("method_t15m", "TEXT"),
    ("mark_t30m", "REAL"),
    ("marked_t30m_at", "TEXT"),
    ("method_t30m", "TEXT"),
)

_RUN_MIGRATE_COLS: tuple[tuple[str, str], ...] = (
    ("run_kind", "TEXT"),
    ("optimal_strategy", "TEXT"),
    ("strategy_outlook", "INTEGER"),
)

_MARK_AT_COL = {
    "t15m": "marked_t15m_at",
    "t30m": "marked_t30m_at",
    "t1h": "marked_t1h_at",
    "t1d": "marked_t1d_at",
    "close": "marked_close_at",
    "expiry": "marked_exp_at",
}

_MARK_METHOD_COL = {
    "t15m": "method_t15m",
    "t30m": "method_t30m",
}


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(flags)")}
    for col, sql_type in _FLAG_MIGRATE_COLS:
        if col not in cols:
            conn.execute(f"ALTER TABLE flags ADD COLUMN {col} {sql_type}")
    # Index after migrate — mark_close is absent on pre-migration flags tables.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flags_due_close ON flags(mark_close, ts_et)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flags_due_t15m ON flags(mark_t15m, ts_et)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_flags_due_t30m ON flags(mark_t30m, ts_et)"
    )
    run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
    for col, sql_type in _RUN_MIGRATE_COLS:
        if col not in run_cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {sql_type}")
            conn.execute(
                "UPDATE runs SET run_kind = 'intraday' WHERE run_kind IS NULL"
            )
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
    cols = [
        "side", "strike", "expiry", "last", "bid", "ask",
        "volume", "openInterest", "dte", "iv",
    ]
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
        ("DTE", "dte"),
        ("IV", "iv"),
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
                "dte": None,
                "iv": None,
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
            "dte": (
                int(r["dte"]) if "dte" in r.index and pd.notna(r.get("dte")) else None
            ),
            "iv": (
                float(r["iv"]) if "iv" in r.index and pd.notna(r.get("iv")) else None
            ),
        })
    return pd.DataFrame(rows)


def build_control_rows_per_pool(
    chain_df: pd.DataFrame,
    spot: float,
) -> pd.DataFrame:
    """
    One ATM CALL+PUT control pair per scoring pool (0DTE / 1DTE+).

    Expiry is the modal expiry within that pool's chain slice. Pools with no
    chain contracts are skipped (no merge into the other pool).
    """
    from scoring_pool import POOL_0DTE, POOL_1DTE, scoring_pool as _sp

    if chain_df is None or getattr(chain_df, "empty", True) or spot <= 0:
        return pd.DataFrame()

    df = chain_df.copy()
    if "dte" not in df.columns and "DTE" in df.columns:
        df = df.rename(columns={"DTE": "dte"})
    if "dte" not in df.columns:
        return pd.DataFrame()
    df["_pool"] = [_sp(v) for v in df["dte"].tolist()]

    out_parts: list[pd.DataFrame] = []
    for pool_name in (POOL_0DTE, POOL_1DTE):
        sub = df[df["_pool"] == pool_name]
        if sub.empty or "expiry" not in sub.columns:
            continue
        exp = str(sub["expiry"].astype(str).mode().iloc[0])
        ctrl = build_control_rows(sub, spot, exp)
        if ctrl.empty:
            continue
        ctrl = ctrl.copy()
        ctrl["pool"] = pool_name
        # Ensure control dte matches pool when synthesised shells have dte=None
        if pool_name == POOL_0DTE:
            ctrl["dte"] = ctrl["dte"].fillna(0).astype(int)
        out_parts.append(ctrl)
    if not out_parts:
        return pd.DataFrame()
    return pd.concat(out_parts, ignore_index=True)


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


def _nullable_int(r: pd.Series, *names: str) -> int | None:
    """Read first present column as int. Missing column → None (never invent 0)."""
    for name in names:
        if name not in r.index:
            continue
        v = r.get(name)
        if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
            return None
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return None


def _nullable_float(r: pd.Series, *names: str) -> float | None:
    """Read first present column as float. Missing column → None (never invent 0.0)."""
    for name in names:
        if name not in r.index:
            continue
        v = r.get(name)
        if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    return None


def _flag_state_from_row(r: pd.Series) -> tuple[int | None, int | None, int | None, float | None]:
    """Frozen-at-flag-time fields: dte, volume, open_interest, iv."""
    return (
        _nullable_int(r, "dte", "DTE"),
        _nullable_int(r, "volume", "Volume"),
        _nullable_int(r, "openInterest", "open_interest", "OpenInterest"),
        _nullable_float(r, "iv", "IV"),
    )


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
    run_kind: str = "intraday",
    daily_closes: list[float] | None = None,
) -> str:
    """
    Append one run + every scored contract + control rows.
    Returns run_id. Raises on DB errors (callers should fail-soft).

    Scoring inputs (delta, leverage/flow legs, base_score, multipliers,
    extrinsic, Optimal_Strategy) are pass-through from calculate_best_value —
    never recomputed here.

    vwap_state is accepted for schema compatibility but left empty on the
    scan path: VWAP is computed only in app.py. Moving it into the scanner
    would change what the engine sees and is out of scope for instrumentation.

    realized_vol_20d / iv_premium are recorded only (not scored). Sourced from
    daily_closes already fetched by the scan — no extra network call.

    run_kind: 'intraday' | 'eod'
      EOD recommendation: insert the run row for audit, but skip flag rows.
      A 16:20 flag can never receive a valid T+1h mark (mark window ends 16:15;
      4h market-time staleness). Logging flags would create permanently
      unmarkable overdue noise. EOD rankings live in the archive JSON instead.
    """
    kind = (run_kind or "intraday").strip().lower()
    if kind not in ("intraday", "eod"):
        raise ValueError(f"run_kind must be 'intraday' or 'eod', got {run_kind!r}")

    ts = ts_et or now_et()
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    ts_iso = ts.astimezone(ET).isoformat(timespec="seconds")

    scored = scored_df
    if scored is not None and not scored.empty and "Value_Score" in scored.columns:
        scored = scored[scored["Value_Score"].notna()].copy()
        # Rank is within-pool (set by calculate_best_value). Fall back only
        # if an older caller omitted _rank — never re-rank across pools.
        if "_rank" not in scored.columns or scored["_rank"].isna().all():
            if "pool" in scored.columns:
                scored["_rank"] = pd.NA
                for _p, g in scored.groupby(scored["pool"], dropna=False):
                    order = g["Value_Score"].sort_values(ascending=False).index
                    scored.loc[order, "_rank"] = range(1, len(order) + 1)
            else:
                scored = scored.sort_values("Value_Score", ascending=False)
                scored["_rank"] = range(1, len(scored) + 1)
        scored = scored.sort_values(
            ["pool", "_rank"] if "pool" in scored.columns else ["_rank"],
            ascending=True,
        )
    else:
        scored = pd.DataFrame()

    n_scored = int(len(scored))
    run_id = str(uuid.uuid4())
    ch = config_hash(cfg)
    sha = engine_sha_val if engine_sha_val is not None else engine_sha()

    # Optimal strategy / outlook — same for every row in the run
    opt_strat: str | None = None
    strat_outlook: int | None = None
    if not scored.empty and "Optimal_Strategy" in scored.columns:
        raw = scored["Optimal_Strategy"].iloc[0]
        if raw is not None and str(raw).strip():
            opt_strat = str(raw).strip()
            try:
                from strategy_engine import strategy_outlook as _outlook
                strat_outlook = _outlook(opt_strat)
            except Exception:
                strat_outlook = None

    # IV vs realized — once per run from scan's daily closes (instrumentation)
    rv_20d: float | None = None
    if daily_closes is not None:
        try:
            from features.realized_vol import realized_vol as _rv
            rv_20d = _rv(list(daily_closes), window=20)
        except Exception as exc:
            log.info("realized_vol_20d unavailable: %s", exc)
            rv_20d = None
        if rv_20d is None:
            log.info(
                "realized_vol_20d unavailable: need 21 positive closes (got %d)",
                len(daily_closes),
            )

    with _db(db_path) as conn:
        conn.execute(
            """
            INSERT INTO runs (
                run_id, ts_et, ticker, n_scored, config_hash, engine_sha,
                daily_bias, market_state, news_bias, spot, vwap_state, run_kind,
                optimal_strategy, strategy_outlook
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, ts_iso, ticker.upper(), n_scored, ch, sha,
                daily_bias, market_state, news_bias,
                float(spot) if spot else None,
                vwap_state, kind,
                opt_strat, strat_outlook,
            ),
        )

        # EOD: run audit row only — no t1h-eligible flags
        if kind == "eod":
            return run_id

        from features.realized_vol import iv_premium as _iv_premium

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

            # Pass-through from engine — never recompute (avoids audit drift)
            nlev_f = _row_float(r, "_nlev")
            nflow_f = _row_float(r, "_nflow")
            base_f = _row_float(r, "_base_score")
            lev_raw = _row_float(r, "_lev")
            flow_raw = _row_float(r, "_flow")
            delta_f = _row_float(r, "delta")
            extrinsic_f = _row_float(r, "extrinsic")
            # leverage_norm / flow_norm are the post-minmax legs
            lev_norm = nlev_f
            flow_norm = nflow_f

            dte_i, vol_i, oi_i, iv_f = _flag_state_from_row(r)
            ivp = _iv_premium(iv_f, rv_20d)
            pool_s = r.get("pool")
            if pool_s is not None and (not isinstance(pool_s, float) or pd.notna(pool_s)):
                pool_s = str(pool_s)
            else:
                pool_s = None
            try:
                rank_i = int(r["_rank"]) if pd.notna(r.get("_rank")) else None
            except (TypeError, ValueError):
                rank_i = None

            flag_rows.append((
                run_id, ts_iso, ticker.upper(), side, strike, expiry,
                float(r["Value_Score"]), rank_i,
                nlev_f, nflow_f, base_f, mult_json,
                mid, bid_f, ask_f, float(spot) if spot else None, 0, None,
                dte_i, vol_i, oi_i, iv_f,
                delta_f, lev_raw, flow_raw, lev_norm, flow_norm, extrinsic_f,
                rv_20d, ivp, pool_s,
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
            dte_i, vol_i, oi_i, iv_f = _flag_state_from_row(r)
            ivp = _iv_premium(iv_f, rv_20d)
            pool_s = r.get("pool")
            if pool_s is not None and (not isinstance(pool_s, float) or pd.notna(pool_s)):
                pool_s = str(pool_s)
            else:
                from scoring_pool import scoring_pool as _sp
                pool_s = _sp(dte_i)
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
                dte_i, vol_i, oi_i, iv_f,
                None, None, None, None, None, None,  # delta..extrinsic
                rv_20d, ivp, pool_s,
            ))

        conn.executemany(
            """
            INSERT INTO flags (
                run_id, ts_et, ticker, side, strike, expiry,
                score, rank, nlev, nflow, base_score, multipliers,
                mid, bid, ask, spot, is_control, notes,
                dte, volume, open_interest, iv,
                delta, leverage_raw, flow_raw, leverage_norm, flow_norm,
                extrinsic, realized_vol_20d, iv_premium, pool
            ) VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            flag_rows,
        )

    return run_id


def _row_float(r: pd.Series, *keys: str) -> float | None:
    for k in keys:
        if k not in r.index:
            continue
        v = r.get(k)
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        try:
            if pd.isna(v):
                return None
        except (TypeError, ValueError):
            pass
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(f):
            return None
        return f
    return None


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

    if horizon == "t15m":
        col = "mark_t15m"
        method_col = "method_t15m"
        cutoff = as_of - timedelta(minutes=15)
    elif horizon == "t30m":
        col = "mark_t30m"
        method_col = "method_t30m"
        cutoff = as_of - timedelta(minutes=30)
    elif horizon == "t1h":
        col = "mark_t1h"
        method_col = None
        cutoff = as_of - timedelta(hours=1)
    elif horizon == "t1d":
        col = "mark_t1d"
        method_col = None
        cutoff = as_of - timedelta(days=1)
    elif horizon == "close":
        col = "mark_close"
        method_col = None
        cutoff = as_of  # unused — session-date rule below
    elif horizon == "expiry":
        col = "mark_expiry"
        method_col = None
        cutoff = as_of  # expiry < today handled in SQL
    else:
        raise ValueError(f"unknown horizon: {horizon}")

    cutoff_iso = cutoff.isoformat(timespec="seconds")
    today = as_of.date().isoformat()

    fail_like = f"%fail:{horizon}%"
    with _db(db_path) as conn:
        if horizon == "expiry":
            rows = conn.execute(
                f"""
                SELECT flag_id, ticker, side, strike, expiry, mid, ts_et, notes, dte
                FROM flags
                WHERE {col} IS NULL
                  AND expiry < ?
                  AND (notes IS NULL OR notes NOT LIKE ?)
                """,
                (today, fail_like),
            ).fetchall()
        elif horizon == "close":
            # Due at 16:15 ET on the flag's session date; also return prior
            # sessions so mark_runner can write stale:close (unrecoverable).
            stale_tag = "stale:close"
            sess = "substr(f.ts_et, 1, 10)"
            if as_of.time() >= CLOSE_MARK_TIME:
                # Today’s session is open for close marks + all prior sessions.
                sess_clause = f"{sess} <= ?"
                sess_args: tuple = (today,)
            else:
                # Before 16:15: only prior sessions (for stale notes).
                sess_clause = f"{sess} < ?"
                sess_args = (today,)
            rows = conn.execute(
                f"""
                SELECT f.flag_id, f.ticker, f.side, f.strike, f.expiry,
                       f.mid, f.ts_et, f.notes, f.dte
                FROM flags f
                LEFT JOIN runs r ON r.run_id = f.run_id
                WHERE f.{col} IS NULL
                  AND {sess_clause}
                  AND (f.notes IS NULL OR f.notes NOT LIKE ?)
                  AND (f.notes IS NULL OR f.notes NOT LIKE ?)
                  AND (f.notes IS NULL OR f.notes NOT LIKE '%n/a:eod%')
                  AND (r.run_kind IS NULL OR r.run_kind != 'eod')
                """,
                (*sess_args, f"%{stale_tag}%", fail_like),
            ).fetchall()
        elif method_col is not None:
            # Short horizons: sealed when mark OR method is set (unavailable/
            # stale seal mark=NULL + method=…). Guard remains mark IS NULL for
            # price writes; method IS NULL excludes sealed non-price rows.
            stale_tag = f"stale:{horizon}"
            rows = conn.execute(
                f"""
                SELECT f.flag_id, f.ticker, f.side, f.strike, f.expiry,
                       f.mid, f.ts_et, f.notes, f.dte
                FROM flags f
                LEFT JOIN runs r ON r.run_id = f.run_id
                WHERE f.{col} IS NULL
                  AND f.{method_col} IS NULL
                  AND f.ts_et <= ?
                  AND (f.notes IS NULL OR f.notes NOT LIKE ?)
                  AND (f.notes IS NULL OR f.notes NOT LIKE ?)
                  AND (f.notes IS NULL OR f.notes NOT LIKE '%n/a:eod%')
                  AND (r.run_kind IS NULL OR r.run_kind != 'eod')
                """,
                (cutoff_iso, f"%{stale_tag}%", fail_like),
            ).fetchall()
        else:
            stale_tag = f"stale:{horizon}"
            rows = conn.execute(
                f"""
                SELECT f.flag_id, f.ticker, f.side, f.strike, f.expiry,
                       f.mid, f.ts_et, f.notes, f.dte
                FROM flags f
                LEFT JOIN runs r ON r.run_id = f.run_id
                WHERE f.{col} IS NULL
                  AND f.ts_et <= ?
                  AND (f.notes IS NULL OR f.notes NOT LIKE ?)
                  AND (f.notes IS NULL OR f.notes NOT LIKE ?)
                  AND (f.notes IS NULL OR f.notes NOT LIKE '%n/a:eod%')
                  AND (r.run_kind IS NULL OR r.run_kind != 'eod')
                """,
                (cutoff_iso, f"%{stale_tag}%", fail_like),
            ).fetchall()
    return list(rows)



def _append_flag_note(
    flag_id: int,
    tag: str,
    *,
    db_path: str | None = None,
) -> bool:
    """Write-once note tag into flags.notes. Returns True if newly written."""
    tag = str(tag).strip()
    if not tag:
        return False
    with _db(db_path) as conn:
        row = conn.execute(
            "SELECT notes FROM flags WHERE flag_id = ?",
            (int(flag_id),),
        ).fetchone()
        if row is None:
            return False
        notes = row["notes"]
        if notes is not None and tag in str(notes):
            return False
        if notes is None or not str(notes).strip():
            new_notes = tag
        else:
            new_notes = f"{str(notes).rstrip()};{tag}"
        cur = conn.execute(
            """
            UPDATE flags
            SET notes = ?
            WHERE flag_id = ?
              AND (notes IS NULL OR notes NOT LIKE ?)
            """,
            (new_notes, int(flag_id), f"%{tag}%"),
        )
        return cur.rowcount == 1


def note_stale_horizon(
    flag_id: int,
    horizon: Horizon,
    *,
    db_path: str | None = None,
) -> bool:
    """
    Write-once 'stale:t1h' / 'stale:t1d' / 'stale:close' into flags.notes.
    Does not write a mark value. Returns True if the note was newly written.
    """
    if horizon not in ("t1h", "t1d", "close"):
        raise ValueError(
            f"staleness notes only apply to t1h/t1d/close, got {horizon}"
        )
    return _append_flag_note(flag_id, f"stale:{horizon}", db_path=db_path)


def note_mark_failure(
    flag_id: int,
    horizon: Horizon,
    reason: str,
    *,
    db_path: str | None = None,
) -> bool:
    """
    Permanent mark failure — write-once ``fail:{horizon}:{reason}``.
    due_for_marking excludes these so the row is never attempted again.
    """
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in str(reason))[:80]
    return _append_flag_note(
        flag_id, f"fail:{horizon}:{safe or 'error'}", db_path=db_path,
    )


def write_mark(
    flag_id: int,
    horizon: Horizon,
    value: float | None,
    *,
    db_path: str | None = None,
    close_method: str | None = None,
    mark_method: str | None = None,
) -> bool:
    """
    Write-once mark. Returns True if written, False if already set or refused.

    Never writes 0.0 as a stand-in for a failed fetch — callers must pass None
    (except expiry OTM intrinsic, and short-horizon seal-only methods).

    Short horizons (t15m/t30m): store BID (or last as method='trade'). Seal-only
    methods ``stale`` / ``unavailable`` write method_* + marked_*_at with
    mark_* left NULL. Idempotent on ``mark_* IS NULL`` (and method_* IS NULL
    for short horizons).
    """
    at_col = _MARK_AT_COL[horizon]
    marked_at = now_et().astimezone(ET).isoformat(timespec="seconds")

    # ── Short-horizon exit marks (bid fill) ───────────────────────────────────
    if horizon in ("t15m", "t30m"):
        col = "mark_t15m" if horizon == "t15m" else "mark_t30m"
        method_col = _MARK_METHOD_COL[horizon]
        method = str(mark_method or "").strip().lower()
        if method not in SHORT_MARK_METHODS:
            log.warning(
                "refusing %s mark without mark_method in %s flag_id=%s got %r",
                horizon, sorted(SHORT_MARK_METHODS), flag_id, mark_method,
            )
            return False

        # Seal-only: due after cash close, or missed the exit window.
        if method in ("stale", "unavailable"):
            if value is not None:
                log.warning(
                    "refusing %s seal method=%s with a price flag_id=%s",
                    horizon, method, flag_id,
                )
                return False
            with _db(db_path) as conn:
                cur = conn.execute(
                    f"""
                    UPDATE flags
                    SET {method_col} = ?, {at_col} = ?
                    WHERE flag_id = ?
                      AND {col} IS NULL
                      AND {method_col} IS NULL
                    """,
                    (method, marked_at, int(flag_id)),
                )
                return cur.rowcount == 1

        if value is None:
            return False
        try:
            v = float(value)
        except (TypeError, ValueError):
            return False
        if v <= 0.0 or not math.isfinite(v):
            log.warning("refusing non-positive %s mark for flag_id=%s: %s",
                        horizon, flag_id, v)
            return False
        with _db(db_path) as conn:
            cur = conn.execute(
                f"""
                UPDATE flags
                SET {col} = ?, {at_col} = ?, {method_col} = ?
                WHERE flag_id = ?
                  AND {col} IS NULL
                  AND {method_col} IS NULL
                """,
                (v, marked_at, method, int(flag_id)),
            )
            return cur.rowcount == 1

    if value is None:
        return False
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    # Reject non-positive marks for live horizons (failed/garbage quotes).
    # Expiry intrinsic may be exactly 0.0 for OTM contracts — that is valid.
    if v < 0.0 or (v == 0.0 and horizon != "expiry"):
        log.warning("refusing non-positive mark for flag_id=%s: %s", flag_id, v)
        return False

    col = {
        "t1h": "mark_t1h",
        "t1d": "mark_t1d",
        "close": "mark_close",
        "expiry": "mark_expiry",
    }[horizon]
    with _db(db_path) as conn:
        if horizon == "close":
            method = str(close_method or "").strip().lower()
            if method not in ("quote", "intrinsic"):
                log.warning(
                    "refusing close mark without close_method "
                    "quote|intrinsic flag_id=%s got %r",
                    flag_id, close_method,
                )
                return False
            cur = conn.execute(
                """
                UPDATE flags
                SET mark_close = ?, marked_close_at = ?, close_method = ?
                WHERE flag_id = ?
                  AND mark_close IS NULL
                """,
                (v, marked_at, method, int(flag_id)),
            )
        else:
            cur = conn.execute(
                f"""
                UPDATE flags
                SET {col} = ?, {at_col} = ?
                WHERE flag_id = ?
                  AND {col} IS NULL
                """,
                (v, marked_at, int(flag_id)),
            )
        return cur.rowcount == 1


def resolve_market_data_source_name() -> str:
    """
    Resolve SCORING['market_data_source'].

    Configured in config.py SCORING (not env). If the key is absent, log.error
    naming the key and the fallback — never silent.
    """
    from config import SCORING

    if "market_data_source" not in SCORING:
        log.error(
            "SCORING missing key %r — falling back to %r",
            "market_data_source",
            "yahoo",
        )
        return "yahoo"
    return str(SCORING["market_data_source"])


def fetch_option_mid(
    ticker: str,
    side: str,
    strike: float,
    expiry: str,
    *,
    source=None,
) -> float | None:
    """
    Best-effort live mid. Never returns 0.0 as a stand-in.

    Re-raises ValueError for permanent failures (unknown expiry / strike).
    Returns None on transient failures. ``source`` is a MarketDataSource
    constructed at the caller entry point when omitted.
    """
    try:
        if source is None:
            from sources import get_source
            source = get_source(resolve_market_data_source_name())
        return source.fetch_option_mid(ticker, side, strike, expiry)
    except ValueError:
        # Permanent (expiry/strike not found) — caller must note & skip retries.
        raise
    except Exception as exc:
        log.debug("fetch_option_mid failed: %s", exc)
        return None


def fetch_option_exit(
    ticker: str,
    side: str,
    strike: float,
    expiry: str,
    *,
    source=None,
) -> tuple[float | None, str | None]:
    """
    Live exit fill for short horizons: prefer BID (method='quote'), else last
    trade (method='trade'). Never returns mid. Never invents a price.

    Returns (price, method) or (None, None) on transient miss.
    Re-raises ValueError for permanent failures (unknown expiry / strike).

    No as-of/historical path — wired sources are live-only. Historical rows
    cannot be backfilled with a true bid-at-timestamp from this stack.

    Prefer constructing ``source`` once per mark_runner pass and passing it
    in — Massive caches the exit chain on the instance.
    """
    try:
        if source is None:
            from sources import get_source
            source = get_source(resolve_market_data_source_name())
        fetch = getattr(source, "fetch_option_exit", None)
        if callable(fetch):
            return fetch(ticker, side, strike, expiry)
        log.error(
            "source %r has no fetch_option_exit — cannot mark exit "
            "%s %s %s %s",
            getattr(source, "name", type(source).__name__),
            ticker, side, strike, expiry,
        )
        return None, None
    except ValueError:
        raise
    except Exception as exc:
        log.warning(
            "fetch_option_exit failed %s %s %s %s: %s",
            ticker, side, strike, expiry, exc,
        )
        return None, None
