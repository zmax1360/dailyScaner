"""
chain_quality.py — Session rollover + IV/quote quality gates (Tasks A/B).

Single place for thresholds (via config.SCORING) so they enter config_hash.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from config import SCORING

ET = ZoneInfo("America/New_York")


def contract_is_usable(
    *,
    bid: Any,
    ask: Any,
    iv: Any,
    dte: Any,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    """
    Per-contract quality predicate (Task B).

    Usable iff bid > 0 AND ask > 0 AND iv >= min_iv_usable AND dte >= 0.
    Never substitutes defaults for degraded values — caller excludes the row.
    """
    cfg = cfg or SCORING
    min_iv = float(cfg.get("min_iv_usable", 0.01))
    try:
        b = float(bid or 0)
        a = float(ask or 0)
        sigma = float(iv)
        d = float(dte)
    except (TypeError, ValueError):
        return False
    if b != b or a != a or sigma != sigma or d != d:  # NaN
        return False
    return b > 0 and a > 0 and sigma >= min_iv and d >= 0


def top_n_contracts(
    volume_block: Mapping[str, Any] | None,
    *,
    side_key: str = "top_calls",
    n: int | None = None,
) -> list[dict]:
    n = int(SCORING.get("quality_top_n", 30) if n is None else n)
    rows = list((volume_block or {}).get(side_key) or [])
    return rows[:n]


def quality_failure_counts(
    contracts: Iterable[Mapping[str, Any]],
    *,
    cfg: Mapping[str, Any] | None = None,
) -> dict[str, int]:
    """Count failures among a contract list (for logging / tests)."""
    cfg = cfg or SCORING
    total = 0
    fail = 0
    zero_ba = 0
    low_iv = 0
    bad_dte = 0
    for c in contracts:
        total += 1
        bid = c.get("bid")
        ask = c.get("ask")
        iv = c.get("impliedVolatility", c.get("iv"))
        dte = c.get("dte", 0)
        try:
            b = float(bid or 0)
            a = float(ask or 0)
            sigma = float(iv) if iv is not None else float("nan")
            d = float(dte)
        except (TypeError, ValueError):
            fail += 1
            continue
        if b <= 0 and a <= 0:
            zero_ba += 1
        if not (sigma == sigma) or sigma < float(cfg.get("min_iv_usable", 0.01)):
            low_iv += 1
        if not (d == d) or d < 0:
            bad_dte += 1
        if not contract_is_usable(bid=bid, ask=ask, iv=iv, dte=dte, cfg=cfg):
            fail += 1
    return {
        "total": total,
        "unusable": fail,
        "zero_bid_ask": zero_ba,
        "low_iv": low_iv,
        "bad_dte": bad_dte,
    }


def chain_fails_quality_gate(
    volume_block: Mapping[str, Any] | None,
    *,
    cfg: Mapping[str, Any] | None = None,
) -> tuple[bool, dict[str, Any]]:
    """
    True when more than max_unusable_frac of the top-N calls fail
    contract_is_usable.

    0DTE rows are excluded from the sample when enough dte>=1 contracts
    remain — near the close, lottery 0DTE quotes are structurally noisy and
    are not the early-session stale-IV failure mode this gate targets.
    """
    cfg = cfg or SCORING
    n = int(cfg.get("quality_top_n", 30))
    max_frac = float(cfg.get("max_unusable_frac", 0.20))
    raw_calls = top_n_contracts(volume_block, side_key="top_calls", n=n)
    raw_puts = top_n_contracts(volume_block, side_key="top_puts", n=n)

    def _sample(rows: list[dict]) -> list[dict]:
        non_0dte = []
        for c in rows:
            try:
                if float(c.get("dte") or 0) >= 1:
                    non_0dte.append(c)
            except (TypeError, ValueError):
                continue
        if len(non_0dte) >= max(5, n // 3):
            return non_0dte
        return rows

    calls = _sample(raw_calls)
    puts = _sample(raw_puts)
    call_stats = quality_failure_counts(calls, cfg=cfg)
    put_stats = quality_failure_counts(puts, cfg=cfg) if puts else {
        "total": 0, "unusable": 0, "zero_bid_ask": 0, "low_iv": 0, "bad_dte": 0,
    }
    total = call_stats["total"]
    unusable = call_stats["unusable"]
    frac = (unusable / total) if total else 1.0
    fails = total > 0 and frac > max_frac
    detail = {
        "fails": fails,
        "frac_unusable": frac,
        "max_unusable_frac": max_frac,
        "calls": call_stats,
        "puts": put_stats,
    }
    return fails, detail


def chain_volume_rolled_over(
    prev_call: int | float,
    prev_put: int | float,
    curr_call: int | float,
    curr_put: int | float,
) -> bool:
    """
    Cumulative session volume cannot decrease. A drop means Yahoo rolled
    prior-session totals off the chain (Task A).
    """
    try:
        return int(curr_call) < int(prev_call) or int(curr_put) < int(prev_put)
    except (TypeError, ValueError):
        return False


def rollover_detectors_active(volume_is_session_scoped: bool) -> bool:
    """
    When the feed's volume resets each session (Massive), Yahoo-style
    rollover / EOD-stale detectors are DORMANT — not deleted.
    """
    return not bool(volume_is_session_scoped)


def archive_session_date(archive: Mapping[str, Any] | None) -> date | None:
    """ET calendar date of an archive payload (from timestamp)."""
    if not archive:
        return None
    ts = archive.get("timestamp")
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET).date()


def iv_degraded_for_1sd(
    iv_values: Iterable[Any],
    *,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    """
    True when the chain lacks a usable IV for expected-move / 1SD bands.
    Never substitute a default IV — skip the 1SD path entirely.
    """
    cfg = cfg or SCORING
    min_iv = float(cfg.get("min_iv_usable", 0.01))
    usable = []
    total = 0
    for v in iv_values:
        total += 1
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f and f >= min_iv:
            usable.append(f)
    if total == 0:
        return True
    # Degraded if fewer than half the rows have usable IV, or none do
    return len(usable) == 0 or (len(usable) / total) < 0.5


# ── Stale volume vs prior EOD (CURSOR_STALE_VOLUME_FIX) ───────────────────────

def prior_trading_day(d: date) -> date:
    """Most recent weekday strictly before *d* (Mon → Fri)."""
    from datetime import timedelta

    cur = d - timedelta(days=1)
    while cur.weekday() >= 5:
        cur -= timedelta(days=1)
    return cur


def parse_cutoff_hhmm(value: str, default: str = "11:00"):
    from datetime import time as dtime

    raw = (value or default).strip()
    parts = raw.split(":")
    return dtime(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)


def stale_check_active(now_et: datetime, *, cfg: Mapping[str, Any] | None = None) -> bool:
    """
    True while the EOD-match stale check should run.

    Chosen: clock cutoff (default 11:00 ET), not 'observed decrease this session'.
    Tracking per-contract roll state across scans needs durable process state that
    dies on restart; a config-hashed cutoff is deterministic and enough for the
    morning Yahoo-cache failure mode.
    """
    cfg = cfg or SCORING
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    now_et = now_et.astimezone(ET)
    cutoff = parse_cutoff_hhmm(str(cfg.get("stale_check_cutoff_et", "11:00")))
    return now_et.time() < cutoff


def is_volume_stale_vs_eod(
    today_volume: float | int,
    prior_eod_volume: float | int,
    *,
    ratio: float | None = None,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    """
    today_volume >= prior_eod_volume * ratio → stale (feed not refreshed).

    Do not use equality — late prints often push the cached value slightly above EOD.
    """
    cfg = cfg or SCORING
    r = float(cfg.get("stale_volume_ratio", 0.95) if ratio is None else ratio)
    try:
        today = float(today_volume)
        eod = float(prior_eod_volume)
    except (TypeError, ValueError):
        return False
    if eod <= 0 or today != today or eod != eod:
        return False
    return today >= eod * r


def eod_volume_lookup(archive: Mapping[str, Any] | None) -> dict[tuple, int]:
    """(side, strike, expiry) → volume from an EOD archive volume block."""
    out: dict[tuple, int] = {}
    if not archive:
        return out
    vol = archive.get("volume") or archive
    for side, key in (("CALL", "top_calls"), ("PUT", "top_puts")):
        for c in vol.get(key) or []:
            try:
                k = (side, float(c.get("strike") or 0), str(c.get("expiry") or ""))
                out[k] = int(float(c.get("volume") or 0))
            except (TypeError, ValueError):
                continue
    return out


def find_prior_eod_archive(
    ticker: str,
    archive_dir: str | Any = "archive",
    *,
    now_et: datetime | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """
    Load the most recent prior-trading-day EOD archive for *ticker*.

    Returns (payload, reason). reason is 'ok' on success; otherwise explains
    why the EOD-match check must be skipped.
    """
    import json
    import logging
    from pathlib import Path

    log = logging.getLogger("chain_quality")

    now_et = now_et or datetime.now(ET)
    if now_et.tzinfo is None:
        now_et = now_et.replace(tzinfo=ET)
    now_et = now_et.astimezone(ET)
    target = prior_trading_day(now_et.date())

    root = Path(archive_dir)
    if not root.is_dir():
        reason = f"archive_dir missing: {archive_dir}"
        log.warning("EOD volume reference unavailable: %s", reason)
        return None, reason

    prefix = f"{ticker.upper()}_"
    candidates: list[tuple[date, Path, dict]] = []
    for path in sorted(root.glob(f"{prefix}*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not data.get("is_eod"):
            continue
        day = archive_session_date(data)
        if day is None:
            continue
        candidates.append((day, path, data))

    if not candidates:
        reason = "no is_eod=true archive on disk for ticker"
        log.warning("EOD volume reference unavailable: %s", reason)
        return None, reason

    for day, path, data in reversed(candidates):
        if day == target:
            if data.get("settlement_converged") is not True:
                reason = (
                    f"EOD archive {path.name} for {target} has "
                    f"settlement_converged={data.get('settlement_converged')!r} "
                    f"(need true)"
                )
                log.warning("EOD volume reference unavailable: %s", reason)
                return None, reason
            return data, "ok"
        if day > target:
            continue
        reason = (
            f"latest EOD archive is {day.isoformat()} "
            f"(need prior trading day {target.isoformat()}); stale reference rejected"
        )
        log.warning("EOD volume reference unavailable: %s", reason)
        return None, reason

    reason = f"no EOD archive for prior trading day {target.isoformat()}"
    log.warning("EOD volume reference unavailable: %s", reason)
    return None, reason


def flag_stale_vs_eod(
    contracts: Iterable[Mapping[str, Any]],
    eod_lookup: Mapping[tuple, int],
    *,
    side: str = "CALL",
    cfg: Mapping[str, Any] | None = None,
) -> list[bool]:
    """Per-contract stale flags (True = stale vs EOD). Length matches input."""
    cfg = cfg or SCORING
    ratio = float(cfg.get("stale_volume_ratio", 0.95))
    flags: list[bool] = []
    for c in contracts:
        try:
            k = (side, float(c.get("strike") or 0), str(c.get("expiry") or ""))
            today_v = float(c.get("volume") or 0)
        except (TypeError, ValueError):
            flags.append(False)
            continue
        eod_v = eod_lookup.get(k)
        if eod_v is None:
            flags.append(False)
            continue
        flags.append(is_volume_stale_vs_eod(today_v, eod_v, ratio=ratio, cfg=cfg))
    return flags


def majority_stale_abort(
    n_stale: int,
    n_total: int,
    *,
    cfg: Mapping[str, Any] | None = None,
) -> bool:
    """True when flagged-stale fraction of top-N exceeds threshold (default 50%)."""
    cfg = cfg or SCORING
    frac_lim = float(cfg.get("stale_majority_abort_frac", 0.50))
    if n_total <= 0:
        return False
    return (n_stale / n_total) > frac_lim
