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
    provides_quotes: bool = True,
    last: Any = None,
) -> bool:
    """
    Per-contract quality predicate (Task B).

    When provides_quotes is True (Yahoo / fixture):
      usable iff bid > 0 AND ask > 0 AND iv >= min_iv_usable AND dte >= 0.

    When provides_quotes is False (Massive Starter — no NBBO entitlement):
      skip bid/ask; require last (day.close) > 0 plus the same iv/dte checks.
      Never synthesise bid/ask from last.

    Never substitutes defaults for degraded values — caller excludes the row.
    """
    cfg = cfg or SCORING
    min_iv = float(cfg.get("min_iv_usable", 0.01))
    try:
        sigma = float(iv)
        d = float(dte)
    except (TypeError, ValueError):
        return False
    if sigma != sigma or d != d:  # NaN
        return False
    if not (sigma >= min_iv and d >= 0):
        return False

    if provides_quotes:
        try:
            b = float(bid)
            a = float(ask)
        except (TypeError, ValueError):
            return False
        if b != b or a != a:  # NaN
            return False
        return b > 0 and a > 0

    # No quote entitlement — price from daily bar only.
    try:
        last_f = float(last)
    except (TypeError, ValueError):
        return False
    return last_f == last_f and last_f > 0


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
    provides_quotes: bool = True,
) -> dict[str, int]:
    """Count failures among a contract list (for logging / tests)."""
    cfg = cfg or SCORING
    total = 0
    fail = 0
    zero_ba = 0
    missing_last = 0
    low_iv = 0
    bad_dte = 0
    for c in contracts:
        total += 1
        bid = c.get("bid")
        ask = c.get("ask")
        iv = c.get("impliedVolatility", c.get("iv"))
        dte = c.get("dte", 0)
        last = c.get("lastPrice", c.get("last"))
        try:
            b = float(bid) if bid is not None else float("nan")
            a = float(ask) if ask is not None else float("nan")
            sigma = float(iv) if iv is not None else float("nan")
            d = float(dte)
            last_f = float(last) if last is not None else float("nan")
        except (TypeError, ValueError):
            fail += 1
            continue
        if provides_quotes:
            if not (b == b and a == a and b > 0 and a > 0):
                zero_ba += 1
        else:
            if not (last_f == last_f and last_f > 0):
                missing_last += 1
        if not (sigma == sigma) or sigma < float(cfg.get("min_iv_usable", 0.01)):
            low_iv += 1
        if not (d == d) or d < 0:
            bad_dte += 1
        if not contract_is_usable(
            bid=bid, ask=ask, iv=iv, dte=dte, cfg=cfg,
            provides_quotes=provides_quotes, last=last,
        ):
            fail += 1
    return {
        "total": total,
        "unusable": fail,
        "zero_bid_ask": zero_ba,
        "missing_last": missing_last,
        "low_iv": low_iv,
        "bad_dte": bad_dte,
    }


def chain_fails_quality_gate(
    volume_block: Mapping[str, Any] | None,
    *,
    cfg: Mapping[str, Any] | None = None,
    provides_quotes: bool = True,
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
    call_stats = quality_failure_counts(
        calls, cfg=cfg, provides_quotes=provides_quotes,
    )
    put_stats = quality_failure_counts(
        puts, cfg=cfg, provides_quotes=provides_quotes,
    ) if puts else {
        "total": 0, "unusable": 0, "zero_bid_ask": 0, "missing_last": 0,
        "low_iv": 0, "bad_dte": 0,
    }
    total = call_stats["total"]
    unusable = call_stats["unusable"]
    frac = (unusable / total) if total else 1.0
    fails = total > 0 and frac > max_frac
    detail = {
        "fails": fails,
        "frac_unusable": frac,
        "max_unusable_frac": max_frac,
        "provides_quotes": bool(provides_quotes),
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

    OR semantics kept for the per-contract / legacy total helpers. The
    chain-level scan guard uses ``chain_rollover_from_volume_blocks`` instead
    (both sides + per-contract majority).
    """
    try:
        return int(curr_call) < int(prev_call) or int(curr_put) < int(prev_put)
    except (TypeError, ValueError):
        return False


# Guardrail parameters — NOT in config.SCORING (must not move config_hash).
MIN_CHAIN_ROLLOVER_MATCHES = 10
# "more than 3 consecutive" → disable on the 4th abort attempt.
MAX_CONSECUTIVE_CHAIN_ROLLOVER_ABORTS = 3


def _contract_vol_key(side: str, strike: Any, expiry: Any) -> tuple | None:
    try:
        return (str(side).upper(), float(strike), str(expiry))
    except (TypeError, ValueError):
        return None


def volume_maps_from_block(
    volume_block: Mapping[str, Any] | None,
) -> dict[tuple, int]:
    """(side, strike, expiry) → volume from top_calls / top_puts."""
    out: dict[tuple, int] = {}
    if not volume_block:
        return out
    for side, key in (("CALL", "top_calls"), ("PUT", "top_puts")):
        for c in volume_block.get(key) or []:
            k = _contract_vol_key(side, c.get("strike"), c.get("expiry"))
            if k is None:
                continue
            try:
                out[k] = int(float(c.get("volume") or 0))
            except (TypeError, ValueError):
                continue
    return out


def matched_chain_volumes(
    prev_volume: Mapping[str, Any] | None,
    curr_volume: Mapping[str, Any] | None,
) -> tuple[int, int, int, int, int]:
    """
    Sum call/put volume over contracts present in BOTH snapshots.

    Returns (prev_call, prev_put, curr_call, curr_put, n_matched).
    Kept for diagnostics; the scan guard no longer aborts on these sums.
    """
    prev_m = volume_maps_from_block(prev_volume)
    curr_m = volume_maps_from_block(curr_volume)
    common = set(prev_m) & set(curr_m)
    pc = pp = cc = cp = 0
    for k in common:
        side = k[0]
        if side == "CALL":
            pc += prev_m[k]
            cc += curr_m[k]
        elif side == "PUT":
            pp += prev_m[k]
            cp += curr_m[k]
    return pc, pp, cc, cp, len(common)


def _side_majority_decreased(
    prev_m: Mapping[tuple, int],
    curr_m: Mapping[tuple, int],
    common: set[tuple],
    side: str,
) -> tuple[bool, int, int]:
    """
    True when a strict majority of matched contracts on *side* each fell.

    Returns (majority_decreased, n_side_matched, n_decreased).
    """
    keys = [k for k in common if k[0] == side]
    n = len(keys)
    if n == 0:
        return False, 0, 0
    dec = sum(1 for k in keys if curr_m[k] < prev_m[k])
    return dec > (n / 2.0), n, dec


def chain_rollover_from_volume_blocks(
    prev_volume: Mapping[str, Any] | None,
    curr_volume: Mapping[str, Any] | None,
    *,
    min_matched: int = MIN_CHAIN_ROLLOVER_MATCHES,
) -> tuple[bool | None, dict[str, Any]]:
    """
    Chain-level session-rollover decision from two volume blocks.

    Abort only when ALL of:
      * at least ``min_matched`` contracts share (side, strike, expiry)
      * at least one matched CALL and one matched PUT exist
      * a strict majority of matched CALLs each decreased, AND
      * a strict majority of matched PUTs each decreased

    One-sided drops and shifting top-N membership are NOT rollovers.
    Sums over matched contracts are NOT used for the abort decision.

    Returns (rolled_over, detail):
      True/False when evaluated; None when skipped (caller must not abort).
    """
    detail: dict[str, Any] = {
        "min_matched": int(min_matched),
        "n_matched": 0,
        "n_call": 0,
        "n_put": 0,
        "n_call_decreased": 0,
        "n_put_decreased": 0,
        "reason": "",
    }
    prev_m = volume_maps_from_block(prev_volume)
    curr_m = volume_maps_from_block(curr_volume)
    common = set(prev_m) & set(curr_m)
    n = len(common)
    detail["n_matched"] = n
    if n < int(min_matched):
        detail["reason"] = f"insufficient_overlap:{n}<{int(min_matched)}"
        return None, detail

    call_maj, n_call, n_call_dec = _side_majority_decreased(
        prev_m, curr_m, common, "CALL",
    )
    put_maj, n_put, n_put_dec = _side_majority_decreased(
        prev_m, curr_m, common, "PUT",
    )
    detail.update(
        n_call=n_call,
        n_put=n_put,
        n_call_decreased=n_call_dec,
        n_put_decreased=n_put_dec,
    )
    if n_call < 1 or n_put < 1:
        detail["reason"] = "missing_side_overlap"
        return None, detail

    # BOTH sides must show a per-contract majority decrease.
    rolled = bool(call_maj and put_maj)
    detail["reason"] = "per_contract_majority_both_sides" if rolled else "ok"
    return rolled, detail


# ── Session circuit breaker (process-local) ──────────────────────────────────

_CHAIN_ROLLOVER_GUARD: dict[tuple[str, str], dict[str, Any]] = {}


def _guard_key(ticker: str, session_date: date | str) -> tuple[str, str]:
    d = session_date.isoformat() if isinstance(session_date, date) else str(session_date)
    return (str(ticker).upper(), d)


def reset_chain_rollover_guard_state() -> None:
    """Test helper — clear process-local circuit-breaker state."""
    _CHAIN_ROLLOVER_GUARD.clear()


def chain_rollover_guard_disabled(ticker: str, session_date: date | str) -> bool:
    st = _CHAIN_ROLLOVER_GUARD.get(_guard_key(ticker, session_date))
    return bool(st and st.get("disabled"))


def note_chain_rollover_clean(ticker: str, session_date: date | str) -> None:
    """Successful non-rollover scan — reset consecutive abort streak."""
    key = _guard_key(ticker, session_date)
    st = _CHAIN_ROLLOVER_GUARD.setdefault(
        key, {"consecutive": 0, "disabled": False, "reason": ""},
    )
    if not st.get("disabled"):
        st["consecutive"] = 0
        st["reason"] = ""


def note_chain_rollover_abort(
    ticker: str,
    session_date: date | str,
    reason: str,
    *,
    max_consecutive: int = MAX_CONSECUTIVE_CHAIN_ROLLOVER_ABORTS,
) -> tuple[bool, dict[str, Any]]:
    """
    Record a chain-rollover abort.

    Returns (should_abort, state).
    When consecutive aborts exceed ``max_consecutive``, the guard is disabled
    for the rest of the session: should_abort is False and state['disabled']
    is True (caller must proceed and log ERROR).
    """
    key = _guard_key(ticker, session_date)
    st = _CHAIN_ROLLOVER_GUARD.setdefault(
        key, {"consecutive": 0, "disabled": False, "reason": ""},
    )
    if st.get("disabled"):
        return False, dict(st)

    if st.get("reason") and st["reason"] != reason:
        # Different reason — restart the streak for this reason.
        st["consecutive"] = 0
    st["reason"] = reason
    st["consecutive"] = int(st.get("consecutive") or 0) + 1

    if st["consecutive"] > int(max_consecutive):
        st["disabled"] = True
        return False, dict(st)
    return True, dict(st)


def rollover_detectors_active(volume_is_session_scoped: bool) -> bool:
    """
    When the feed's volume resets each session (Massive), Yahoo-style
    rollover / EOD-stale detectors are DORMANT — not deleted.
    """
    return not bool(volume_is_session_scoped)


def archive_source_name(archive: Mapping[str, Any] | None) -> str:
    """
    MarketDataSource.name that wrote this archive.

    Archives written before source tagging have no ``source`` key — treat as
    ``"yahoo"`` (that is what wrote them), not as unknown.
    """
    if not archive or "source" not in archive:
        return "yahoo"
    raw = archive.get("source")
    if raw is None or str(raw).strip() == "":
        return "yahoo"
    return str(raw).strip().lower()


def volume_sources_match(prev_source: str, current_source: str) -> bool:
    """True when both sides share the same volume semantics for comparison."""
    return archive_source_name({"source": prev_source}) == archive_source_name(
        {"source": current_source}
    )


def should_apply_chain_rollover_check(
    prev_archive: Mapping[str, Any] | None,
    current_source: str,
    today_et: date,
) -> tuple[bool, str]:
    """
    Whether chain-level call/put totals may be compared for session rollover.

    Returns (apply, reason). reason is ``"ok"`` when apply is True; otherwise a
    short skip reason (``different_session_date``, ``source_mismatch:...``, …).
    """
    if not prev_archive:
        return False, "no_prev"
    prev_day = archive_session_date(prev_archive)
    if prev_day != today_et:
        return False, "different_session_date"
    prev_src = archive_source_name(prev_archive)
    curr_src = archive_source_name({"source": current_source})
    if prev_src != curr_src:
        return False, f"source_mismatch:{prev_src}->{curr_src}"
    return True, "ok"


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
    required_source: str | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """
    Load the most recent prior-trading-day EOD archive for *ticker*.

    Returns (payload, reason). reason is 'ok' on success; otherwise explains
    why the EOD-match check must be skipped.

    When *required_source* is set, the EOD archive's ``source`` must match
    (missing key → yahoo). Cross-source EOD volume is not comparable.
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

    want_src = (
        None
        if required_source is None
        else archive_source_name({"source": required_source})
    )

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
            if want_src is not None:
                got = archive_source_name(data)
                if got != want_src:
                    reason = (
                        f"EOD archive source {got!r} != current source {want_src!r}"
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
