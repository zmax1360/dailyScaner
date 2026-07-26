#!/usr/bin/env python3
"""
mark_runner.py — Write-once T+1h / T+1d / expiry marks for attribution flags.

  python mark_runner.py --dry-run
  python mark_runner.py
  python mark_runner.py --expiry-only
  python mark_runner.py --force          # ignore market-hours gate
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from attribution import (
    default_db_path,
    due_for_marking,
    fetch_option_mid,
    write_mark,
)

ET = ZoneInfo("America/New_York")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
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


def _in_mark_window(now: datetime | None = None) -> bool:
    """Weekdays 09:30–16:15 ET (buffer for delayed quotes after the close)."""
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    return dtime(9, 30) <= now.time() < dtime(16, 15)


def _mark_horizon(horizon: str, *, dry_run: bool) -> tuple[int, int]:
    rows = due_for_marking(horizon)  # type: ignore[arg-type]
    attempted = 0
    written = 0
    for r in rows:
        attempted += 1
        mid = fetch_option_mid(
            r["ticker"], r["side"], float(r["strike"]), str(r["expiry"]),
        )
        if mid is None:
            log.info(
                "skip %s flag_id=%s %s %s %s — no mid",
                horizon, r["flag_id"], r["ticker"], r["side"], r["strike"],
            )
            continue
        if dry_run:
            log.info(
                "dry-run %s flag_id=%s → mid=%.4f",
                horizon, r["flag_id"], mid,
            )
            continue
        ok = write_mark(int(r["flag_id"]), horizon, mid)  # type: ignore[arg-type]
        if ok:
            written += 1
            log.info("marked %s flag_id=%s mid=%.4f", horizon, r["flag_id"], mid)
        else:
            log.info(
                "unchanged %s flag_id=%s (already set or refused)",
                horizon, r["flag_id"],
            )
    return attempted, written


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
    log.info("db=%s dry_run=%s", default_db_path(), args.dry_run)

    if args.expiry_only:
        horizons: tuple[str, ...] = ("expiry",)
    elif args.force or _in_mark_window():
        horizons = ("t1h", "t1d", "expiry")
    else:
        log.info("outside mark window (09:30–16:15 ET) — expiry pass only")
        horizons = ("expiry",)

    for h in horizons:
        a, w = _mark_horizon(h, dry_run=args.dry_run)
        log.info("%s: due=%d written=%d", h, a, w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
