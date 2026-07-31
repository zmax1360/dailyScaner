#!/usr/bin/env python3
"""
mark_runner.py — Write-once T+1h / T+1d / expiry marks for attribution flags.

  python mark_runner.py --dry-run
  python mark_runner.py
  python mark_runner.py --expiry-only
  python mark_runner.py --force          # ignore market-hours gate

Expiry marks use intrinsic value from the underlying's close on the expiry
date (expired contracts are absent from live chains). Horizons run in priority
order: t1h → t1d → expiry. A wall-clock runtime cap exits 0 so launchd's next
StartInterval gets a clean slot.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import time
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

from attribution import (
    default_db_path,
    due_for_marking,
    fetch_option_mid,
    note_mark_failure,
    note_stale_horizon,
    now_et,
    write_mark,
)
from logging_config import setup_logging

ET = ZoneInfo("America/New_York")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

# Single source of truth for the t1h/t1d mark window (also used by health_check).
MARK_WINDOW_START = dtime(9, 30)
MARK_WINDOW_END = dtime(16, 15)
# Full weekday mark window length (09:30–16:15) — t1d ceiling = one session.
_SESSION_LEN = datetime.combine(datetime(2000, 1, 1).date(), MARK_WINDOW_END) - datetime.combine(
    datetime(2000, 1, 1).date(), MARK_WINDOW_START
)
SESSION_LEN_HOURS = _SESSION_LEN.total_seconds() / 3600.0  # 6.75
T1H_STALE_MARKET_HOURS = 4.0

# Time-sensitive first — never starve t1h behind expiry backfill.
HORIZON_PRIORITY: tuple[str, ...] = ("t1h", "t1d", "expiry")

log = logging.getLogger("mark_runner")


def _load_env() -> None:
    """Inject .env into os.environ if keys are unset."""
    try:
        with open(ENV_FILE) as fh:
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


def _cfg_float(key: str, default: float) -> float:
    try:
        from config import SCORING
        return float(SCORING.get(key, default))
    except Exception:
        return float(default)


def in_mark_window(now: datetime | None = None) -> bool:
    """Weekdays 09:30–16:15 ET (buffer for delayed quotes after the close)."""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    now = now.astimezone(ET)
    if now.weekday() >= 5:
        return False
    return MARK_WINDOW_START <= now.time() < MARK_WINDOW_END


def _in_mark_window(now: datetime | None = None) -> bool:
    """Backward-compatible alias."""
    return in_mark_window(now)


def first_markable_at(due: datetime) -> datetime:
    """
    Earliest ET instant >= `due` that falls inside a weekday mark window.
    Used so overdue checks ignore flags whose due time fell outside hours.
    """
    if due.tzinfo is None:
        due = due.replace(tzinfo=ET)
    due = due.astimezone(ET)
    candidate = due
    for _ in range(14):  # enough to clear a long weekend
        if candidate.weekday() < 5:
            start = datetime.combine(candidate.date(), MARK_WINDOW_START, tzinfo=ET)
            end = datetime.combine(candidate.date(), MARK_WINDOW_END, tzinfo=ET)
            if candidate < start:
                return start
            if candidate < end:
                return candidate
        next_day = candidate.date() + timedelta(days=1)
        candidate = datetime.combine(next_day, MARK_WINDOW_START, tzinfo=ET)
    return candidate


def market_hours_between(start: datetime, end: datetime) -> float:
    """
    Hours of mark-window (market) time in [start, end).
    Weekends and overnight gaps contribute 0.
    """
    if start.tzinfo is None:
        start = start.replace(tzinfo=ET)
    if end.tzinfo is None:
        end = end.replace(tzinfo=ET)
    start = start.astimezone(ET)
    end = end.astimezone(ET)
    if end <= start:
        return 0.0
    total = 0.0
    day = start.date()
    last = end.date()
    while day <= last:
        if day.weekday() < 5:
            ws = datetime.combine(day, MARK_WINDOW_START, tzinfo=ET)
            we = datetime.combine(day, MARK_WINDOW_END, tzinfo=ET)
            lo = max(start, ws)
            hi = min(end, we)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600.0
        day = day + timedelta(days=1)
    return total


def due_datetime(ts_et: datetime | str, horizon: str) -> datetime:
    if isinstance(ts_et, str):
        ts = datetime.fromisoformat(ts_et)
    else:
        ts = ts_et
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    ts = ts.astimezone(ET)
    if horizon == "t1h":
        return ts + timedelta(hours=1)
    if horizon == "t1d":
        return ts + timedelta(days=1)
    raise ValueError(f"due_datetime only for t1h/t1d, got {horizon}")


def is_past_staleness_ceiling(
    horizon: str,
    ts_et: datetime | str,
    as_of: datetime,
) -> bool:
    """
    True when too much *market* time has elapsed since first_markable_at(due).

    t1h: > 4 market hours
    t1d: > one full mark session (09:30–16:15)
    expiry: never stale under this rule
    """
    if horizon == "expiry":
        return False
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=ET)
    as_of = as_of.astimezone(ET)
    first = first_markable_at(due_datetime(ts_et, horizon))
    elapsed = market_hours_between(first, as_of)
    if horizon == "t1h":
        return elapsed > T1H_STALE_MARKET_HOURS
    if horizon == "t1d":
        return elapsed > SESSION_LEN_HOURS
    return False


def is_t1h_overdue(ts_et: datetime | str, as_of: datetime, *, marked: bool = False) -> bool:
    """
    True only when mark_t1h is still null AND the due time (ts+1h) became
    markable inside a window and as_of is strictly after that first chance.
    """
    if marked:
        return False
    if isinstance(ts_et, str):
        ts = datetime.fromisoformat(ts_et)
    else:
        ts = ts_et
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    ts = ts.astimezone(ET)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=ET)
    as_of = as_of.astimezone(ET)
    due = ts + timedelta(hours=1)
    first = first_markable_at(due)
    return as_of > first


def count_overdue_t1h(conn, as_of: datetime | None = None) -> int:
    """Count unmarked t1h flags that are overdue under the window-aware rule.

    Excludes stale-noted rows and flags belonging to EOD runs (run_kind='eod').
    """
    as_of = as_of or datetime.now(ET)
    try:
        rows = conn.execute(
            """
            SELECT f.ts_et AS ts_et, f.notes AS notes
            FROM flags f
            LEFT JOIN runs r ON r.run_id = f.run_id
            WHERE f.mark_t1h IS NULL
              AND (f.notes IS NULL OR f.notes NOT LIKE '%stale:t1h%')
              AND (f.notes IS NULL OR f.notes NOT LIKE '%fail:t1h%')
              AND (f.notes IS NULL OR f.notes NOT LIKE '%n/a:eod%')
              AND (r.run_kind IS NULL OR r.run_kind != 'eod')
            """
        ).fetchall()
    except Exception:
        # Minimal test schemas may lack notes / runs.join
        try:
            rows = conn.execute(
                """
                SELECT ts_et FROM flags
                WHERE mark_t1h IS NULL
                  AND (notes IS NULL OR notes NOT LIKE '%stale:t1h%')
                  AND (notes IS NULL OR notes NOT LIKE '%fail:t1h%')
                  AND (notes IS NULL OR notes NOT LIKE '%n/a:eod%')
                """
            ).fetchall()
        except Exception:
            rows = conn.execute(
                "SELECT ts_et FROM flags WHERE mark_t1h IS NULL"
            ).fetchall()
    n = 0
    for r in rows:
        if hasattr(r, "keys"):
            ts = r["ts_et"]
            notes = r["notes"] if "notes" in r.keys() else None
        else:
            ts = r[0]
            notes = r[1] if len(r) > 1 else None
        if notes is not None and (
            "stale:t1h" in str(notes)
            or "fail:t1h" in str(notes)
            or "n/a:eod" in str(notes)
        ):
            continue
        if is_t1h_overdue(ts, as_of):
            n += 1
    return n


def option_intrinsic(side: str, strike: float, underlying: float) -> float:
    """Settlement-style intrinsic from underlying close (never negative)."""
    k = float(strike)
    u = float(underlying)
    if str(side).upper() == "CALL":
        return max(0.0, u - k)
    return max(0.0, k - u)


def fetch_underlying_close(
    ticker: str,
    expiry_day: date,
    *,
    cache: dict[tuple[str, str], float | None],
) -> float | None:
    """
    Underlying close on *expiry_day* (ET calendar). Cached per (ticker, date).

    Returns a positive close, or None when the bar is confirmed missing
    (cached). Raises on transient transport errors so the caller can retry
    later without writing a permanent-fail note.
    """
    key = (str(ticker).upper(), expiry_day.isoformat())
    if key in cache:
        return cache[key]
    from sources.yahoo import fetch_underlying_close_on

    close = fetch_underlying_close_on(ticker, expiry_day)
    cache[key] = close
    return close


def _deadline_exceeded(deadline: float | None) -> bool:
    return deadline is not None and time.monotonic() >= deadline


def _mark_horizon(
    horizon: str,
    *,
    dry_run: bool,
    as_of: datetime | None = None,
    db_path: str | None = None,
    deadline: float | None = None,
    underlying_cache: dict[tuple[str, str], float | None] | None = None,
) -> tuple[int, int, bool]:
    """
    Returns (attempted, written, stopped_early).
    stopped_early True when the runtime deadline fired mid-horizon.
    """
    as_of = as_of or now_et()
    cache = underlying_cache if underlying_cache is not None else {}
    rows = due_for_marking(horizon, db_path=db_path, as_of=as_of)  # type: ignore[arg-type]
    attempted = 0
    written = 0
    for r in rows:
        if _deadline_exceeded(deadline):
            log.warning(
                "runtime cap hit during %s — stopping horizon early "
                "(attempted=%d written=%d remaining≈%d)",
                horizon, attempted, written, max(0, len(rows) - attempted),
            )
            return attempted, written, True

        fid = int(r["flag_id"])
        if horizon in ("t1h", "t1d") and is_past_staleness_ceiling(
            horizon, r["ts_et"], as_of
        ):
            if dry_run:
                log.info(
                    "dry-run stale %s flag_id=%s — would note, no mark",
                    horizon, fid,
                )
            else:
                noted = note_stale_horizon(fid, horizon, db_path=db_path)  # type: ignore[arg-type]
                log.info(
                    "stale %s flag_id=%s — %s, no mark written",
                    horizon, fid, "noted" if noted else "already noted",
                )
            continue

        attempted += 1
        mid: float | None = None
        try:
            if horizon == "expiry":
                exp = date.fromisoformat(str(r["expiry"])[:10])
                try:
                    u_close = fetch_underlying_close(
                        str(r["ticker"]), exp, cache=cache,
                    )
                except Exception as exc:
                    # Network / rate-limit after source retries — try next run.
                    log.info(
                        "skip expiry flag_id=%s %s — underlying close "
                        "transient: %s",
                        fid, r["ticker"], exc,
                    )
                    continue
                if u_close is None:
                    raise ValueError(
                        f"underlying close unavailable for "
                        f"{r['ticker']} on {exp.isoformat()}"
                    )
                mid = option_intrinsic(str(r["side"]), float(r["strike"]), u_close)
            else:
                mid = fetch_option_mid(
                    r["ticker"], r["side"], float(r["strike"]), str(r["expiry"]),
                )
        except ValueError as exc:
            reason = str(exc) or "permanent"
            if dry_run:
                log.info(
                    "dry-run fail %s flag_id=%s — would note %r",
                    horizon, fid, reason,
                )
            else:
                noted = note_mark_failure(
                    fid, horizon, reason, db_path=db_path,  # type: ignore[arg-type]
                )
                log.info(
                    "permanent fail %s flag_id=%s %s %s %s — %s (%s)",
                    horizon, fid, r["ticker"], r["side"], r["strike"],
                    reason, "noted" if noted else "already noted",
                )
            continue

        if mid is None:
            # Transient empty quote — leave unmarked for a later pass.
            log.info(
                "skip %s flag_id=%s %s %s %s — no mid (transient)",
                horizon, fid, r["ticker"], r["side"], r["strike"],
            )
            continue
        if dry_run:
            log.info(
                "dry-run %s flag_id=%s → mid=%.4f",
                horizon, fid, mid,
            )
            continue
        ok = write_mark(fid, horizon, mid, db_path=db_path)  # type: ignore[arg-type]
        if ok:
            written += 1
            log.info("marked %s flag_id=%s mid=%.4f", horizon, fid, mid)
        else:
            log.info(
                "unchanged %s flag_id=%s (already set or refused)",
                horizon, fid,
            )
    return attempted, written, False


def max_marked_t1h_age_minutes(
    conn,
    as_of: datetime | None = None,
) -> float | None:
    """Age in minutes of MAX(marked_t1h_at), or None if no t1h marks exist."""
    as_of = as_of or datetime.now(ET)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=ET)
    as_of = as_of.astimezone(ET)
    row = conn.execute("SELECT MAX(marked_t1h_at) FROM flags").fetchone()
    raw = row[0] if row else None
    if not raw:
        return None
    try:
        marked = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if marked.tzinfo is None:
        marked = marked.replace(tzinfo=ET)
    marked = marked.astimezone(ET)
    return (as_of - marked).total_seconds() / 60.0


def t1h_mark_health_ok(
    conn,
    as_of: datetime | None = None,
    *,
    max_age_min: float | None = None,
) -> tuple[bool, str]:
    """
    During mark-window hours: fail if MAX(marked_t1h_at) is older than max_age_min
    (default from config, 90). Outside the window: always ok.
    """
    as_of = as_of or datetime.now(ET)
    if max_age_min is None:
        max_age_min = _cfg_float("mark_t1h_health_max_age_min", 90.0)
    if not in_mark_window(as_of):
        return True, "outside mark window"
    age = max_marked_t1h_age_minutes(conn, as_of=as_of)
    if age is None:
        return False, "no marked_t1h_at rows"
    if age > float(max_age_min):
        return False, f"MAX(marked_t1h_at) age={age:.1f}m > {max_age_min:.0f}m"
    return True, f"MAX(marked_t1h_at) age={age:.1f}m"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Attribution mark runner")
    p.add_argument("--dry-run", action="store_true", help="print only, write nothing")
    p.add_argument("--expiry-only", action="store_true", help="only expiry marks")
    p.add_argument(
        "--force",
        action="store_true",
        help="run t1h/t1d even outside the mark window",
    )
    args = p.parse_args(argv)

    _load_env()
    setup_logging("mark_runner")
    max_runtime = _cfg_float("mark_runner_max_runtime_sec", 600.0)
    sock_timeout = _cfg_float("mark_runner_socket_timeout_sec", 30.0)
    prev_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(sock_timeout)
    deadline = time.monotonic() + max_runtime
    log.info(
        "db=%s dry_run=%s max_runtime=%.0fs socket_timeout=%.0fs",
        default_db_path(), args.dry_run, max_runtime, sock_timeout,
    )

    try:
        if args.expiry_only:
            horizons: tuple[str, ...] = ("expiry",)
        elif args.force or _in_mark_window():
            horizons = HORIZON_PRIORITY
        else:
            log.info("outside mark window (09:30–16:15 ET) — expiry pass only")
            horizons = ("expiry",)

        underlying_cache: dict[tuple[str, str], float | None] = {}
        for h in horizons:
            if _deadline_exceeded(deadline):
                log.warning(
                    "runtime cap %.0fs exceeded before %s — exiting 0 for launchd",
                    max_runtime, h,
                )
                return 0
            a, w, stopped = _mark_horizon(
                h,
                dry_run=args.dry_run,
                deadline=deadline,
                underlying_cache=underlying_cache,
            )
            log.info("%s: due=%d written=%d", h, a, w)
            if stopped:
                log.warning(
                    "runtime cap %.0fs exceeded — exiting 0 for launchd",
                    max_runtime,
                )
                return 0
        return 0
    finally:
        socket.setdefaulttimeout(prev_timeout)


if __name__ == "__main__":
    sys.exit(main())
