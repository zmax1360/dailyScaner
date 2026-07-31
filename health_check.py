#!/usr/bin/env python3
"""
health_check.py — Daily attribution integrity checks.

  python health_check.py
  python health_check.py --alert-on-fail
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, time as dtime

from attribution import _db, default_db_path, now_et
from mark_runner import count_overdue_t1h, t1h_mark_health_ok

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")


def within_et_window(
    target_hhmm: str,
    *,
    window_min: int = 20,
    now: datetime | None = None,
) -> bool:
    """
    True when America/New_York clock is within ±window_min of target HH:MM.

    launchd StartCalendarInterval uses the *machine* local timezone (F-23).
    Gate scheduled jobs on ET so a Pacific-hosted Mac or DST edge does not
    shift when the health check actually runs.
    """
    now = now or now_et()
    parts = target_hhmm.strip().split(":")
    target = dtime(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    target_min = target.hour * 60 + target.minute
    now_min = now.hour * 60 + now.minute
    # Circular distance on a 24h clock (minutes)
    delta = abs(now_min - target_min)
    delta = min(delta, 24 * 60 - delta)
    return delta <= int(window_min)


def _load_env() -> None:
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


def _checks(conn) -> list[tuple[str, bool, str]]:
    today = now_et().date().isoformat()
    rows_today = conn.execute(
        "SELECT COUNT(*) FROM flags WHERE date(ts_et) = ?", (today,)
    ).fetchone()[0]
    empty_mults = conn.execute(
        """
        SELECT COUNT(*) FROM flags
        WHERE multipliers = '{}' OR multipliers IS NULL
        """
    ).fetchone()[0]
    overdue = count_overdue_t1h(conn, as_of=now_et())
    hashes = conn.execute(
        """
        SELECT COUNT(DISTINCT config_hash) FROM runs
        WHERE ts_et > date('now', '-7 days')
        """
    ).fetchone()[0]
    # Expiry intrinsic may be exactly 0.0 (OTM settled) — not a garbage quote.
    zero_marks = conn.execute(
        """
        SELECT COUNT(*) FROM flags
        WHERE mark_t1h = 0 OR mark_t1d = 0
        """
    ).fetchone()[0]
    t1h_ok, t1h_detail = t1h_mark_health_ok(conn, as_of=now_et())
    null_scores = conn.execute(
        """
        SELECT COUNT(*) FROM flags
        WHERE score IS NULL AND is_control = 0
        """
    ).fetchone()[0]
    # Latest run must persist base parts (A2). Older pre-fix rows may lack them.
    latest = conn.execute(
        "SELECT run_id FROM runs ORDER BY ts_et DESC LIMIT 1"
    ).fetchone()
    if latest:
        missing_base = conn.execute(
            """
            SELECT COUNT(*) FROM flags
            WHERE run_id = ?
              AND is_control = 0
              AND (nlev IS NULL OR nflow IS NULL OR base_score IS NULL)
            """,
            (latest[0],),
        ).fetchone()[0]
    else:
        missing_base = 0

    # On non-trading days, rows_today==0 is expected
    expect_rows = now_et().weekday() < 5
    return [
        (
            "rows_today",
            (rows_today > 0) if expect_rows else True,
            f"{rows_today} (expect >0 on trading day)",
        ),
        ("empty_mults", empty_mults == 0, str(empty_mults)),
        ("overdue_t1h_window", overdue == 0, str(overdue)),
        (
            "config_hash_7d",
            hashes <= 1,
            f"{hashes} distinct (expect ≤1)",
        ),
        ("zero_marks", zero_marks == 0, str(zero_marks)),
        ("t1h_mark_freshness", t1h_ok, t1h_detail),
        ("null_score_non_control", null_scores == 0, str(null_scores)),
        (
            "latest_run_missing_base",
            missing_base == 0,
            str(missing_base),
        ),
    ]


def _telegram_alert(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_TOKEN")
    chat_raw = os.environ.get("TELEGRAM_CHAT_ID", "")
    chat = (chat_raw.split(",")[0] or "").strip()
    if not token or not chat:
        print("WARN: Telegram not configured; alert not sent", file=sys.stderr)
        return
    try:
        import urllib.parse
        import urllib.request

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({
            "chat_id": chat,
            "text": text,
            "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(url, data=data, timeout=15)
    except Exception as exc:
        print(f"WARN: Telegram send failed: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--alert-on-fail", action="store_true")
    p.add_argument(
        "--require-et",
        metavar="HH:MM",
        default=None,
        help=(
            "Exit 0 without checking unless now (America/New_York) is within "
            "--et-window-min of this time. Use for launchd (F-23)."
        ),
    )
    p.add_argument(
        "--et-window-min",
        type=int,
        default=20,
        help="Half-width minutes for --require-et (default 20).",
    )
    args = p.parse_args(argv)

    _load_env()
    as_of = now_et()
    if args.require_et and not within_et_window(
        args.require_et, window_min=args.et_window_min, now=as_of
    ):
        print(
            f"SKIP: ET {as_of.strftime('%H:%M')} outside "
            f"{args.require_et} ±{args.et_window_min}m window (F-23)"
        )
        return 0

    db = default_db_path()
    print(f"db={db}  as_of={as_of.isoformat(timespec='seconds')}")
    if not os.path.exists(db):
        print("FAIL: attribution db missing")
        if args.alert_on_fail:
            _telegram_alert(f"attribution health: DB missing at {db}")
        return 1

    with _db(db) as conn:
        results = _checks(conn)

    failed = []
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"  {status}  {name}: {detail}")
        if not ok:
            failed.append(f"{name}={detail}")

    if failed:
        msg = "attribution health FAIL:\n" + "\n".join(failed)
        if args.alert_on_fail:
            _telegram_alert(msg)
        return 1
    print("OK: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
