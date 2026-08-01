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
from datetime import date, datetime, timedelta
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
OUTLIER = 1.0                # returns >= +100% treated as tail events

# ── Paper-strategy analysis parameters (NOT in config.py — must not move hash)
ENTRY_TIME_ET = "10:00"      # CLOCK_1000: first scan at/after this ET clock
CONFIRM_N = 5                # CONFIRM_5: consecutive rank-1 scans before entry
_LOCKED_ENTRY_TIME_ET = "10:00"
_LOCKED_CONFIRM_N = 5

PAPER_RULES = ("FIRST_SEEN", "CLOCK_1000", "CONFIRM_5")
PAPER_VARIANTS = ("ALL", "0DTE", "1DTE+", "CONTROL")

# Shared by section_buckets and section_verdict — must never diverge.
# NULL dte (pre-migration rows) is UNKNOWN, never silently pooled into 1DTE+.
DTE_BUCKET_SQL = """CASE
    WHEN dte IS NULL THEN 'UNKNOWN'
    WHEN dte = 0 THEN '0DTE'
    ELSE '1DTE+'
END"""

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
               SUM(mark_expiry IS NOT NULL) mexp
        FROM v_outcomes WHERE {where}""", args)[0]
    flags, ctrl, runs, days, engines, m1h, m1d, mexp = r
    if not flags:
        print("  no rows — check --date / --ticker")
        return False
    print(f"  runs {runs:<6} days {days:<4} flags {flags:<7} controls {ctrl}")
    print(f"  marked   T+1h {m1h:<7} T+1d {m1d:<7} expiry {mexp}")
    print(f"  coverage T+1h {m1h/flags:.0%}")
    if engines > 1:
        print(f"  ⚠  {engines} distinct config_hash values — sample spans "
              f"multiple engines and must be segmented before interpreting")
    return True


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
    if dte is None:
        return "UNKNOWN"
    try:
        return "0DTE" if int(dte) == 0 else "1DTE+"
    except (TypeError, ValueError):
        return "UNKNOWN"


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

    Engine path: rank = 1, is_control = 0.
    Control path: is_control = 1 (rank is NULL on controls); one row per run
    (prefer CALL when both sides exist).

    Requires conn.row_factory = sqlite3.Row.
    """
    if control:
        sql = f"""
            SELECT {session_date_sql("ts_et")} AS sess,
                   {et_clock_sql("ts_et")} AS clock,
                   ts_et, run_id, ticker, side, strike, expiry, dte,
                   mid, mark_close,
                   ticker || side || strike || expiry AS contract
            FROM flags
            WHERE {where}
              AND is_control = 1
              AND mid IS NOT NULL AND mid > 0
            ORDER BY sess, ticker, ts_et, CASE side WHEN 'CALL' THEN 0 ELSE 1 END
        """
        rows = q(conn, sql, args)
        by: dict[tuple[str, str], list[dict]] = defaultdict(list)
        seen_run: set[tuple[str, str, str]] = set()
        for r in rows:
            key = (r["sess"], r["ticker"])
            run_key = (r["sess"], r["ticker"], r["run_id"])
            if run_key in seen_run:
                continue
            seen_run.add(run_key)
            by[key].append(_row_to_scan(r))
        return by

    sql = f"""
        SELECT {session_date_sql("ts_et")} AS sess,
               {et_clock_sql("ts_et")} AS clock,
               ts_et, ticker, side, strike, expiry, dte,
               mid, mark_close,
               ticker || side || strike || expiry AS contract
        FROM flags
        WHERE {where}
          AND is_control = 0
          AND rank = 1
          AND mid IS NOT NULL AND mid > 0
        ORDER BY sess, ticker, ts_et
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

    # Compare against an ET-offset ISO cutoff — never datetime('now') vs ts_et.
    cutoff = (datetime.now(ET) - timedelta(hours=4)).isoformat(timespec="seconds")
    n = q(conn, f"""SELECT COUNT(*) FROM v_outcomes WHERE {v_where}
                     AND mark_t1h IS NULL AND ts_et < ?""",
          (*v_args, cutoff))[0][0]
    if n:
        out.append(f"{n} rows overdue for a T+1h mark — is mark_runner alive?")

    print("  " + ("\n  ".join(f"⚠ {x}" for x in out) if out else "none"))


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
        "--out",
        default=None,
        help="explicit output path (default: report/eod_<span>[_TICKER]_<ts>.txt)",
    )
    a = p.parse_args(argv)

    filt, span, span_key = parse_filter(a)
    w, args = filt.where_sql()
    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

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
        section_buckets(conn, w, args, "t1h")
        section_buckets(conn, w, args, "t1d")
        section_verdict(conn, w, args)
        section_paper_strategy(conn, filt)
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
