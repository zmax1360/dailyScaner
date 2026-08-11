#!/usr/bin/env python3
"""
eod_report.py — daily scorecard for the scanner.

Run after the close:
    python eod_report.py                 # today, all tickers
    python eod_report.py --date 2026-08-03 --ticker AAPL
    python eod_report.py --days 15       # rolling window across sessions

Grades the engine against recorded outcomes. Every number comes from
data/attribution.db — nothing is recomputed or estimated here.

Design notes:
  * Rows are CLUSTERED by (date, contract) before averaging. A contract scored
    every 5 minutes is ONE observation, not 78. Un-clustered counts overstate
    the sample by ~50x and make noise look significant.
  * Only marks taken within MAX_HOURS of the flag are used for T+1h / T+1d.
    A mark written the next morning is an overnight move wearing a 1-hour label.
  * 0DTE, 1DTE+, and UNKNOWN (NULL dte, pre-migration) are reported separately.
  * Expiry intrinsic marks have no lag filter — settlement value does not depend
    on when the mark was written (see MAX_HOURS_EXPIRY note below).
  * Both mean and median are shown. Option returns are bounded at -100% and
    unbounded up, so a mean can be positive while most contracts lost money.
"""

from __future__ import annotations

import argparse
import io
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(_BASE, "report")
DB = os.environ.get(
    "SCANNER_DB",
    os.path.join(_BASE, "data", "attribution.db"),
)
ET = ZoneInfo("America/New_York")
MAX_HOURS_T1H = 2.0          # reject marks taken more than 2h after the flag
MAX_HOURS_T1D = 30.0         # ~24h target + overnight slack
# Expiry marks are intrinsic from the underlying close on expiry day — lag
# between flag time and write time does not change the settlement value.
# Do NOT apply a hours_expiry guard if/when an expiry outcomes section is added.
MAX_HOURS_EXPIRY = None
# Short-horizon exit marks use minutes_* lag (not hours_*). Cap ≈ 2× due lag.
MAX_MINUTES_T15M = 30.0
MAX_MINUTES_T30M = 60.0
OUTLIER = 1.0                # returns >= +100% treated as tail events
FACTOR_LOW_N = 30            # buckets below this get "(low n)"
# Collection window — factor section aggregates from here through report date.
WINDOW_START = "2026-08-10"
WINDOW_END_NOTE = "2026-08-28"
SHORT_MARK_OK = ("quote", "trade")  # usable exit observations only

# ── Paper-strategy analysis parameters (NOT in config.py — must not move hash)
ENTRY_TIME_ET = "10:00"      # CLOCK_1000: first scan at/after this ET clock
CONFIRM_N = 5                # CONFIRM_5: consecutive rank-1 scans before entry
_LOCKED_ENTRY_TIME_ET = "10:00"
_LOCKED_CONFIRM_N = 5

PAPER_RULES = ("FIRST_SEEN", "CLOCK_1000", "CONFIRM_5")
PAPER_VARIANTS = ("ALL", "0DTE", "1DTE+", "CONTROL")

# Shared by section_buckets and section_verdict — must never diverge.
# Single definition lives in scoring_pool.py (also used by the scorer).
from scoring_pool import DTE_BUCKET_SQL, dte_bucket as _dte_bucket_fn  # noqa: E402

# Live-quote window end (exclusive) — same constant mark_runner uses (09:30–16:15).
from mark_runner import MARK_WINDOW_END  # noqa: E402


def _et_clock_str(t: dtime) -> str:
    return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}"


def last_markable_flag_clock(horizon: str) -> str:
    """
    Latest ET flag clock whose mark due still falls inside the live-quote window.

    T+1h due = ts+1h → flags at/after (MARK_WINDOW_END − 1h) are unmarkable.
    T+1d due = ts+1d (same clock next day) → flags at/after MARK_WINDOW_END
    are unmarkable the next session.
    """
    end_dt = datetime.combine(date(2000, 1, 1), MARK_WINDOW_END)
    if horizon == "t1h":
        return _et_clock_str((end_dt - timedelta(hours=1)).time())
    if horizon == "t1d":
        return _et_clock_str(MARK_WINDOW_END)
    raise ValueError(f"last_markable_flag_clock: unsupported {horizon}")

# SQLite date()/time()/strftime() reinterpret offset-aware ISO strings
# (…T16:19:37-04:00 → 20:19:37 UTC wall). Read the stored ET fields instead.
def session_date_sql(column: str = "ts_et") -> str:
    """ET calendar date YYYY-MM-DD from an offset-aware ISO timestamp column."""
    return f"substr({column}, 1, 10)"


def et_clock_sql(column: str = "ts_et") -> str:
    """ET wall-clock HH:MM:SS from an offset-aware ISO timestamp column."""
    return f"substr({column}, 12, 8)"


@dataclass(frozen=True)
class ReportFilter:
    """Parsed CLI window — used to build table-specific WHERE clauses."""

    since: str | None = None          # session date >= since
    on_date: str | None = None        # session date = on_date
    ticker: str | None = None

    def where_sql(self, *, table_alias: str = "") -> tuple[str, list]:
        """
        Build an explicit WHERE for a given table (no string rewriting).

        table_alias: '' for bare columns, or 'f.' / 'v.' to qualify.
        Session dates use substr — never SQLite date() on offset-aware ISO.
        """
        p = f"{table_alias}." if table_alias and not table_alias.endswith(".") else table_alias
        sess = session_date_sql(f"{p}ts_et")
        clauses = ["1=1"]
        args: list = []
        if self.since is not None:
            clauses.append(f"{sess} >= ?")
            args.append(self.since)
        if self.on_date is not None:
            clauses.append(f"{sess} = ?")
            args.append(self.on_date)
        if self.ticker is not None:
            clauses.append(f"{p}ticker = ?")
            args.append(self.ticker)
        return " AND ".join(clauses), args


def max_hours_for(horizon: str) -> float | None:
    if horizon == "t1h":
        return MAX_HOURS_T1H
    if horizon == "t1d":
        return MAX_HOURS_T1D
    if horizon == "expiry":
        return MAX_HOURS_EXPIRY  # intentionally None — intrinsic, lag-irrelevant
    raise ValueError(f"unknown horizon: {horizon}")


def max_minutes_for(horizon: str) -> float:
    if horizon == "t15m":
        return MAX_MINUTES_T15M
    if horizon == "t30m":
        return MAX_MINUTES_T30M
    raise ValueError(f"not a short horizon: {horizon}")


def _short_method_col(horizon: str) -> str:
    return f"method_{horizon}"


def _short_minutes_col(horizon: str) -> str:
    return f"minutes_{horizon}"


class _Tee:
    """Write to stdout and an in-memory buffer (saved under report/)."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


def report_path(*, span_key: str, ticker: str | None, generated: datetime) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    parts = ["eod", span_key]
    if ticker:
        parts.append(ticker.upper())
    parts.append(generated.strftime("%Y%m%d_%H%M%S"))
    return os.path.join(REPORT_DIR, "_".join(parts) + ".txt")


def q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def pct(x):
    return "     -" if x is None else f"{x:+6.1%}"


def hr(title=""):
    print("\n" + (f"── {title} " + "─" * (66 - len(title)) if title else "─" * 68))


def count_late_marks(conn, where: str, args, horizon: str) -> int:
    """Rows with a return present but hours_* above the horizon cap."""
    max_h = max_hours_for(horizon)
    if max_h is None:
        return 0
    ret, hrs = f"ret_{horizon}", f"hours_{horizon}"
    return int(q(conn, f"""
        SELECT COUNT(*) FROM v_outcomes
        WHERE {where}
          AND {ret} IS NOT NULL
          AND {hrs} IS NOT NULL
          AND {hrs} > ?
    """, (*args, max_h))[0][0])


def clustered_bucket_rows(conn, where: str, args, horizon: str = "t1h"):
    """
    Clustered (date, contract) outcomes by dte_bucket × rank_bucket.
    Returns list of (dte_b, rank_b, n, mean, ex_out, tails, win).
    """
    ret, hrs = f"ret_{horizon}", f"hours_{horizon}"
    max_h = max_hours_for(horizon)
    guard = f"AND {hrs} <= {max_h}" if max_h is not None else ""
    return q(conn, f"""
        WITH pc AS (
          SELECT {DTE_BUCKET_SQL} AS dte_b,
                 CASE WHEN is_control = 1 THEN 'CONTROL'
                      WHEN rank <= 3  THEN '01-03'
                      WHEN rank <= 10 THEN '04-10'
                      WHEN rank <= 20 THEN '11-20'
                      ELSE '21+' END rank_b,
                 {session_date_sql("ts_et")} d,
                 ticker||side||strike||expiry contract,
                 AVG({ret}) r
          FROM v_outcomes
          WHERE {where} AND {ret} IS NOT NULL {guard}
          GROUP BY dte_b, rank_b, d, contract
        )
        SELECT dte_b, rank_b, COUNT(*) n,
               AVG(r) mean,
               AVG(CASE WHEN r < {OUTLIER} THEN r END) ex_out,
               SUM(r >= {OUTLIER}) tails,
               SUM(r > 0)*1.0/COUNT(*) win
        FROM pc GROUP BY dte_b, rank_b ORDER BY dte_b, rank_b
    """, args)


def verdict_rows(conn, where: str, args):
    """
    Clustered T+1h verdict rows for 1DTE+ only (same DTE_BUCKET_SQL as buckets).
    Returns list of (bucket, n, mean) where bucket in TOP3 / CONTROL / REST.
    """
    return q(conn, f"""
        WITH pc AS (
          SELECT CASE WHEN is_control = 1 THEN 'CONTROL'
                      WHEN rank <= 3 THEN 'TOP3' ELSE 'REST' END b,
                 {session_date_sql("ts_et")} d,
                 ticker||side||strike||expiry c,
                 AVG(ret_t1h) r
          FROM v_outcomes
          WHERE {where}
            AND ret_t1h IS NOT NULL
            AND hours_t1h <= {MAX_HOURS_T1H}
            AND ({DTE_BUCKET_SQL}) = '1DTE+'
          GROUP BY b, d, c
        )
        SELECT b, COUNT(*), AVG(r) FROM pc GROUP BY b
    """, args)


# ── sections ─────────────────────────────────────────────────────────────────

def section_coverage(conn, where, args):
    hr("COVERAGE")
    r = q(conn, f"""
        SELECT COUNT(*) flags,
               SUM(is_control) ctrl,
               COUNT(DISTINCT run_id) runs,
               COUNT(DISTINCT {session_date_sql("ts_et")}) days,
               COUNT(DISTINCT config_hash) engines,
               SUM(mark_t1h IS NOT NULL) m1h,
               SUM(mark_t1d IS NOT NULL) m1d,
               SUM(mark_expiry IS NOT NULL) mexp,
               SUM(mark_t15m IS NOT NULL) m15,
               SUM(mark_t30m IS NOT NULL) m30,
               SUM(method_t15m IN ('quote', 'trade')) ok15,
               SUM(method_t30m IN ('quote', 'trade')) ok30
        FROM v_outcomes WHERE {where}""", args)[0]
    (flags, ctrl, runs, days, engines, m1h, m1d, mexp,
     m15, m30, ok15, ok30) = r
    if not flags:
        print("  no rows — check --date / --ticker")
        return False
    m15 = int(m15 or 0)
    m30 = int(m30 or 0)
    ok15 = int(ok15 or 0)
    ok30 = int(ok30 or 0)
    print(f"  runs {runs:<6} days {days:<4} flags {flags:<7} controls {ctrl}")
    print(
        f"  marked   T+15m {m15:<6} T+30m {m30:<6} "
        f"T+1h {m1h:<7} T+1d {m1d:<7} expiry {mexp}"
    )
    print(
        f"  coverage T+15m {m15/flags:.0%}  T+30m {m30/flags:.0%}  "
        f"T+1h {m1h/flags:.0%}"
    )
    print(
        f"  usable   T+15m {ok15:<6} T+30m {ok30:<6} "
        f"(method quote|trade only — sealed stale/unavailable excluded)"
    )
    if engines > 1:
        print(f"  ⚠  {engines} distinct config_hash values — sample spans "
              f"multiple engines and must be segmented before interpreting")
    return True


def count_short_sealed(conn, where: str, args, horizon: str) -> tuple[int, int]:
    """Return (n_stale, n_unavailable) for a short horizon."""
    mcol = _short_method_col(horizon)
    row = q(conn, f"""
        SELECT SUM({mcol} = 'stale'),
               SUM({mcol} = 'unavailable')
        FROM v_outcomes WHERE {where}
    """, args)[0]
    return int(row[0] or 0), int(row[1] or 0)


def count_late_short_marks(conn, where: str, args, horizon: str) -> int:
    ret = f"ret_{horizon}"
    mins = _short_minutes_col(horizon)
    mcol = _short_method_col(horizon)
    max_m = max_minutes_for(horizon)
    return int(q(conn, f"""
        SELECT COUNT(*) FROM v_outcomes
        WHERE {where}
          AND {ret} IS NOT NULL
          AND {mcol} IN ('quote', 'trade')
          AND {mins} IS NOT NULL
          AND {mins} > ?
    """, (*args, max_m))[0][0])


def clustered_short_bucket_rows(conn, where: str, args, horizon: str):
    """
    Clustered short-horizon outcomes (ask-entry ret_* from v_outcomes).
    Only method in quote|trade; lag filter on minutes_*.
    """
    ret = f"ret_{horizon}"
    mins = _short_minutes_col(horizon)
    mcol = _short_method_col(horizon)
    max_m = max_minutes_for(horizon)
    return q(conn, f"""
        WITH pc AS (
          SELECT {DTE_BUCKET_SQL} AS dte_b,
                 CASE WHEN is_control = 1 THEN 'CONTROL'
                      WHEN rank <= 3  THEN '01-03'
                      WHEN rank <= 10 THEN '04-10'
                      WHEN rank <= 20 THEN '11-20'
                      ELSE '21+' END rank_b,
                 {session_date_sql("ts_et")} d,
                 ticker||side||strike||expiry contract,
                 AVG({ret}) r
          FROM v_outcomes
          WHERE {where}
            AND {ret} IS NOT NULL
            AND {mcol} IN ('quote', 'trade')
            AND ask IS NOT NULL AND ask > 0
            AND ({mins} IS NULL OR {mins} <= {max_m})
          GROUP BY dte_b, rank_b, d, contract
        )
        SELECT dte_b, rank_b, COUNT(*) n,
               AVG(r) mean,
               AVG(CASE WHEN r < {OUTLIER} THEN r END) ex_out,
               SUM(r >= {OUTLIER}) tails,
               SUM(r > 0)*1.0/COUNT(*) win
        FROM pc GROUP BY dte_b, rank_b ORDER BY dte_b, rank_b
    """, args)


def section_short_buckets(conn, where, args, horizon: str):
    """OUTCOMES @ T15M / T30M — ask entry, bid exit, quote|trade only."""
    max_m = max_minutes_for(horizon)
    mins = _short_minutes_col(horizon)
    excluded = count_late_short_marks(conn, where, args, horizon)
    n_stale, n_unavail = count_short_sealed(conn, where, args, horizon)
    title = (
        f"OUTCOMES @ {horizon.upper()}  (clustered, ask-entry / bid-exit, "
        f"{mins} <= {max_m:g}; excluded {excluded} late; "
        f"sealed stale={n_stale} unavailable={n_unavail})"
    )
    hr(title)

    rows = clustered_short_bucket_rows(conn, where, args, horizon)
    if not rows:
        print("  no usable short-horizon outcomes in window "
              "(need method quote|trade + ask > 0)")
        if n_stale or n_unavail:
            print(
                f"  sealed (not observations): stale={n_stale}  "
                f"unavailable={n_unavail}"
            )
        return rows

    print(f"  {'':<8} {'bucket':<9} {'n':>5} {'mean':>8} {'ex-tail':>8} "
          f"{'tails':>6} {'win%':>7}")
    last = None
    unknown_n = 0
    for dte_b, rank_b, n, mean, ex, tails, win in rows:
        lead = dte_b if dte_b != last else ""
        last = dte_b
        if dte_b == "UNKNOWN":
            unknown_n += int(n)
        print(f"  {lead:<8} {rank_b:<9} {n:>5} {pct(mean)} {pct(ex)} "
              f"{tails:>6} {win:>6.0%}")
    if unknown_n > 0:
        print(
            f"\n  ⚠  UNKNOWN n={unknown_n}: rows predate the dte column and "
            f"cannot be classified — never merge into 0DTE or 1DTE+"
        )
    print("\n  mean    = ask-entry / bid-exit return (trade we actually make)")
    print("  ex-tail = mean with >= +100% removed")
    print(
        f"  sealed excluded from n: stale={n_stale}  unavailable={n_unavail}"
    )
    return rows


def section_buckets(conn, where, args, horizon="t1h"):
    """Clustered by (date, contract). One observation per contract per day."""
    hrs = f"hours_{horizon}"
    max_h = max_hours_for(horizon)
    excluded = count_late_marks(conn, where, args, horizon)
    if max_h is None:
        title = (
            f"OUTCOMES @ {horizon.upper()}  (clustered, no lag filter — "
            f"intrinsic/settlement)"
        )
    else:
        title = (
            f"OUTCOMES @ {horizon.upper()}  (clustered, {hrs} <= {max_h}"
            f"; excluded {excluded} marks taken > {max_h}h late)"
        )
    hr(title)
    if horizon == "t1h":
        # Live-quote window ends at MARK_WINDOW_END; T+1h due past that is
        # unmarkable — final-hour flags never enter the T1H sample.
        cut_hm = last_markable_flag_clock("t1h")[:5]
        print(
            f"  note: excludes flags after ~{cut_hm} ET "
            f"(T+1h falls past the quote window)"
        )
        print(
            "        — hour_et buckets for the final hour are "
            "structurally empty at T1H"
        )

    rows = clustered_bucket_rows(conn, where, args, horizon)
    if not rows:
        print("  no marked outcomes in window")
        return rows

    print(f"  {'':<8} {'bucket':<9} {'n':>5} {'mean':>8} {'ex-tail':>8} "
          f"{'tails':>6} {'win%':>7}")
    last = None
    unknown_n = 0
    for dte_b, rank_b, n, mean, ex, tails, win in rows:
        lead = dte_b if dte_b != last else ""
        last = dte_b
        if dte_b == "UNKNOWN":
            unknown_n += int(n)
        print(f"  {lead:<8} {rank_b:<9} {n:>5} {pct(mean)} {pct(ex)} "
              f"{tails:>6} {win:>6.0%}")
    if unknown_n > 0:
        print(
            f"\n  ⚠  UNKNOWN n={unknown_n}: rows predate the dte column and "
            f"cannot be classified — never merge into 0DTE or 1DTE+"
        )
    print("\n  mean    = what an equal-weight basket returned (this is your P&L)")
    print("  ex-tail = mean with >= +100% removed (is the edge broad or 3 lucky rows?)")
    return rows


def section_verdict(conn, where, args):
    hr("VERDICT")
    rows = verdict_rows(conn, where, args)
    d = {b: (n, m) for b, n, m in rows}
    top, ctl = d.get("TOP3"), d.get("CONTROL")
    if not top or not ctl:
        print("  insufficient data — need both TOP3 and CONTROL rows (1DTE+)")
        return rows
    edge = top[1] - ctl[1]
    print(f"  1DTE+ TOP3   n={top[0]:<5} {pct(top[1])}")
    print(f"  1DTE+ CONTROL n={ctl[0]:<5} {pct(ctl[1])}")
    print(f"  edge over control: {edge:+.1%}")
    n = min(top[0], ctl[0])
    if n < 200:
        print(f"  ⚠  n={n} is too small to call. Need ~200+ contract-days per bucket.")
    elif abs(edge) < 0.02:
        print("  → no meaningful edge over picking nearest-ATM")
    elif edge > 0:
        print("  → engine beats control. Check it holds across days before believing it.")
    else:
        print("  → control beats the engine.")
    return rows


# ── paper strategy ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PaperTrade:
    rule: str
    session: str
    ticker: str
    contract: str
    entry_mid: float
    mark_close: float
    pnl: float
    dte_bucket: str          # 0DTE / 1DTE+ / UNKNOWN
    persist_scans: int       # consecutive rank-1 (or control) scans from entry
    is_control: bool


def _dte_bucket(dte) -> str:
    return _dte_bucket_fn(dte)


def _persist_from(scans: list, start_idx: int, contract: str) -> int:
    """Count consecutive scans from start_idx where contract stays selected."""
    n = 0
    for i in range(start_idx, len(scans)):
        if scans[i]["contract"] != contract:
            break
        n += 1
    return n


def _pick_entry_first_seen(scans: list) -> int | None:
    return 0 if scans else None


def _pick_entry_clock(scans: list) -> int | None:
    for i, s in enumerate(scans):
        if s["clock"] >= ENTRY_TIME_ET:
            return i
    return None


def _pick_entry_confirm(scans: list) -> int | None:
    if CONFIRM_N < 1:
        return None
    streak_c = None
    streak_n = 0
    for i, s in enumerate(scans):
        c = s["contract"]
        if c == streak_c:
            streak_n += 1
        else:
            streak_c = c
            streak_n = 1
        if streak_n >= CONFIRM_N:
            return i
    return None


_ENTRY_PICKERS = {
    "FIRST_SEEN": _pick_entry_first_seen,
    "CLOCK_1000": _pick_entry_clock,
    "CONFIRM_5": _pick_entry_confirm,
}


def _row_to_scan(r) -> dict:
    mc = r["mark_close"]
    return {
        "clock": r["clock"],
        "ts_et": r["ts_et"],
        "contract": r["contract"],
        "mid": float(r["mid"]),
        "mark_close": None if mc is None else float(mc),
        "dte_bucket": _dte_bucket(r["dte"]),
    }


def _load_paper_scans(
    conn,
    where: str,
    args,
    *,
    control: bool,
) -> dict[tuple[str, str], list[dict]]:
    """
    Scans keyed by (session, ticker), ordered by ts_et.

    Engine path: rank = 1 within pool, is_control = 0.
    Post-v1.2: both pools may have rank=1 in one run — load both; variants
    filter by dte_bucket. Pre-migration (pool NULL): treat as 1DTE+ timeline.

    Control path: is_control = 1; one row per (run_id, pool) (prefer CALL).

    Requires conn.row_factory = sqlite3.Row.
    """
    if control:
        sql = f"""
            SELECT {session_date_sql("ts_et")} AS sess,
                   {et_clock_sql("ts_et")} AS clock,
                   ts_et, run_id, ticker, side, strike, expiry, dte,
                   mid, mark_close, pool,
                   ticker || side || strike || expiry AS contract
            FROM flags
            WHERE {where}
              AND is_control = 1
              AND mid IS NOT NULL AND mid > 0
            ORDER BY sess, ticker, ts_et,
                     CASE pool WHEN '1DTE+' THEN 0 WHEN '0DTE' THEN 1 ELSE 2 END,
                     CASE side WHEN 'CALL' THEN 0 ELSE 1 END
        """
        rows = q(conn, sql, args)
        by: dict[tuple[str, str], list[dict]] = defaultdict(list)
        # One control per (run, pool) — not one per run (two pools now)
        seen_run: set[tuple[str, str, str, str]] = set()
        for r in rows:
            key = (r["sess"], r["ticker"])
            pool_k = r["pool"] if r["pool"] is not None else ""
            run_key = (r["sess"], r["ticker"], r["run_id"], pool_k)
            if run_key in seen_run:
                continue
            seen_run.add(run_key)
            by[key].append(_row_to_scan(r))
        return by

    sql = f"""
        SELECT {session_date_sql("ts_et")} AS sess,
               {et_clock_sql("ts_et")} AS clock,
               ts_et, ticker, side, strike, expiry, dte,
               mid, mark_close, pool,
               ticker || side || strike || expiry AS contract
        FROM flags
        WHERE {where}
          AND is_control = 0
          AND rank = 1
          AND mid IS NOT NULL AND mid > 0
        ORDER BY sess, ticker, ts_et,
                 CASE pool WHEN '1DTE+' THEN 0 WHEN '0DTE' THEN 1 ELSE 2 END
    """
    rows = q(conn, sql, args)
    by = defaultdict(list)
    for r in rows:
        by[(r["sess"], r["ticker"])].append(_row_to_scan(r))
    return by


def paper_trades_for_rule(
    scans_by_day: dict[tuple[str, str], list[dict]],
    rule: str,
    *,
    is_control: bool,
) -> list[PaperTrade]:
    """
    One trade per (rule, session, contract). Re-entries of the same contract
    the same day collapse — the first entry under the rule wins.
    """
    picker = _ENTRY_PICKERS[rule]
    out: list[PaperTrade] = []
    seen: set[tuple[str, str, str]] = set()  # session, ticker, contract
    for (sess, ticker), scans in sorted(scans_by_day.items()):
        idx = picker(scans)
        if idx is None:
            continue
        s = scans[idx]
        contract = s["contract"]
        key = (sess, ticker, contract)
        if key in seen:
            continue
        if s["mark_close"] is None:
            continue
        seen.add(key)
        pnl = (s["mark_close"] - s["mid"]) * 100.0
        out.append(PaperTrade(
            rule=rule,
            session=sess,
            ticker=ticker,
            contract=contract,
            entry_mid=s["mid"],
            mark_close=s["mark_close"],
            pnl=pnl,
            dte_bucket=s["dte_bucket"],
            persist_scans=_persist_from(scans, idx, contract),
            is_control=is_control,
        ))
    return out


def _summarize_pnls(trades: list[PaperTrade]) -> dict:
    if not trades:
        return {
            "n_days": 0, "n": 0, "win": None, "total": None, "mean": None,
            "median": None, "best": None, "worst": None, "persist": None,
        }
    pnls = [t.pnl for t in trades]
    days = len({t.session for t in trades})
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n_days": days,
        "n": len(trades),
        "win": wins / len(pnls),
        "total": sum(pnls),
        "mean": statistics.mean(pnls),
        "median": statistics.median(pnls),
        "best": max(pnls),
        "worst": min(pnls),
        "persist": statistics.mean(t.persist_scans for t in trades),
    }


def _fmt_money(x) -> str:
    if x is None:
        return "      -"
    return f"{x:+8.0f}"


def _fmt_win(x) -> str:
    if x is None:
        return "    -"
    return f"{x:5.0%}"


def section_paper_strategy(conn, filt: ReportFilter):
    """
    Buy-one-contract, hold-to-close under three fixed entry rules.

    Anti-curve-fitting: rules and parameters are locked; do not search over
    entry times / confirm counts or pick entries using the exit.
    """
    hr("PAPER STRATEGY")
    print(
        "  three entry rules, fixed in advance — "
        "do not add, remove, or tune after seeing results"
    )
    print(
        f"  params  ENTRY_TIME_ET={ENTRY_TIME_ET}  CONFIRM_N={CONFIRM_N}  "
        f"exit=mark_close  pnl=(mark_close-mid)*100"
    )
    if (
        ENTRY_TIME_ET != _LOCKED_ENTRY_TIME_ET
        or CONFIRM_N != _LOCKED_CONFIRM_N
    ):
        print(
            "  ⚠  ENTRY_TIME_ET/CONFIRM_N differ from locked defaults "
            f"({_LOCKED_ENTRY_TIME_ET} / {_LOCKED_CONFIRM_N}) — "
            "results across parameter values are not comparable"
        )

    where, args = filt.where_sql()
    engine_scans = _load_paper_scans(conn, where, args, control=False)
    control_scans = _load_paper_scans(conn, where, args, control=True)

    # Precompute all trades for each rule
    engine_by_rule = {
        rule: paper_trades_for_rule(engine_scans, rule, is_control=False)
        for rule in PAPER_RULES
    }
    control_by_rule = {
        rule: paper_trades_for_rule(control_scans, rule, is_control=True)
        for rule in PAPER_RULES
    }

    n_close = q(conn, f"""
        SELECT COUNT(*) FROM flags
        WHERE {where} AND mark_close IS NOT NULL
    """, args)[0][0]
    if not n_close:
        print(
            "\n  no mark_close rows in window — paper P&L empty until "
            "mark_runner writes session-close marks"
        )

    header = (
        f"  {'variant':<8} {'n_days':>6} {'n':>4} {'win%':>6} "
        f"{'total$':>8} {'mean$':>8} {'med$':>8} "
        f"{'best':>8} {'worst':>8} {'persist':>7}"
    )

    for rule in PAPER_RULES:
        print(f"\n  {rule}")
        print(header)
        engine = engine_by_rule[rule]
        control = control_by_rule[rule]
        buckets = {
            "ALL": list(engine),
            "0DTE": [t for t in engine if t.dte_bucket == "0DTE"],
            "1DTE+": [t for t in engine if t.dte_bucket == "1DTE+"],
            "CONTROL": control,
        }

        for variant in PAPER_VARIANTS:
            s = _summarize_pnls(buckets[variant])
            persist = (
                "      -" if s["persist"] is None else f"{s['persist']:7.1f}"
            )
            print(
                f"  {variant:<8} {s['n_days']:>6} {s['n']:>4} "
                f"{_fmt_win(s['win'])} "
                f"{_fmt_money(s['total'])} {_fmt_money(s['mean'])} "
                f"{_fmt_money(s['median'])} {_fmt_money(s['best'])} "
                f"{_fmt_money(s['worst'])} {persist}"
            )

    print(
        "\n  persist = mean consecutive rank-1 (or control) scans from entry "
        "(diagnostic only; not extra observations)"
    )
    print(
        "  clustering: one trade per (rule, date, contract) — "
        "re-entries the same day do not add n"
    )


def _pool_expr_flags() -> str:
    """Prefer flags.pool; fall back to dte CASE (never silent-merge NULL)."""
    return f"""CASE
        WHEN pool IN ('0DTE', '1DTE+') THEN pool
        ELSE ({DTE_BUCKET_SQL})
    END"""


def _factor_quartile_edges(values: list[float]) -> tuple[float, float, float] | None:
    """Three cut points for Q1|Q2|Q3|Q4 from the full window sample."""
    clean = sorted(float(v) for v in values if v is not None and v == v)
    if len(clean) < 4:
        return None
    try:
        cuts = statistics.quantiles(clean, n=4, method="inclusive")
    except statistics.StatisticsError:
        return None
    if len(cuts) != 3:
        return None
    return float(cuts[0]), float(cuts[1]), float(cuts[2])


def _bucket_label_fixed(var: str, value) -> str | None:
    """Fixed pre-registered edges — do not retune."""
    if var == "hour_et":
        try:
            h = int(value)
        except (TypeError, ValueError):
            return None
        if 10 <= h <= 15:
            return f"{h:02d}"
        return None
    if value is None:
        if var == "delta":
            return "NULL"
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        if var == "delta":
            return "NULL"
        return None
    if var == "iv":
        if v < 0.20:
            return "<0.20"
        if v <= 0.35:
            return "0.20-0.35"
        return ">0.35"
    if var == "spread_pct":
        if v < 0.03:
            return "<0.03"
        if v <= 0.08:
            return "0.03-0.08"
        return ">0.08"
    if var == "delta":
        if v < 0.15:
            return "<0.15"
        if v <= 0.40:
            return "0.15-0.40"
        return ">0.40"
    if var == "iv_premium":
        if v < 0.5:
            return "<0.5"
        if v <= 1.0:
            return "0.5-1.0"
        return ">1.0"
    if var == "vol_oi":
        if v < 1.0:
            return "<1"
        if v <= 10.0:
            return "1-10"
        return ">10"
    return None


def _quartile_label(value, edges: tuple[float, float, float]) -> str:
    v = float(value)
    q1, q2, q3 = edges
    if v <= q1:
        return "Q1"
    if v <= q2:
        return "Q2"
    if v <= q3:
        return "Q3"
    return "Q4"


# Display order for fixed buckets (quartiles appended dynamically).
_FACTOR_BUCKET_ORDER = {
    "iv": ("<0.20", "0.20-0.35", ">0.35"),
    "spread_pct": ("<0.03", "0.03-0.08", ">0.08"),
    "delta": ("NULL", "<0.15", "0.15-0.40", ">0.40"),
    "hour_et": ("10", "11", "12", "13", "14", "15"),
    "iv_premium": ("<0.5", "0.5-1.0", ">1.0"),
    "vol_oi": ("<1", "1-10", ">10"),
    "flow_raw": ("Q1", "Q2", "Q3", "Q4"),
    "leverage_raw": ("Q1", "Q2", "Q3", "Q4"),
}


def _load_factor_observations(
    conn,
    *,
    since: str,
    until: str,
    ticker: str | None,
) -> list[dict]:
    """
    One clustered (session, contract, pool, is_control) observation with
    T15M ask-entry return and factor inputs from flags (read-only).
    """
    sess = session_date_sql("ts_et")
    pool_x = _pool_expr_flags()
    clauses = [
        f"{sess} >= ?",
        f"{sess} <= ?",
        "method_t15m IN ('quote', 'trade')",
        "mark_t15m IS NOT NULL",
        "ask IS NOT NULL AND ask > 0",
        f"{pool_x} IN ('0DTE', '1DTE+')",
    ]
    args: list = [since, until]
    if ticker:
        clauses.append("ticker = ?")
        args.append(ticker)
    where = " AND ".join(clauses)
    rows = q(conn, f"""
        SELECT {sess} AS sess,
               ticker || side || strike || expiry AS contract,
               {pool_x} AS pool,
               is_control,
               AVG((mark_t15m - ask) / ask) AS ret,
               AVG(iv) AS iv,
               AVG(CASE
                     WHEN mid IS NOT NULL AND mid > 0
                          AND ask IS NOT NULL AND bid IS NOT NULL
                     THEN (ask - bid) / mid
                   END) AS spread_pct,
               AVG(ABS(delta)) AS delta_abs,
               SUM(CASE WHEN delta IS NULL THEN 1 ELSE 0 END) AS delta_nulls,
               COUNT(*) AS n_scans,
               CAST(substr(MIN(ts_et), 12, 2) AS INTEGER) AS hour_et,
               AVG(flow_raw) AS flow_raw,
               AVG(leverage_raw) AS leverage_raw,
               AVG(iv_premium) AS iv_premium,
               AVG(CASE
                     WHEN open_interest IS NOT NULL AND open_interest > 0
                     THEN CAST(volume AS REAL) / open_interest
                   END) AS vol_oi
        FROM flags
        WHERE {where}
        GROUP BY sess, contract, pool, is_control
    """, args)
    out = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else None
        if d is None:
            continue
        # delta: if every scan null → NULL bucket; else use avg |delta|
        if int(d["delta_nulls"] or 0) >= int(d["n_scans"] or 0):
            d["delta"] = None
        else:
            d["delta"] = d["delta_abs"]
        out.append(d)
    return out


def section_factor_separation(
    conn,
    *,
    since: str,
    until: str,
    ticker: str | None,
):
    """
    Diagnostic: which logged variables separate T15M winners from losers.
    Aggregates window-to-date. Does not feed scoring.
    """
    hr("FACTOR SEPARATION")
    print(
        "  diagnostic only — do not act on these until the window closes "
        f"{WINDOW_END_NOTE}"
    )
    obs = _load_factor_observations(
        conn, since=since, until=until, ticker=ticker,
    )
    sessions = sorted({o["sess"] for o in obs}) if obs else []
    print(
        f"  range {since} → {until}  sessions={len(sessions)}  "
        f"clustered_obs={len(obs)}"
        f"{'  ticker=' + ticker if ticker else ''}"
    )
    print("  returns = (mark_t15m - ask) / ask  |  method quote|trade only")
    if not obs:
        print("  no usable T15M observations in factor window")
        return

    # Quartile edges over FULL window to date, per pool (engine rows only so
    # control density does not shift cuts — still report CONTROL in buckets).
    q_edges: dict[tuple[str, str], tuple[float, float, float]] = {}
    for pool in ("0DTE", "1DTE+"):
        for var in ("flow_raw", "leverage_raw"):
            vals = [
                float(o[var])
                for o in obs
                if o["pool"] == pool
                and not o["is_control"]
                and o[var] is not None
                and o[var] == o[var]
            ]
            edges = _factor_quartile_edges(vals)
            if edges:
                q_edges[(pool, var)] = edges

    fixed_vars = (
        "iv", "spread_pct", "delta", "hour_et",
        "flow_raw", "leverage_raw", "iv_premium", "vol_oi",
    )

    for pool in ("0DTE", "1DTE+"):
        pool_obs = [o for o in obs if o["pool"] == pool]
        print(f"\n  ── {pool}  (n_obs={len(pool_obs)}) ──")
        if not pool_obs:
            print("  (no observations)")
            continue

        for var in fixed_vars:
            print(f"\n  {var}")
            if var in ("flow_raw", "leverage_raw"):
                edges = q_edges.get((pool, var))
                if not edges:
                    print("  (insufficient data for quartile edges)")
                    continue
                print(
                    f"  quartile cuts (engine, window): "
                    f"{edges[0]:.4g} | {edges[1]:.4g} | {edges[2]:.4g}"
                )
            print(f"  {'bucket':<12} {'who':<8} {'n':>5} {'mean':>8} {'win%':>7}")

            # Assign labels
            labeled: list[tuple[str, dict]] = []
            for o in pool_obs:
                if var in ("flow_raw", "leverage_raw"):
                    edges = q_edges.get((pool, var))
                    if not edges or o[var] is None or o[var] != o[var]:
                        continue
                    lab = _quartile_label(o[var], edges)
                elif var == "hour_et":
                    lab = _bucket_label_fixed(var, o.get("hour_et"))
                else:
                    lab = _bucket_label_fixed(var, o.get(var))
                if lab is None:
                    continue
                labeled.append((lab, o))

            order = list(_FACTOR_BUCKET_ORDER[var])
            seen_extra = sorted({lab for lab, _ in labeled if lab not in order})
            for lab in order + seen_extra:
                for who, ctrl_flag in (("engine", 0), ("CONTROL", 1)):
                    subset = [
                        o for lab_i, o in labeled
                        if lab_i == lab and int(o["is_control"] or 0) == ctrl_flag
                    ]
                    if not subset and ctrl_flag == 1:
                        continue  # skip empty CONTROL lines
                    if not subset and ctrl_flag == 0:
                        # still print empty engine buckets? skip for noise
                        continue
                    n = len(subset)
                    rets = [float(o["ret"]) for o in subset]
                    mean = statistics.mean(rets) if rets else None
                    win = (
                        sum(1 for r in rets if r > 0) / n if n else None
                    )
                    low = " (low n)" if n < FACTOR_LOW_N else ""
                    win_s = "     -" if win is None else f"{win:>6.0%}"
                    print(
                        f"  {lab:<12} {who:<8} {n:>5} {pct(mean)} {win_s}{low}"
                    )


def section_flags(conn, filt: ReportFilter):
    """Data-quality checks with explicit WHERE per table (no string rewrite)."""
    hr("DATA-QUALITY FLAGS")
    out = []
    v_where, v_args = filt.where_sql()
    f_where, f_args = filt.where_sql()  # flags shares ts_et / ticker columns

    n = q(conn, f"SELECT COUNT(*) FROM v_outcomes WHERE {v_where} AND score > 1.0",
          v_args)[0][0]
    if n:
        out.append(f"{n} rows scored above 1.0  (F-03: multipliers uncapped)")

    n = q(conn, f"""SELECT COUNT(*) FROM v_outcomes
                    WHERE {v_where} AND hours_t1h > ?""",
          (*v_args, MAX_HOURS_T1H))[0][0]
    if n:
        out.append(f"{n} T+1h marks taken > {MAX_HOURS_T1H}h late (excluded above)")

    n = q(conn, f"""SELECT COUNT(*) FROM v_outcomes
                    WHERE {v_where} AND hours_t1d > ?""",
          (*v_args, MAX_HOURS_T1D))[0][0]
    if n:
        out.append(f"{n} T+1d marks taken > {MAX_HOURS_T1D}h late (excluded above)")

    for horizon, label in (("t15m", "T+15m"), ("t30m", "T+30m")):
        n_stale, n_unavail = count_short_sealed(conn, v_where, v_args, horizon)
        if n_stale:
            out.append(
                f"{n_stale} {label} sealed method=stale "
                f"(not an observation)"
            )
        if n_unavail:
            out.append(
                f"{n_unavail} {label} sealed method=unavailable "
                f"(due after 16:00 ET — not an observation)"
            )
        n_late = count_late_short_marks(conn, v_where, v_args, horizon)
        if n_late:
            out.append(
                f"{n_late} {label} marks taken > "
                f"{max_minutes_for(horizon):g}m late (excluded above)"
            )

    n = q(conn, f"""SELECT COUNT(*) FROM flags
                    WHERE {f_where} AND notes LIKE '%stale:%'""", f_args)[0][0]
    if n:
        out.append(f"{n} rows refused as stale — permanently unmarkable")

    r = q(conn, f"""
        SELECT AVG(open_interest) FROM flags
        WHERE {f_where} AND rank <= 3 AND is_control = 0
          AND open_interest IS NOT NULL
        """, f_args)
    if r and r[0][0] is not None and r[0][0] < 200:
        out.append(f"top-3 avg open interest {r[0][0]:.0f} — vol/OI likely inflated")

    info: list[str] = []
    # Split null marks: late-session flags whose due falls past the live-quote
    # window (MARK_WINDOW_END) are structurally unmarkable — not a runner fault.
    now_et = datetime.now(ET)
    clock = "substr(ts_et, 12, 8)"
    win_hm = _et_clock_str(MARK_WINDOW_END)[:5]
    # Age before a null mark is treated as overdue (due lag + slack).
    overdue_age = {"t1h": timedelta(hours=4), "t1d": timedelta(hours=28)}

    for horizon, label in (("t1h", "T+1h"), ("t1d", "T+1d")):
        mark_col = f"mark_{horizon}"
        last_ok = last_markable_flag_clock(horizon)
        # Unmarkable: due (flag + lag) at/after quote-window close.
        n_unmark = int(q(conn, f"""
            SELECT COUNT(*) FROM v_outcomes
            WHERE {v_where}
              AND {mark_col} IS NULL
              AND {clock} >= ?
        """, (*v_args, last_ok))[0][0])
        if n_unmark:
            info.append(
                f"{n_unmark} rows unmarkable for {label} "
                f"(due after {win_hm} ET — late-session flags)"
            )
        # Genuinely overdue: due was inside the window, flag old enough, still null.
        cutoff = (now_et - overdue_age[horizon]).isoformat(timespec="seconds")
        n_over = int(q(conn, f"""
            SELECT COUNT(*) FROM v_outcomes
            WHERE {v_where}
              AND {mark_col} IS NULL
              AND ts_et < ?
              AND {clock} < ?
        """, (*v_args, cutoff, last_ok))[0][0])
        if n_over:
            out.append(
                f"{n_over} rows overdue for a {label} mark — "
                f"is mark_runner alive?"
            )

    lines = []
    for x in info:
        lines.append(f"i  {x}")
    for x in out:
        lines.append(f"⚠ {x}")
    print("  " + ("\n  ".join(lines) if lines else "none"))


# ── main ─────────────────────────────────────────────────────────────────────

def parse_filter(a: argparse.Namespace) -> tuple[ReportFilter, str, str]:
    """Return (filter, human span label, filename span_key)."""
    if a.days:
        since = (date.today() - timedelta(days=a.days)).isoformat()
        filt = ReportFilter(
            since=since,
            ticker=a.ticker.upper() if a.ticker else None,
        )
        return filt, f"last {a.days} days", f"last{a.days}d"
    d = a.date or date.today().isoformat()
    filt = ReportFilter(
        on_date=d,
        ticker=a.ticker.upper() if a.ticker else None,
    )
    return filt, d, d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--days", type=int, help="rolling window instead of one day")
    p.add_argument("--ticker")
    p.add_argument("--db", default=DB)
    p.add_argument(
        "--factor-since",
        default=WINDOW_START,
        help=(
            f"start date for FACTOR SEPARATION window "
            f"(default: {WINDOW_START})"
        ),
    )
    p.add_argument(
        "--out",
        default=None,
        help="explicit output path (default: report/eod_<span>[_TICKER]_<ts>.txt)",
    )
    a = p.parse_args(argv)

    filt, span, span_key = parse_filter(a)
    w, args = filt.where_sql()
    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

    # Factor section: window start → report date (not just today's session).
    factor_since = a.factor_since or WINDOW_START
    if filt.on_date:
        factor_until = filt.on_date
    else:
        factor_until = date.today().isoformat()
    if factor_since > factor_until:
        factor_since = factor_until

    generated = datetime.now()
    out_path = a.out or report_path(
        span_key=span_key, ticker=filt.ticker, generated=generated,
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = _Tee(old_stdout, buf)
    try:
        print("=" * 68)
        print(f"  SCANNER REPORT — {span}"
              f"{' — ' + filt.ticker if filt.ticker else ''}")
        print(f"  generated {generated.strftime('%Y-%m-%d %H:%M')}")
        print(f"  db {a.db}")
        print("=" * 68)

        if not section_coverage(conn, w, args):
            text = buf.getvalue()
            with open(out_path, "w", encoding="utf-8") as fh:
                fh.write(text)
            print(f"\n  wrote {out_path}", file=old_stdout)
            return 1
        section_short_buckets(conn, w, args, "t15m")
        section_short_buckets(conn, w, args, "t30m")
        section_buckets(conn, w, args, "t1h")
        section_buckets(conn, w, args, "t1d")
        section_verdict(conn, w, args)
        section_paper_strategy(conn, filt)
        section_factor_separation(
            conn,
            since=factor_since,
            until=factor_until,
            ticker=filt.ticker,
        )
        section_flags(conn, filt)
        print()
        print("── SAVED ─────────────────────────────────────────────────────────")
        print(f"  {out_path}")
        print()
    finally:
        sys.stdout = old_stdout

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(buf.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
