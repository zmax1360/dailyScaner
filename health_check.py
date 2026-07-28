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

from attribution import _db, default_db_path, now_et
from mark_runner import count_overdue_t1h

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")


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
    zero_marks = conn.execute(
        """
        SELECT COUNT(*) FROM flags
        WHERE mark_t1h = 0 OR mark_t1d = 0 OR mark_expiry = 0
        """
    ).fetchone()[0]
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
    args = p.parse_args(argv)

    _load_env()
    db = default_db_path()
    print(f"db={db}  as_of={now_et().isoformat(timespec='seconds')}")
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
