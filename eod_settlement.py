"""
eod_settlement.py — End-of-day volume convergence helpers (pure / testable).

The EOD archive is only considered settled once two consecutive chain volume
totals match after a configured gap. Used by dailyScaner --eod.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Callable
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class VolumeSnapshot:
    total_call_vol: int
    total_put_vol: int


def volumes_equal(a: VolumeSnapshot, b: VolumeSnapshot) -> bool:
    return (
        int(a.total_call_vol) == int(b.total_call_vol)
        and int(a.total_put_vol) == int(b.total_put_vol)
    )


def check_convergence_pair(
    first: VolumeSnapshot,
    second: VolumeSnapshot,
    *,
    elapsed_sec: float,
    min_gap_sec: float,
) -> bool:
    """True when the pair is far enough apart and totals match."""
    if elapsed_sec < float(min_gap_sec):
        return False
    return volumes_equal(first, second)


def await_volume_convergence(
    read_volumes: Callable[[], VolumeSnapshot],
    *,
    gap_sec: float = 600.0,
    max_attempts: int = 3,
    sleep_fn: Callable[[float], None] | None = None,
    monotonic: Callable[[], float] | None = None,
) -> tuple[bool, VolumeSnapshot, list[VolumeSnapshot]]:
    """
    Poll chain volume totals until two consecutive reads (>= gap_sec apart)
    match, or until max_attempts reads are exhausted.

    Returns (converged, last_snapshot, all_snapshots).
    Attempt 1 is immediate; subsequent attempts sleep `gap_sec` first.
    """
    import time as _time

    sleep_fn = sleep_fn or _time.sleep
    mono = monotonic or _time.monotonic

    snaps: list[VolumeSnapshot] = []
    times: list[float] = []

    for attempt in range(1, int(max_attempts) + 1):
        if attempt > 1:
            sleep_fn(float(gap_sec))
        snap = read_volumes()
        snaps.append(snap)
        times.append(mono())
        if len(snaps) >= 2:
            elapsed = times[-1] - times[-2]
            if check_convergence_pair(
                snaps[-2], snaps[-1],
                elapsed_sec=elapsed,
                min_gap_sec=gap_sec,
            ):
                return True, snaps[-1], snaps

    return False, snaps[-1], snaps


def parse_hhmm(value: str, default: str = "16:20") -> dtime:
    raw = (value or default).strip()
    parts = raw.split(":")
    return dtime(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)


def scan_window_end_et(cfg: dict) -> dtime:
    """
    End of the daily scan window in ET.

    Prefer explicit ``eod_time`` (default 16:20). Fall back to
    market_close + post_close_buffer_min so older configs still work.
    """
    if cfg.get("eod_time"):
        return parse_hhmm(str(cfg["eod_time"]), "16:20")
    close_h, close_m = map(int, str(cfg.get("market_close", "16:00")).split(":"))
    buffer = int(cfg.get("post_close_buffer_min", 20))
    total = close_h * 60 + close_m + buffer
    return dtime(total // 60, total % 60)


def is_eod_slot(now: datetime, cfg: dict) -> bool:
    """True when current ET clock is at/after configured eod_time today."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    now = now.astimezone(ET)
    if now.weekday() >= 5:
        return False
    return now.time() >= scan_window_end_et(cfg)


def seconds_until_et(target: dtime, *, now: datetime | None = None) -> float:
    """Seconds until the next occurrence of ``target`` on an ET clock (F-23)."""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    now = now.astimezone(ET)
    candidate = datetime.combine(now.date(), target, tzinfo=ET)
    if candidate <= now:
        candidate = candidate + timedelta(days=1)
    # Skip weekends for market jobs
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return max(0.0, (candidate - now).total_seconds())
