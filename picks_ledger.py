#!/usr/bin/env python3
"""
picks_ledger.py — per-pick ledger for one session (Phase 1: 1DTE+).

What did the scanner pick, and what happened to each pick afterward?

Every P/L figure is ask-entry / bid-exit. Contracts are keyed
(ticker, side, strike, expiry). Quote path is the flags table itself.

    python picks_ledger.py --ticker AAPL --session 2026-09-11
    python picks_ledger.py --backfill
    python picks_ledger.py --backfill 2026-08-10 2026-09-14
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(_BASE, "data", "attribution.db")
REPORT_DIR = os.path.join(_BASE, "report")

POOL_1DTE = "1DTE+"
TOP_N = 10
LIVE_START = dtime(9, 30)
LIVE_END = dtime(16, 15)  # exclusive — same as mark_runner.MARK_WINDOW_END
OPEN_SUSPECT_END = dtime(9, 35)
COVERAGE_FLOOR = 80.0
MFE_PLUS = 0.30
HOSTILE_SPREAD = 0.08
HOSTILE_MONEYNESS = 0.03
HOSTILE_DELTA = 0.15
MAX_SPREAD_PCT = 0.25
BACKFILL_START = "2026-08-10"
INDEX_JSON = "ledger_index.json"


class RankIntegrityError(RuntimeError):
    """Ranks in a scan are not unique+contiguous 1..N."""


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    s = str(raw).strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def session_date_et(now: datetime | None = None) -> str:
    """Trading-session date in America/New_York — never UTC wall-clock."""
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    return now.astimezone(ET).date().isoformat()


def _in_live_window(ts: datetime) -> bool:
    t = ts.astimezone(ET).time()
    return LIVE_START <= t < LIVE_END


def _open_suspect(ts: datetime) -> bool:
    return ts.astimezone(ET).time() < OPEN_SUSPECT_END


def assert_scan_ranks(snaps: list[Snap], *, ts: datetime) -> None:
    ranks = [s.rank for s in snaps if s.rank is not None]
    if not ranks:
        return
    if len(ranks) != len(set(ranks)):
        raise RankIntegrityError(
            f"duplicate ranks at {ts.isoformat(timespec='seconds')}: {sorted(ranks)}"
        )
    ordered = sorted(ranks)
    if 1 in ordered and ordered != list(range(1, ordered[-1] + 1)):
        raise RankIntegrityError(
            f"non-contiguous ranks at {ts.isoformat(timespec='seconds')}: {ordered}"
        )


def _expiry_key(raw: Any) -> str:
    s = str(raw or "").strip()
    return s[:10] if len(s) >= 10 else s


def _strike_key(raw: Any) -> float:
    return round(float(raw), 4)


def _contract_key(side: str, strike: float, expiry: str) -> tuple[str, float, str]:
    return (str(side or "").upper(), _strike_key(strike), _expiry_key(expiry))


def _median(xs: list[float]) -> float | None:
    vals = [float(x) for x in xs if x is not None and math.isfinite(float(x))]
    if not vals:
        return None
    return float(statistics.median(vals))


def _fmt_pct(x: float | None, digits: int = 1) -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return f"{x * 100:+.{digits}f}%"


def _fmt_num(x: float | None, digits: int = 2) -> str:
    if x is None or not math.isfinite(x):
        return "—"
    return f"{x:.{digits}f}"


def _fmt_int(x: int | float | None) -> str:
    if x is None:
        return "—"
    return str(int(x))


def _fmt_decay(x: float | None) -> str:
    """t_decay is N/A when bid never falls back below entry_ask."""
    if x is None or not math.isfinite(x):
        return "N/A"
    return f"{x:.0f}"


def _right(side: str) -> str:
    s = (side or "").upper()
    if s.startswith("C"):
        return "C"
    if s.startswith("P"):
        return "P"
    return s or "?"


def _strike_label(strike: float) -> str:
    if abs(strike - round(strike)) < 1e-9:
        return str(int(round(strike)))
    return f"{strike:g}"


def _pick_label(p: "Pick") -> str:
    return (
        f"#{p.first_seen_rank} {_strike_label(p.strike)}"
        f"{_right(p.side)} {p.expiry[5:]}"
    )


def _minutes(a: datetime, b: datetime) -> float:
    return (b - a).total_seconds() / 60.0


@dataclass
class Snap:
    ts: datetime
    bid: float | None
    ask: float | None
    spot: float | None
    rank: int | None
    score: float | None
    flag_id: int
    run_id: str
    dte: int | None = None
    delta: float | None = None
    iv: float | None = None
    volume: int | None = None
    oi: int | None = None
    mark_close: float | None = None
    mark_expiry: float | None = None
    paired_flag_id: int | None = None
    close_method: str | None = None
    method_t15m: str | None = None
    method_t30m: str | None = None
    side: str = ""
    strike: float = 0.0
    expiry: str = ""


@dataclass
class Pick:
    pick_id: str
    ticker: str
    side: str
    strike: float
    expiry: str
    first_seen: datetime
    first_seen_rank: int
    entry_ask: float
    entry_bid: float | None
    spot_at_flag: float | None
    dte: int | None
    delta: float | None
    iv: float | None
    volume: int | None
    oi: int | None
    score: float | None
    flag_id: int
    run_id: str
    mark_close: float | None
    mark_expiry: float | None
    paired_flag_id: int | None
    path: list[dict[str, Any]] = field(default_factory=list)
    mfe: float | None = None
    mfe_pct: float | None = None
    t_mfe: float | None = None
    mae: float | None = None
    mae_pct: float | None = None
    t_decay: float | None = None
    scans_profitable: int = 0
    max_win_streak_scans: int = 0
    t_first_profitable: float | None = None
    n_snapshots: int = 0
    coverage_pct: float | None = None
    median_scan_interval_sec: float | None = None
    best_rank: int | None = None
    minutes_in_top10: float | None = None
    moneyness: float | None = None
    spread_pct: float | None = None
    spread_cost: float | None = None
    pnl_close: float | None = None
    paired_pick_id: str | None = None
    low_coverage: bool = False
    open_suspect: bool = False

    @property
    def hostile(self) -> dict[str, bool]:
        abs_d = abs(self.delta) if self.delta is not None else None
        return {
            "spread": bool(
                self.spread_pct is not None and self.spread_pct > HOSTILE_SPREAD
            ),
            "moneyness": bool(
                self.moneyness is not None
                and abs(self.moneyness) > HOSTILE_MONEYNESS
            ),
            "delta": bool(abs_d is not None and abs_d < HOSTILE_DELTA),
            "coverage": self.low_coverage,
        }


@dataclass
class Quality:
    n_score_gt_1: int = 0
    n_t15m_stale: int = 0
    n_t15m_unavail: int = 0
    n_t30m_stale: int = 0
    n_t30m_unavail: int = 0
    n_close_stale: int = 0
    n_close_unavail: int = 0
    n_late_close: int = 0


@dataclass
class SessionReport:
    session: str
    ticker: str
    generated: datetime
    db_path: str
    picks: list[Pick]
    quality: Quality
    n_session_scans: int
    median_scan_interval_sec: float | None
    summary: str = ""
    n_views: int = 0
    n_reached_30: int = 0
    median_t_mfe: float | None = None
    median_mfe: float | None = None
    median_coverage: float | None = None
    n_omitted: int = 0
    n_usable: int = 0
    n_never_profitable: int = 0
    reconstructed: bool = True
    benchmark: dict[str, Any] | None = None
    decision_ts: str | None = None


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _as_int(v: Any) -> int | None:
    f = _as_float(v)
    if f is None:
        return None
    return int(f)


def load_flags(
    conn: sqlite3.Connection, *, ticker: str, session: str,
) -> list[Snap]:
    ticker = ticker.upper()
    q = """
        SELECT flag_id, run_id, ts_et, side, strike, expiry, rank, score,
               bid, ask, spot, dte, delta, iv, volume, open_interest,
               mark_close, mark_expiry, close_method, method_t15m, method_t30m
        FROM flags
        WHERE ticker = ?
          AND substr(ts_et, 1, 10) = ?
          AND COALESCE(is_control, 0) = 0
          AND pool = ?
        ORDER BY ts_et, rank
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(flags)")}
    paired_sql = ", paired_flag_id" if "paired_flag_id" in cols else ", NULL"
    q = q.replace(
        "mark_close, mark_expiry, close_method, method_t15m, method_t30m",
        "mark_close, mark_expiry, close_method, method_t15m, method_t30m"
        + paired_sql,
    )
    out: list[Snap] = []
    for row in conn.execute(q, (ticker, session, POOL_1DTE)):
        ts = _parse_ts(row[2])
        if ts is None:
            continue
        out.append(
            Snap(
                ts=ts,
                bid=_as_float(row[8]),
                ask=_as_float(row[9]),
                spot=_as_float(row[10]),
                rank=_as_int(row[6]),
                score=_as_float(row[7]),
                flag_id=int(row[0]),
                run_id=str(row[1] or ""),
                dte=_as_int(row[11]),
                delta=_as_float(row[12]),
                iv=_as_float(row[13]),
                volume=_as_int(row[14]),
                oi=_as_int(row[15]),
                mark_close=_as_float(row[16]),
                mark_expiry=_as_float(row[17]),
                close_method=row[18],
                method_t15m=row[19],
                method_t30m=row[20],
                paired_flag_id=_as_int(row[21]) if len(row) > 21 else None,
                side=str(row[3] or "").upper(),
                strike=_strike_key(row[4]),
                expiry=_expiry_key(row[5]),
            )
        )
    return out


def load_quality(
    conn: sqlite3.Connection, *, ticker: str, session: str,
) -> Quality:
    q = Quality()
    row = conn.execute(
        """
        SELECT
          SUM(CASE WHEN score > 1.0 THEN 1 ELSE 0 END),
          SUM(CASE WHEN method_t15m = 'stale' THEN 1 ELSE 0 END),
          SUM(CASE WHEN method_t15m = 'unavailable' THEN 1 ELSE 0 END),
          SUM(CASE WHEN method_t30m = 'stale' THEN 1 ELSE 0 END),
          SUM(CASE WHEN method_t30m = 'unavailable' THEN 1 ELSE 0 END),
          SUM(CASE WHEN close_method = 'stale' THEN 1 ELSE 0 END),
          SUM(CASE WHEN close_method = 'unavailable' THEN 1 ELSE 0 END)
        FROM flags
        WHERE ticker = ? AND substr(ts_et, 1, 10) = ?
          AND COALESCE(is_control, 0) = 0
        """,
        (ticker.upper(), session),
    ).fetchone()
    if row:
        q.n_score_gt_1 = int(row[0] or 0)
        q.n_t15m_stale = int(row[1] or 0)
        q.n_t15m_unavail = int(row[2] or 0)
        q.n_t30m_stale = int(row[3] or 0)
        q.n_t30m_unavail = int(row[4] or 0)
        q.n_close_stale = int(row[5] or 0)
        q.n_close_unavail = int(row[6] or 0)
    return q


def list_sessions(
    conn: sqlite3.Connection, *, ticker: str, start: str, end: str,
) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT substr(ts_et, 1, 10)
        FROM flags
        WHERE ticker = ?
          AND COALESCE(is_control, 0) = 0
          AND pool = ?
          AND rank BETWEEN 1 AND ?
          AND substr(ts_et, 1, 10) >= ?
          AND substr(ts_et, 1, 10) <= ?
        ORDER BY 1
        """,
        (ticker.upper(), POOL_1DTE, TOP_N, start, end),
    ).fetchall()
    return [r[0] for r in rows if r[0]]


def latest_session(conn: sqlite3.Connection, ticker: str) -> str | None:
    row = conn.execute(
        """
        SELECT MAX(substr(ts_et, 1, 10)) FROM flags
        WHERE ticker = ? AND pool = ? AND COALESCE(is_control, 0) = 0
        """,
        (ticker.upper(), POOL_1DTE),
    ).fetchone()
    return row[0] if row and row[0] else None


def _dedupe_snaps(snaps: list[Snap]) -> list[Snap]:
    """One snapshot per timestamp; prefer a ranked row if both exist."""
    by_ts: dict[datetime, Snap] = {}
    for s in snaps:
        prev = by_ts.get(s.ts)
        if prev is None:
            by_ts[s.ts] = s
            continue
        prev_r = prev.rank if prev.rank is not None else 10**9
        new_r = s.rank if s.rank is not None else 10**9
        if new_r < prev_r:
            by_ts[s.ts] = s
    return [by_ts[k] for k in sorted(by_ts)]


def _path_stats(
    entry_ask: float,
    path_snaps: list[Snap],
    first_seen: datetime,
    session_scans: list[datetime],
) -> dict[str, Any]:
    """Observed bids only. A missing session scan breaks a profitable streak."""
    points: list[dict[str, Any]] = []
    bids: list[tuple[datetime, float]] = []
    by_ts = {s.ts: s for s in path_snaps}
    for s in path_snaps:
        tmin = _minutes(first_seen, s.ts)
        points.append({
            "t": round(tmin, 3),
            "bid": s.bid,
            "ask": s.ask,
            "spot": s.spot,
            "rank": s.rank,
            "ts": s.ts.isoformat(timespec="seconds"),
        })
        if s.bid is not None:
            bids.append((s.ts, s.bid))

    scans_after = [t for t in session_scans if t >= first_seen]
    profitable_flags: list[bool | None] = []
    for t in scans_after:
        s = by_ts.get(t)
        if s is None or s.bid is None:
            profitable_flags.append(None)
        else:
            profitable_flags.append(s.bid > entry_ask)
    scans_profitable = int(sum(1 for x in profitable_flags if x is True))
    streak = best = 0
    for flag in profitable_flags:
        if flag is True:
            streak += 1
            best = max(best, streak)
        else:
            streak = 0

    mfe = t_mfe_ts = None
    if bids:
        mfe_ts, mfe = max(bids, key=lambda x: x[1])
        t_mfe_ts = mfe_ts

    mae = None
    if t_mfe_ts is not None:
        window = [b for ts, b in bids if ts <= t_mfe_ts]
        if window:
            mae = min(window)

    t_decay = None
    if t_mfe_ts is not None:
        for ts, bid in bids:
            if ts > t_mfe_ts and bid < entry_ask:
                t_decay = _minutes(first_seen, ts)
                break

    t_first = None
    for ts, bid in bids:
        if bid > entry_ask:
            t_first = _minutes(first_seen, ts)
            break

    close_bid = bids[-1][1] if bids else None

    gaps = [
        (path_snaps[i].ts - path_snaps[i - 1].ts).total_seconds()
        for i in range(1, len(path_snaps))
    ]
    return {
        "path": points,
        "mfe": mfe,
        "t_mfe": _minutes(first_seen, t_mfe_ts) if t_mfe_ts else None,
        "mae": mae,
        "t_decay": t_decay,
        "scans_profitable": scans_profitable,
        "max_win_streak_scans": best,
        "t_first_profitable": t_first,
        "median_scan_interval_sec": _median(gaps),
        "n_snapshots": len(path_snaps),
        "close_bid": close_bid,
    }


def _minutes_in_top10(
    path_snaps: list[Snap],
    session_scans: list[datetime],
    first_seen: datetime,
    median_gap_sec: float | None,
) -> float:
    rank_at = {s.ts: s.rank for s in path_snaps}
    scans = [t for t in session_scans if t >= first_seen]
    if not scans:
        return 0.0
    total = 0.0
    fallback = median_gap_sec if median_gap_sec and median_gap_sec > 0 else 150.0
    for i, t in enumerate(scans):
        r = rank_at.get(t)
        if r is None or r > TOP_N:
            continue
        if i + 1 < len(scans):
            total += (scans[i + 1] - t).total_seconds()
        else:
            total += fallback
    return total / 60.0


def _pair_picks(picks: list[Pick]) -> None:
    by_scan: dict[str, list[Pick]] = defaultdict(list)
    for p in picks:
        by_scan[p.first_seen.isoformat(timespec="seconds")].append(p)
    for group in by_scan.values():
        calls = [p for p in group if p.side == "CALL"]
        puts = [p for p in group if p.side == "PUT"]
        used: set[str] = set()
        for c in calls:
            best = None
            best_d = None
            for u in puts:
                if u.pick_id in used:
                    continue
                d = abs(u.strike - c.strike)
                if best_d is None or d < best_d:
                    best_d = d
                    best = u
            if best is None:
                continue
            used.add(best.pick_id)
            c.paired_pick_id = best.pick_id
            best.paired_pick_id = c.pick_id


def build_session(
    conn: sqlite3.Connection,
    *,
    ticker: str,
    session: str,
    db_path: str,
    generated: datetime | None = None,
) -> SessionReport:
    ticker = ticker.upper()
    generated = generated or datetime.now(ET)
    snaps = load_flags(conn, ticker=ticker, session=session)
    quality = load_quality(conn, ticker=ticker, session=session)

    live = [s for s in snaps if _in_live_window(s.ts)]
    by_ts: dict[datetime, list[Snap]] = defaultdict(list)
    by_key: dict[tuple[str, float, str], list[Snap]] = defaultdict(list)
    by_run: dict[tuple[datetime, str], list[Snap]] = defaultdict(list)
    for s in live:
        by_ts[s.ts].append(s)
        by_key[_contract_key(s.side, s.strike, s.expiry)].append(s)
        by_run[(s.ts, s.run_id)].append(s)

    for (ts, _rid), group in by_run.items():
        assert_scan_ranks(group, ts=ts)

    session_scans = sorted(by_ts)
    scan_gaps = [
        (session_scans[i] - session_scans[i - 1]).total_seconds()
        for i in range(1, len(session_scans))
    ]
    session_median_gap = _median(scan_gaps)

    decision_ts = None
    for ts in session_scans:
        if any(
            s.rank is not None and 1 <= s.rank <= TOP_N for s in by_ts[ts]
        ):
            decision_ts = ts
            break
    if decision_ts is None:
        return SessionReport(
            session=session, ticker=ticker, generated=generated, db_path=db_path,
            picks=[], quality=quality, n_session_scans=len(session_scans),
            median_scan_interval_sec=session_median_gap,
            summary="0 / 0 usable | no live 1DTE+ top-10 scan",
            reconstructed=True,
        )

    try:
        from config import SCORING as _SCR
        max_spread = float(_SCR.get("max_spread_pct", MAX_SPREAD_PCT))
    except Exception:
        max_spread = MAX_SPREAD_PCT

    decision_entries: list[Snap] = []
    seen_keys: set[tuple[str, float, str]] = set()
    for s in sorted(by_ts[decision_ts], key=lambda x: x.rank or 10**9):
        if s.rank is None or not (1 <= s.rank <= TOP_N):
            continue
        if s.ask is None or s.ask <= 0:
            continue
        key = _contract_key(s.side, s.strike, s.expiry)
        if key in seen_keys:
            continue
        spread = None
        if s.bid is not None:
            spread = (s.ask - s.bid) / s.ask
        if spread is not None and spread > max_spread:
            continue
        seen_keys.add(key)
        decision_entries.append(s)

    picks: list[Pick] = []
    for entry in decision_entries:
        key = _contract_key(entry.side, entry.strike, entry.expiry)
        rows = _dedupe_snaps(by_key[key])
        side, strike, expiry = key
        path_snaps = [s for s in rows if s.ts >= entry.ts]
        stats = _path_stats(entry.ask, path_snaps, entry.ts, session_scans)
        n_after = sum(1 for t in session_scans if t >= entry.ts)
        cov = (
            100.0 * stats["n_snapshots"] / n_after if n_after else None
        )
        spread = None
        if entry.bid is not None:
            spread = (entry.ask - entry.bid) / entry.ask
        money = None
        if entry.spot is not None and entry.spot > 0:
            money = (strike - entry.spot) / entry.spot
        close_bid = stats["close_bid"]
        pnl_close = None
        if close_bid is not None:
            pnl_close = (close_bid - entry.ask) / entry.ask
        expiry_mark = entry.mark_expiry
        if expiry_mark is None:
            for s in reversed(path_snaps):
                if s.mark_expiry is not None:
                    expiry_mark = s.mark_expiry
                    break
        ranks = [s.rank for s in path_snaps if s.rank is not None]
        mae_pct = None
        if stats["mae"] is not None and entry.bid is not None and entry.bid > 0:
            mae_pct = (stats["mae"] - entry.bid) / entry.bid
        pick_id = f"{side}_{_strike_label(strike)}_{expiry}"
        p = Pick(
            pick_id=pick_id,
            ticker=ticker,
            side=side,
            strike=strike,
            expiry=expiry,
            first_seen=entry.ts,
            first_seen_rank=int(entry.rank or 0),
            entry_ask=float(entry.ask),
            entry_bid=entry.bid,
            spot_at_flag=entry.spot,
            dte=entry.dte,
            delta=entry.delta,
            iv=entry.iv,
            volume=entry.volume,
            oi=entry.oi,
            score=entry.score,
            flag_id=entry.flag_id,
            run_id=entry.run_id,
            mark_close=close_bid,
            mark_expiry=expiry_mark,
            paired_flag_id=entry.paired_flag_id,
            path=stats["path"],
            mfe=stats["mfe"],
            mfe_pct=(
                (stats["mfe"] - entry.ask) / entry.ask
                if stats["mfe"] is not None else None
            ),
            t_mfe=stats["t_mfe"],
            mae=stats["mae"],
            mae_pct=mae_pct,
            t_decay=stats["t_decay"],
            scans_profitable=stats["scans_profitable"],
            max_win_streak_scans=stats["max_win_streak_scans"],
            t_first_profitable=stats["t_first_profitable"],
            n_snapshots=stats["n_snapshots"],
            coverage_pct=cov,
            median_scan_interval_sec=stats["median_scan_interval_sec"],
            best_rank=min(ranks) if ranks else None,
            minutes_in_top10=_minutes_in_top10(
                path_snaps, session_scans, entry.ts, session_median_gap,
            ),
            moneyness=money,
            spread_pct=spread,
            spread_cost=spread,
            pnl_close=pnl_close,
            low_coverage=bool(cov is not None and cov < COVERAGE_FLOOR),
            open_suspect=_open_suspect(entry.ts),
        )
        picks.append(p)

    picks.sort(key=lambda x: (x.first_seen_rank, x.strike, x.expiry))
    _pair_picks(picks)
    bench = _benchmark_series(
        live,
        pick_keys={_contract_key(p.side, p.strike, p.expiry) for p in picks},
        pick_dtes={p.dte for p in picks if p.dte is not None},
        pick_sides={p.side for p in picks},
        decision_ts=decision_ts,
        session_scans=session_scans,
    )

    usable = [p for p in picks if not p.low_coverage]
    n_30 = sum(
        1 for p in usable
        if p.mfe_pct is not None and p.mfe_pct >= MFE_PLUS
    )
    n_never = sum(1 for p in usable if p.scans_profitable == 0)
    med_tmfe = _median([p.t_mfe for p in usable if p.t_mfe is not None])
    med_mfe = _median([p.mfe_pct for p in usable if p.mfe_pct is not None])
    med_cov = _median([p.coverage_pct for p in usable if p.coverage_pct is not None])
    views = len({(p.side, p.strike) for p in picks})
    med_int = _median(
        [p.median_scan_interval_sec for p in usable if p.median_scan_interval_sec]
    ) or session_median_gap

    tmfe_s = f"{med_tmfe:.0f}m" if med_tmfe is not None else "—"
    cov_s = f"{med_cov:.0f}%" if med_cov is not None else "—"
    summary = (
        f"{len(usable)} / {len(picks)} usable | {n_30} reached +30% MFE | "
        f"{n_never} never profitable | median t_MFE {tmfe_s} | "
        f"median coverage {cov_s}"
    )
    if med_int is not None:
        summary += f" | median scan interval {med_int:.0f}s"

    return SessionReport(
        session=session,
        ticker=ticker,
        generated=generated,
        db_path=db_path,
        picks=picks,
        quality=quality,
        n_session_scans=len(session_scans),
        median_scan_interval_sec=session_median_gap,
        summary=summary,
        n_views=views,
        n_reached_30=n_30,
        median_t_mfe=med_tmfe,
        median_mfe=med_mfe,
        median_coverage=med_cov,
        n_omitted=0,
        n_usable=len(usable),
        n_never_profitable=n_never,
        reconstructed=True,
        benchmark=bench,
        decision_ts=decision_ts.isoformat(timespec="seconds"),
    )


def _benchmark_series(
    live: list[Snap],
    *,
    pick_keys: set[tuple[str, float, str]],
    pick_dtes: set[int],
    pick_sides: set[str],
    decision_ts: datetime,
    session_scans: list[datetime],
) -> dict[str, Any] | None:
    """One random same-DTE/same-side non-pick from the decision-scan universe."""
    import hashlib
    cands = [
        s for s in live
        if s.ts == decision_ts
        and s.ask is not None and s.ask > 0
        and _contract_key(s.side, s.strike, s.expiry) not in pick_keys
    ]
    if not cands:
        return None
    if pick_sides:
        same_side = [s for s in cands if s.side in pick_sides]
        if same_side:
            cands = same_side
    if pick_dtes:
        same_dte = [s for s in cands if s.dte in pick_dtes]
        if same_dte:
            cands = same_dte
    seed = hashlib.sha256(
        f"{decision_ts.isoformat()}|{len(cands)}".encode()
    ).digest()
    idx = int.from_bytes(seed[:2], "big") % len(cands)
    b = cands[idx]
    key = _contract_key(b.side, b.strike, b.expiry)
    path = [
        s for s in live
        if _contract_key(s.side, s.strike, s.expiry) == key and s.ts >= decision_ts
    ]
    path = _dedupe_snaps(path)
    pts = []
    for s in path:
        if s.bid is None:
            continue
        pts.append({
            "t": round(_minutes(decision_ts, s.ts), 3),
            "v": (s.bid - b.ask) / b.ask,
        })
    return {
        "label": f"bench {_strike_label(b.strike)}{_right(b.side)} {b.expiry[5:]}",
        "path": pts,
        "entry_ask": b.ask,
    }


def _sparkline_svg(p: Pick, width: int = 200, height: int = 40) -> str:
    pts = [
        (float(pt["t"]), (float(pt["bid"]) - p.entry_ask) / p.entry_ask)
        for pt in p.path
        if pt.get("bid") is not None and p.entry_ask
    ]
    if not pts:
        return (
            f'<svg class="spark" width="{width}" height="{height}" '
            f'viewBox="0 0 {width} {height}"></svg>'
        )
    ts = [a for a, _ in pts]
    vs = [b for _, b in pts]
    t0, t1 = min(ts), max(ts)
    span_t = (t1 - t0) or 1.0
    lo = min(min(vs), 0.0)
    hi = max(max(vs), 0.0)
    span_v = (hi - lo) or 0.01
    pad = 3

    def xy(t: float, v: float) -> tuple[float, float]:
        x = pad + (t - t0) / span_t * (width - 2 * pad)
        y = height - pad - (v - lo) / span_v * (height - 2 * pad)
        return x, y

    zero_y = xy(t0, 0.0)[1]
    d_line = " ".join(
        f"{'M' if i == 0 else 'L'}{xy(t, v)[0]:.1f},{xy(t, v)[1]:.1f}"
        for i, (t, v) in enumerate(pts)
    )
    bands = []
    run: list[tuple[float, float]] = []
    for t, v in pts + [(pts[-1][0], -1.0)]:
        if v > 0:
            run.append((t, v))
        elif run:
            xs = [xy(t, 0)[0] for t, _ in run]
            bands.append(
                f'<rect x="{min(xs):.1f}" y="{min(zero_y, pad):.1f}" '
                f'width="{max(max(xs) - min(xs), 1):.1f}" '
                f'height="{max(abs(xy(run[0][0], 0)[1] - pad), 1):.1f}" '
                f'class="win"/>'
            )
            run = []
    # simpler band: polyline fill where v>0 is noisy; keep rects light
    mfe_dot = mae_dot = ""
    if p.t_mfe is not None and p.mfe_pct is not None:
        x, y = xy(p.t_mfe, p.mfe_pct)
        mfe_dot = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" class="mfe"/>'
    if p.t_mfe is not None and p.mae_pct is not None:
        # MAE is the low before MFE — place at t of min in that window
        mae_t = 0.0
        mae_v = p.mae_pct
        best = None
        for t, v in pts:
            if t <= (p.t_mfe or 0) and (best is None or v < best):
                best = v
                mae_t = t
                mae_v = v
        x, y = xy(mae_t, mae_v)
        mae_dot = f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.2" class="mae"/>'
    return (
        f'<svg class="spark" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
        f'<line x1="0" y1="{zero_y:.1f}" x2="{width}" y2="{zero_y:.1f}" '
        f'class="zero"/>'
        f"{''.join(bands)}"
        f'<path d="{d_line}" class="pl"/>'
        f"{mfe_dot}{mae_dot}"
        f"</svg>"
    )


def _pick_json(p: Pick) -> dict[str, Any]:
    return {
        "id": p.pick_id,
        "side": p.side,
        "strike": p.strike,
        "expiry": p.expiry,
        "label": _pick_label(p),
        "first_seen": p.first_seen.isoformat(timespec="seconds"),
        "first_seen_rank": p.first_seen_rank,
        "entry_ask": p.entry_ask,
        "entry_bid": p.entry_bid,
        "spot": p.spot_at_flag,
        "dte": p.dte,
        "delta": p.delta,
        "iv": p.iv,
        "volume": p.volume,
        "oi": p.oi,
        "score": p.score,
        "moneyness": p.moneyness,
        "spread_pct": p.spread_pct,
        "spread_cost": p.spread_cost,
        "mfe": p.mfe,
        "mfe_pct": p.mfe_pct,
        "t_mfe": p.t_mfe,
        "mae": p.mae,
        "mae_pct": p.mae_pct,
        "t_decay": p.t_decay,
        "scans_profitable": p.scans_profitable,
        "max_win_streak_scans": p.max_win_streak_scans,
        "t_first_profitable": p.t_first_profitable,
        "n_snapshots": p.n_snapshots,
        "coverage_pct": p.coverage_pct,
        "median_scan_interval_sec": p.median_scan_interval_sec,
        "best_rank": p.best_rank,
        "minutes_in_top10": p.minutes_in_top10,
        "mark_close": p.mark_close,
        "mark_expiry": p.mark_expiry,
        "pnl_close": p.pnl_close,
        "paired_pick_id": p.paired_pick_id,
        "low_coverage": p.low_coverage,
        "open_suspect": p.open_suspect,
        "hostile": p.hostile,
        "path": p.path,
    }


def render_txt(rep: SessionReport) -> str:
    recon = (
        "RECONSTRUCTED from flags — historical backfill, not a live-captured pick log."
    )
    lines = [
        f"PICKS LEDGER — {rep.session} — {rep.ticker}  (1DTE+)",
        recon,
        f"generated {rep.generated.strftime('%Y-%m-%d %H:%M %Z')}".rstrip(),
        f"db {rep.db_path}",
        rep.summary,
        (
            f"n_distinct_underlying_views {rep.n_views} / {len(rep.picks)} picks"
            f"  scans={rep.n_session_scans}"
        ),
        "",
        "P/L is ask-entry / bid-exit. Streaks are in scans, not minutes.",
        "MFE is sampled (~2 min); peaks between snapshots are missed (biased down).",
        "mark_close = last valid bid in 09:30–16:15 ET. mark_expiry is settlement, not an exit.",
        "t_decay is N/A when bid never falls back below entry_ask.",
        "",
    ]
    hdr = (
        f"{'#':>3} {'time':>5} {'k':>6} {'cp':>2} {'expiry':>10} {'dte':>3} "
        f"{'spot':>7} {'mny':>6} {'δ':>6} {'sprd':>6} {'ask':>6} "
        f"{'MFE':>7} {'tMFE':>5} {'MAE':>7} {'stk':>4} {'win':>4} "
        f"{'cls%':>7} {'sprd$':>6} {'cov':>5} {'int':>4} {'n':>4}"
    )
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for p in rep.picks:
        flag = " !" if p.low_coverage else (" ~" if p.open_suspect else "  ")
        if p.low_coverage:
            happened = f"{'INSUFFICIENT DATA':>32}"
            extra = (
                f"{_fmt_pct(p.spread_cost):>6} "
                f"{_fmt_num(p.coverage_pct, 0):>5} "
                f"{_fmt_num(p.median_scan_interval_sec, 0):>4} "
                f"{p.n_snapshots:4d}{flag}"
            )
        else:
            happened = (
                f"{_fmt_pct(p.mfe_pct):>7} {_fmt_num(p.t_mfe, 0):>5} "
                f"{_fmt_pct(p.mae_pct):>7} {p.max_win_streak_scans:4d} "
                f"{p.scans_profitable:4d} {_fmt_pct(p.pnl_close):>7} "
            )
            extra = (
                f"{_fmt_pct(p.spread_cost):>6} "
                f"{_fmt_num(p.coverage_pct, 0):>5} "
                f"{_fmt_num(p.median_scan_interval_sec, 0):>4} "
                f"{p.n_snapshots:4d}{flag}"
            )
        lines.append(
            f"{p.first_seen_rank:3d} {p.first_seen.strftime('%H:%M'):>5} "
            f"{_strike_label(p.strike):>6} {_right(p.side):>2} {p.expiry:>10} "
            f"{_fmt_int(p.dte):>3} {_fmt_num(p.spot_at_flag, 2):>7} "
            f"{_fmt_pct(p.moneyness):>6} {_fmt_num(p.delta, 2):>6} "
            f"{_fmt_pct(p.spread_pct):>6} {_fmt_num(p.entry_ask, 2):>6} "
            f"{happened}{extra}"
        )
    lines.append("")
    lines.append("DATA QUALITY")
    q = rep.quality
    lines.append(
        f"F-03 score>1.0: {q.n_score_gt_1}  "
        f"t15m stale/unavail {q.n_t15m_stale}/{q.n_t15m_unavail}  "
        f"t30m {q.n_t30m_stale}/{q.n_t30m_unavail}  "
        f"close {q.n_close_stale}/{q.n_close_unavail}"
    )
    lines.extend(_caveats())
    lines.append("")
    return "\n".join(lines) + "\n"


def _caveats() -> list[str]:
    return [
        "Sampling: MFE is the highest bid on a flags snapshot, not the true high.",
        "That biases MFE downward — do not 'correct' it up.",
        "Streak resolution is ~2 minutes (±1 scan). An exit shorter than ~6 min "
        "is not measurable. max_win_streak_scans stays in scans.",
        "A missing snapshot breaks a profitable streak. Bids are never interpolated.",
        "coverage_pct < 80 is INSUFFICIENT DATA — excluded from all aggregates.",
        "MAE is measured from entry_bid; spread_cost is (ask-bid)/ask at entry.",
        "Quotes with spread_pct > 0.25 at flag time are not in the pick set.",
        "Series stops at the live-quote window 09:30–16:15 ET.",
        "mark_close is the last valid live bid in that window, not the DB seal column.",
        "mark_expiry is settlement/reference — not an executable exit.",
        "Historical ranks are the stored live decision. Score cap (engine-v1.3) "
        "applies to future scans only.",
    ]


_HTML_CSS = """
:root { color-scheme: light; }
body {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.4; color: #111; background: #f7f7f5;
  margin: 16px; max-width: 1480px;
}
header { border: 2px solid #111; padding: 10px 12px; background: #fff; }
.recon {
  margin: 8px 0 0; padding: 6px 8px; border-left: 4px solid #111;
  background: #fff3c4; font-weight: 700;
}
header .title { font-weight: 700; font-size: 14px; }
header .sum { margin-top: 4px; font-weight: 600; }
header .meta { color: #444; margin-top: 2px; }
h2 { font-size: 13px; margin: 18px 0 8px; border-bottom: 1px solid #bbb; padding-bottom: 3px; }
.wrap { background: #fff; border: 1px solid #ccc; padding: 8px; margin: 8px 0 14px; }
#overlay { width: 100%; height: 220px; display: block; cursor: pointer; }
#detail { width: 100%; height: 240px; display: block; }
#spot { width: 100%; height: 160px; display: block; }
#tip {
  position: fixed; pointer-events: none; display: none;
  background: #111; color: #fff; padding: 2px 6px; font-size: 11px;
  z-index: 9;
}
table { border-collapse: collapse; width: 100%; background: #fff; }
th, td { border: 1px solid #ddd; padding: 2px 6px; white-space: nowrap; }
th { background: #f0f0f0; cursor: pointer; user-select: none; }
th:hover { background: #e4e4e4; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
tr.sel td { background: #fff3c4; }
tr.low td { opacity: 0.55; background: #ececec; }
td.bad { background: #fde8e8; }
.badge {
  font-size: 10px; font-weight: 700; letter-spacing: 0.02em;
  border: 1px solid #555; padding: 0 4px; margin-left: 4px;
}
.spark { display: block; }
.spark .zero { stroke: #111; stroke-width: 1; opacity: 0.35; }
.spark .pl { fill: none; stroke: #1a3a8a; stroke-width: 1.2; }
.spark .mfe { fill: #0a7a2f; }
.spark .mae { fill: #b00020; }
.spark .win { fill: #0a7a2f; opacity: 0.12; }
#ann { background: #fff; border: 1px solid #ccc; padding: 8px 10px; min-height: 4.5em; }
.footer { color: #333; margin-top: 16px; }
.footer div { margin: 2px 0; }
.legend { font-size: 11px; color: #333; margin: 4px 0 0; }
.toggle { margin: 6px 0; }
button {
  font: inherit; padding: 2px 8px; cursor: pointer;
  border: 1px solid #111; background: #fff;
}
button.on { background: #111; color: #fff; }
"""


_HTML_JS = r"""
(function () {
  const picks = LEDGER.picks;
  const byId = Object.fromEntries(picks.map(p => [p.id, p]));
  let selected = picks.slice().sort((a,b) =>
    (a.first_seen_rank - b.first_seen_rank) || a.first_seen.localeCompare(b.first_seen)
  )[0];
  let pnlMode = false;
  let sortKey = "first_seen";
  let sortDir = 1;

  const COLORS = [
    "#1a3a8a","#0a7a2f","#b00020","#7a3e00","#6a1b9a",
    "#00695c","#e65100","#37474f","#c2185b","#1565c0",
    "#33691e","#4e342e","#283593","#00838f","#f9a825",
    "#ad1457","#4527a0","#2e7d32","#d84315","#546e7a",
    "#6d4c41","#0277bd","#9e9d24","#c62828","#00897b"
  ];

  function color(p) {
    const i = picks.findIndex(x => x.id === p.id);
    return COLORS[i % COLORS.length];
  }
  function fmtPct(x, d) {
    if (x === null || x === undefined || Number.isNaN(x)) return "—";
    return (x * 100).toFixed(d ?? 1) + "%";
  }
  function fmt(x, d) {
    if (x === null || x === undefined || Number.isNaN(x)) return "—";
    return Number(x).toFixed(d ?? 2);
  }

  function select(id) {
    selected = byId[id] || selected;
    document.querySelectorAll("tr[data-id]").forEach(tr => {
      tr.classList.toggle("sel", tr.dataset.id === selected.id);
    });
    drawOverlay();
    drawDetail();
    annotate();
  }

  function overlayData(p) {
    return (p.path || []).filter(pt => pt.bid != null).map(pt => ({
      t: pt.t, v: (pt.bid - p.entry_ask) / p.entry_ask
    }));
  }

  function drawOverlay() {
    const svg = document.getElementById("overlay");
    const W = svg.clientWidth || 1100, H = 220;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    const pad = {l: 48, r: 12, t: 10, b: 24};
    let tMax = 1, lo = 0, hi = 0;
    picks.forEach(p => overlayData(p).forEach(pt => {
      tMax = Math.max(tMax, pt.t);
      lo = Math.min(lo, pt.v);
      hi = Math.max(hi, pt.v);
    }));
    const bench0 = LEDGER.benchmark;
    if (bench0 && bench0.path) {
      bench0.path.forEach(pt => {
        tMax = Math.max(tMax, pt.t);
        lo = Math.min(lo, pt.v);
        hi = Math.max(hi, pt.v);
      });
    }
    const spanV = (hi - lo) || 0.01;
    const x = t => pad.l + t / tMax * (W - pad.l - pad.r);
    const y = v => pad.t + (1 - (v - lo) / spanV) * (H - pad.t - pad.b);
    let html = `<line x1="${pad.l}" y1="${y(0)}" x2="${W-pad.r}" y2="${y(0)}"
      stroke="#111" stroke-width="1.4"/>`;
    html += `<text x="8" y="${y(0)+4}" fill="#111">0</text>`;
    picks.forEach(p => {
      const pts = overlayData(p);
      if (!pts.length) return;
      const d = pts.map((pt,i) =>
        `${i?"L":"M"}${x(pt.t).toFixed(1)},${y(pt.v).toFixed(1)}`
      ).join(" ");
      const sw = (selected && p.id === selected.id) ? 2.4 : 1.1;
      const op = (selected && p.id === selected.id) ? 1 : 0.55;
      html += `<path data-id="${p.id}" d="${d}" fill="none" stroke="${color(p)}"
        stroke-width="${sw}" opacity="${op}"/>`;
    });
    const bench = LEDGER.benchmark;
    if (bench && bench.path && bench.path.length) {
      const d = bench.path.map((pt,i) =>
        `${i?"L":"M"}${x(pt.t).toFixed(1)},${y(pt.v).toFixed(1)}`
      ).join(" ");
      html += `<path d="${d}" fill="none" stroke="#888" stroke-width="1.6"
        stroke-dasharray="5 3" opacity="0.85"/>`;
    }
    svg.innerHTML = html;
    const tip = document.getElementById("tip");
    svg.querySelectorAll("path[data-id]").forEach(el => {
      el.addEventListener("click", ev => {
        ev.stopPropagation();
        select(el.getAttribute("data-id"));
      });
      el.addEventListener("mousemove", ev => {
        const p = byId[el.getAttribute("data-id")];
        if (!p || !tip) return;
        tip.style.display = "block";
        tip.style.left = (ev.clientX + 8) + "px";
        tip.style.top = (ev.clientY - 18) + "px";
        tip.textContent = p.label;
      });
      el.addEventListener("mouseleave", () => {
        if (tip) tip.style.display = "none";
      });
    });
    const leg = document.getElementById("ov-legend");
    if (leg) {
      let items = picks.map(p =>
        `<span style="color:${color(p)};margin-right:10px">${p.label}</span>`
      );
      if (bench && bench.label) {
        items.push(
          `<span style="color:#888;margin-right:10px">— ${bench.label}</span>`
        );
      }
      leg.innerHTML = items.join("");
    }
  }

  function drawDetail() {
    const svg = document.getElementById("detail");
    const p = selected;
    const W = svg.clientWidth || 1100, H = 280;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    if (!p) { svg.innerHTML = ""; return; }
    const pad = {l: 56, r: 12, t: 14, b: 28};
    const path = p.path || [];
    const xs = path.map(pt => pt.t);
    const tMax = Math.max(1, ...xs, 1);
    let lo, hi;
    if (pnlMode) {
      const vs = path.filter(pt => pt.bid != null)
        .map(pt => (pt.bid - p.entry_ask) / p.entry_ask);
      lo = Math.min(0, ...vs); hi = Math.max(0, ...vs);
    } else {
      const vs = [];
      path.forEach(pt => {
        if (pt.bid != null) vs.push(pt.bid);
        if (pt.ask != null) vs.push(pt.ask);
      });
      vs.push(p.entry_ask);
      lo = Math.min(...vs); hi = Math.max(...vs);
    }
    const span = (hi - lo) || 0.01;
    const x = t => pad.l + t / tMax * (W - pad.l - pad.r);
    const y = v => pad.t + (1 - (v - lo) / span) * (H - pad.t - pad.b);
    let html = "";
    // profitable bands
    let run = null;
    path.forEach((pt, i) => {
      const ok = pt.bid != null && pt.bid > p.entry_ask;
      if (ok && run == null) run = pt.t;
      if ((!ok || i === path.length - 1) && run != null) {
        const t1 = ok ? pt.t : pt.t;
        html += `<rect x="${x(run)}" y="${pad.t}" width="${Math.max(1, x(t1)-x(run))}"
          height="${H-pad.t-pad.b}" fill="#0a7a2f" opacity="0.08"/>`;
        run = null;
      }
    });
    const line = (key, stroke) => {
      const pts = path.filter(pt => pt[key] != null);
      if (!pts.length) return "";
      const d = pts.map((pt,i) => {
        const v = pnlMode ? (pt[key] - p.entry_ask) / p.entry_ask : pt[key];
        return `${i?"L":"M"}${x(pt.t).toFixed(1)},${y(v).toFixed(1)}`;
      }).join(" ");
      return `<path d="${d}" fill="none" stroke="${stroke}" stroke-width="1.6"/>`;
    };
    html += line("ask", "#c62828");
    html += line("bid", "#1565c0");
    path.forEach(pt => {
      if (pt.bid == null) return;
      const v = pnlMode ? (pt.bid - p.entry_ask) / p.entry_ask : pt.bid;
      html += `<circle cx="${x(pt.t).toFixed(1)}" cy="${y(v).toFixed(1)}" r="1.6"
        fill="#1565c0"/>`;
    });
    const entryY = y(pnlMode ? 0 : p.entry_ask);
    html += `<line x1="${pad.l}" y1="${entryY}" x2="${W-pad.r}" y2="${entryY}"
      stroke="#111" stroke-dasharray="4 3"/>`;
    if (p.t_mfe != null && p.mfe != null) {
      const v = pnlMode ? p.mfe_pct : p.mfe;
      html += `<circle cx="${x(p.t_mfe)}" cy="${y(v)}" r="4" fill="#0a7a2f"/>`;
    }
    if (p.mae != null) {
      let maeT = 0, maeV = p.mae;
      path.forEach(pt => {
        if (pt.bid != null && (p.t_mfe == null || pt.t <= p.t_mfe) && pt.bid <= maeV) {
          maeT = pt.t; maeV = pt.bid;
        }
      });
      const v = pnlMode ? (maeV - p.entry_ask) / p.entry_ask : maeV;
      html += `<circle cx="${x(maeT)}" cy="${y(v)}" r="4" fill="#b00020"/>`;
    }
    if (p.t_decay != null) {
      html += `<line x1="${x(p.t_decay)}" y1="${pad.t}" x2="${x(p.t_decay)}"
        y2="${H-pad.b}" stroke="#7a3e00" stroke-dasharray="2 2"/>`;
    }
    html += `<text x="6" y="${entryY+4}" fill="#111">${pnlMode ? "0%" : fmt(p.entry_ask,2)}</text>`;
    html += `<text x="${W/2}" y="${H-6}" fill="#333" text-anchor="middle">minutes since first_seen</text>`;
    svg.innerHTML = html;
    drawSpot();
  }

  function drawSpot() {
    const svg = document.getElementById("spot");
    if (!svg) return;
    const p = selected;
    const W = svg.clientWidth || 1100, H = 160;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    if (!p) { svg.innerHTML = ""; return; }
    const pad = {l: 56, r: 12, t: 14, b: 28};
    const path = (p.path || []).filter(pt => pt.spot != null);
    const tMax = Math.max(1, ...((p.path || []).map(pt => pt.t)), 1);
    const spots = path.map(pt => pt.spot);
    if (p.strike != null) spots.push(p.strike);
    if (!spots.length) { svg.innerHTML = ""; return; }
    const lo = Math.min(...spots), hi = Math.max(...spots);
    const span = (hi - lo) || 1;
    const x = t => pad.l + t / tMax * (W - pad.l - pad.r);
    const y = v => pad.t + (1 - (v - lo) / span) * (H - pad.t - pad.b);
    let html = "";
    if (path.length) {
      const d = path.map((pt,i) =>
        `${i?"L":"M"}${x(pt.t).toFixed(1)},${y(pt.spot).toFixed(1)}`
      ).join(" ");
      html += `<path d="${d}" fill="none" stroke="#37474f" stroke-width="1.6"/>`;
      path.forEach(pt => {
        html += `<circle cx="${x(pt.t).toFixed(1)}" cy="${y(pt.spot).toFixed(1)}" r="1.6" fill="#37474f"/>`;
      });
    }
    html += `<line x1="${pad.l}" y1="${y(p.strike)}" x2="${W-pad.r}" y2="${y(p.strike)}"
      stroke="#b00020" stroke-dasharray="4 3"/>`;
    html += `<line x1="${x(0)}" y1="${pad.t}" x2="${x(0)}" y2="${H-pad.b}"
      stroke="#1a3a8a" stroke-dasharray="2 2"/>`;
    html += `<text x="6" y="${y(p.strike)+4}" fill="#b00020">${fmt(p.strike,1)}</text>`;
    html += `<text x="${W/2}" y="${H-6}" fill="#333" text-anchor="middle">spot vs strike · first_seen at t=0</text>`;
    svg.innerHTML = html;
  }

  function annotate() {
    const p = selected;
    const el = document.getElementById("ann");
    if (!p) { el.textContent = ""; return; }
    const bits = [
      p.label,
      `ask ${fmt(p.entry_ask,2)}`,
      `MFE ${_fmtMaybe(p.mfe)} (${fmtPct(p.mfe_pct)}) @ ${fmt(p.t_mfe,0)}m`,
      `MAE ${_fmtMaybe(p.mae)} (${fmtPct(p.mae_pct)})`,
      `streak ${p.max_win_streak_scans} scans / ${fmt(p.median_scan_interval_sec,0)}s median gap`,
      `profitable ${p.scans_profitable} scans`,
      `t_decay ${p.t_decay == null ? "N/A" : fmt(p.t_decay,0) + "m"}`,
      `cov ${fmt(p.coverage_pct,0)}% n=${p.n_snapshots}`,
      `δ ${fmt(p.delta,2)} mny ${fmtPct(p.moneyness)} sprd ${fmtPct(p.spread_pct)}`,
      p.paired_pick_id ? `paired ${p.paired_pick_id}` : null,
    ].filter(Boolean);
    el.textContent = bits.join("  ·  ");
  }
  function _fmtMaybe(x) {
    if (x === null || x === undefined) return "—";
    return Number(x).toFixed(2);
  }

  document.getElementById("overlay").addEventListener("click", () => {});
  document.getElementById("btn-px").addEventListener("click", () => {
    pnlMode = false;
    document.getElementById("btn-px").classList.add("on");
    document.getElementById("btn-pnl").classList.remove("on");
    drawDetail();
  });
  document.getElementById("btn-pnl").addEventListener("click", () => {
    pnlMode = true;
    document.getElementById("btn-pnl").classList.add("on");
    document.getElementById("btn-px").classList.remove("on");
    drawDetail();
  });
  document.querySelectorAll("tr[data-id]").forEach(tr => {
    tr.addEventListener("click", () => select(tr.dataset.id));
  });
  document.querySelectorAll("th[data-k]").forEach(th => {
    th.addEventListener("click", () => {
      const k = th.dataset.k;
      if (sortKey === k) sortDir *= -1;
      else { sortKey = k; sortDir = 1; }
      const tb = document.querySelector("tbody");
      const rows = Array.from(tb.querySelectorAll("tr"));
      rows.sort((a, b) => {
        const pa = byId[a.dataset.id], pb = byId[b.dataset.id];
        const va = pa[k], vb = pb[k];
        if (va == null && vb == null) return 0;
        if (va == null) return 1;
        if (vb == null) return -1;
        if (va < vb) return -1 * sortDir;
        if (va > vb) return 1 * sortDir;
        return 0;
      });
      rows.forEach(r => tb.appendChild(r));
    });
  });
  window.addEventListener("resize", () => { drawOverlay(); drawDetail(); });
  if (selected) select(selected.id);
  else { drawOverlay(); }
})();
"""


def _td(text: str, *, num: bool = False, bad: bool = False) -> str:
    cls = []
    if num:
        cls.append("num")
    if bad:
        cls.append("bad")
    attr = f' class="{" ".join(cls)}"' if cls else ""
    return f"<td{attr}>{text}</td>"


def render_html(rep: SessionReport) -> str:
    payload = {
        "session": rep.session,
        "ticker": rep.ticker,
        "generated": rep.generated.isoformat(timespec="seconds"),
        "reconstructed": True,
        "benchmark": rep.benchmark,
        "picks": [_pick_json(p) for p in rep.picks],
    }
    blob = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    rows = []
    for p in rep.picks:
        h = p.hostile
        cls = ["low"] if p.low_coverage else []
        rank_cell = str(p.first_seen_rank)
        if p.low_coverage:
            rank_cell += ' <span class="badge">INSUFFICIENT DATA</span>'
        rows.append(
            f'<tr data-id="{p.pick_id}" class="{" ".join(cls)}">'
            + _td(rank_cell, num=True)
            + _td(p.first_seen.strftime("%H:%M"))
            + _td(_strike_label(p.strike), num=True)
            + _td(_right(p.side))
            + _td(p.expiry)
            + _td(_fmt_int(p.dte), num=True)
            + _td(_fmt_num(p.spot_at_flag, 2), num=True)
            + _td(_fmt_pct(p.moneyness), num=True, bad=h["moneyness"])
            + _td(_fmt_num(p.delta, 2), num=True, bad=h["delta"])
            + _td(_fmt_num(p.iv, 3) if p.iv is not None else "—", num=True)
            + _td(_fmt_pct(p.spread_pct), num=True, bad=h["spread"])
            + _td(_fmt_num(p.entry_ask, 2), num=True)
            + _td(_fmt_int(p.volume), num=True)
            + _td(_fmt_int(p.oi), num=True)
            + _td(_fmt_num(p.score, 4), num=True)
            + (
                _td("INSUFFICIENT DATA", bad=True)
                + _td("—", num=True)
                + _td("—", num=True)
                + _td("—", num=True)
                + _td("—", num=True)
                + _td("—", num=True)
                + _td("—", num=True)
                + _td(_fmt_num(p.mark_expiry, 2), num=True)
                if p.low_coverage else
                _td(_fmt_pct(p.mfe_pct), num=True)
                + _td(_fmt_num(p.t_mfe, 0), num=True)
                + _td(_fmt_pct(p.mae_pct), num=True)
                + _td(str(p.max_win_streak_scans), num=True)
                + _td(str(p.scans_profitable), num=True)
                + _td(_fmt_num(p.mark_close, 2), num=True)
                + _td(_fmt_pct(p.pnl_close), num=True)
                + _td(_fmt_num(p.mark_expiry, 2), num=True)
            )
            + _td(_fmt_pct(p.spread_cost), num=True)
            + _td(_fmt_num(p.coverage_pct, 0), num=True, bad=h["coverage"])
            + _td(_fmt_num(p.median_scan_interval_sec, 0), num=True)
            + _td(str(p.n_snapshots), num=True)
            + _td(str(p.first_seen_rank), num=True)
            + _td(_fmt_int(p.best_rank), num=True)
            + _td(_fmt_num(p.minutes_in_top10, 0), num=True)
            + f'<td>{_sparkline_svg(p)}</td>'
            + "</tr>"
        )
    q = rep.quality
    footer = [
        f"F-03 rows scored above 1.0: {q.n_score_gt_1} "
        f"(historical; cap is engine-v1.3 going forward)",
        (
            f"sealed stale/unavailable  t15m {q.n_t15m_stale}/{q.n_t15m_unavail}  "
            f"t30m {q.n_t30m_stale}/{q.n_t30m_unavail}  "
            f"close {q.n_close_stale}/{q.n_close_unavail}"
        ),
        * _caveats(),
    ]
    th = "".join(
        f'<th data-k="{k}">{lab}</th>'
        for k, lab in [
            ("first_seen_rank", "rank"),
            ("first_seen", "time"),
            ("strike", "strike"),
            ("side", "right"),
            ("expiry", "expiry"),
            ("dte", "DTE"),
            ("spot", "spot@flag"),
            ("moneyness", "mny"),
            ("delta", "delta"),
            ("iv", "IV"),
            ("spread_pct", "spread%"),
            ("entry_ask", "ask"),
            ("volume", "vol"),
            ("oi", "OI"),
            ("score", "score"),
            ("mfe_pct", "MFE"),
            ("t_mfe", "t_MFE"),
            ("mae_pct", "MAE"),
            ("max_win_streak_scans", "streak"),
            ("scans_profitable", "win#"),
            ("mark_close", "close"),
            ("pnl_close", "P/L%"),
            ("mark_expiry", "expiry$"),
            ("spread_cost", "spread_cost"),
            ("coverage_pct", "cov%"),
            ("median_scan_interval_sec", "int s"),
            ("n_snapshots", "n"),
            ("first_seen_rank", "fs#"),
            ("best_rank", "best#"),
            ("minutes_in_top10", "min10"),
            ("", "path"),
        ]
    )
    gen = rep.generated.strftime("%Y-%m-%d %H:%M")
    omit_html = ""
    if rep.n_omitted:
        omit_html = (
            f'<div class="meta">{rep.n_omitted} other 1DTE+ names entered rank 1–10; '
            "omitted (not in top 10 by score at first_seen).</div>"
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Picks ledger {rep.session} {rep.ticker}</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<header>
  <div class="title">PICKS LEDGER — {rep.session} — {rep.ticker} · 1DTE+</div>
  <div class="recon">RECONSTRUCTED from flags — historical backfill, not a live-captured pick log.</div>
  <div class="sum">{rep.summary}</div>
  <div class="meta">n_distinct_underlying_views {rep.n_views} / {len(rep.picks)} picks
    · scans={rep.n_session_scans}
    · generated {gen}
    · db {rep.db_path}</div>
  {omit_html}
  <div class="meta">Ask-entry / bid-exit. Overlay X = minutes since each pick&rsquo;s first_seen.
    mark_close = last live bid (09:30–16:15). mark_expiry is settlement, not an exit.</div>
</header>
<h2>Overlay</h2>
<div class="wrap">
  <svg id="overlay"></svg>
  <div class="legend" id="ov-legend"></div>
  <div class="legend">Y = (bid − entry_ask) / entry_ask. Dashed grey = random same-DTE/same-side non-pick.
    Hover identifies; click a line or a row.</div>
</div>
<h2>Ledger</h2>
<div class="wrap" style="overflow-x:auto">
<table>
<thead><tr>{th}</tr></thead>
<tbody>
{''.join(rows)}
</tbody>
</table>
</div>
<h2>Selected pick</h2>
<div class="toggle">
  <button id="btn-px" class="on">bid / ask</button>
  <button id="btn-pnl">P/L%</button>
</div>
<div class="wrap"><svg id="detail"></svg></div>
<div class="wrap"><svg id="spot"></svg></div>
<div id="ann"></div>
<div id="tip"></div>
<div class="footer">
{''.join(f'<div>{line}</div>' for line in footer)}
</div>
<script>const LEDGER = {blob};</script>
<script>{_HTML_JS}</script>
</body>
</html>
"""


def session_paths(session: str, ticker: str, report_dir: str) -> tuple[str, str]:
    os.makedirs(report_dir, exist_ok=True)
    stem = f"eod_{session}_{ticker.upper()}_ledger"
    return (
        os.path.join(report_dir, stem + ".html"),
        os.path.join(report_dir, stem + ".txt"),
    )


def write_session(rep: SessionReport, report_dir: str) -> tuple[str, str]:
    html_path, txt_path = session_paths(rep.session, rep.ticker, report_dir)
    with open(txt_path, "w", encoding="utf-8") as fh:
        fh.write(render_txt(rep))
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(render_html(rep))
    return html_path, txt_path


def _index_path(report_dir: str) -> str:
    return os.path.join(report_dir, "index.html")


def _index_json_path(report_dir: str) -> str:
    return os.path.join(report_dir, INDEX_JSON)


def update_index(rep: SessionReport, report_dir: str, html_path: str) -> None:
    path = _index_json_path(report_dir)
    data: dict[str, Any] = {}
    if os.path.exists(path):
        try:
            data = json.loads(open(path, encoding="utf-8").read())
        except (OSError, json.JSONDecodeError):
            data = {}
    key = f"{rep.session}|{rep.ticker}"
    data[key] = {
        "session": rep.session,
        "ticker": rep.ticker,
        "n_picks": len(rep.picks),
        "n_views": rep.n_views,
        "n_reached_30": rep.n_reached_30,
        "median_mfe": rep.median_mfe,
        "median_t_mfe": rep.median_t_mfe,
        "median_coverage": rep.median_coverage,
        "median_scan_interval_sec": rep.median_scan_interval_sec,
        "href": os.path.basename(html_path),
        "summary": rep.summary,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    write_index_html(data, report_dir)


def write_index_html(data: dict[str, Any], report_dir: str) -> None:
    rows = sorted(data.values(), key=lambda r: (r["session"], r["ticker"]), reverse=True)
    body = []
    for r in rows:
        body.append(
            "<tr>"
            f'<td><a href="{r["href"]}">{r["session"]}</a></td>'
            f'<td>{r["ticker"]}</td>'
            f'<td class="num">{r["n_picks"]}</td>'
            f'<td class="num">{r.get("n_views", "—")}</td>'
            f'<td class="num">{_fmt_pct(r.get("median_mfe"))}</td>'
            f'<td class="num">{r.get("n_reached_30", 0)}</td>'
            f'<td class="num">{_fmt_num(r.get("median_coverage"), 0)}</td>'
            "</tr>"
        )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Picks ledger index</title>
<style>{_HTML_CSS}</style></head>
<body>
<header>
  <div class="title">PICKS LEDGER INDEX</div>
  <div class="meta">{len(rows)} sessions · 1DTE+ · ask-entry / bid-exit</div>
</header>
<div class="wrap" style="overflow-x:auto">
<table>
<thead><tr>
  <th>date</th><th>ticker</th><th>n picks</th><th>views</th>
  <th>median MFE</th><th>n +30% MFE</th><th>median cov%</th>
</tr></thead>
<tbody>
{''.join(body)}
</tbody>
</table>
</div>
</body></html>
"""
    with open(_index_path(report_dir), "w", encoding="utf-8") as fh:
        fh.write(html)


def generate_range(
    *,
    db_path: str,
    ticker: str,
    start: str,
    end: str | None,
    report_dir: str,
) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    if end is None:
        end = latest_session(conn, ticker) or start
    sessions = list_sessions(conn, ticker=ticker, start=start, end=end)
    written: list[tuple[str, str]] = []
    gen = datetime.now(ET)
    for sess in sessions:
        rep = build_session(
            conn, ticker=ticker, session=sess, db_path=db_path, generated=gen,
        )
        html_path, txt_path = write_session(rep, report_dir)
        update_index(rep, report_dir, html_path)
        written.append((html_path, txt_path))
        print(f"  wrote {html_path}")
        print(f"  wrote {txt_path}")
    conn.close()
    return written


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Interactive picks ledger (1DTE+)")
    p.add_argument("--date", "--session", dest="session",
                   help="YYYY-MM-DD trading session in ET (default: ET today)")
    p.add_argument("--ticker", default="AAPL")
    p.add_argument("--db", default=DB)
    p.add_argument("--out-dir", default=REPORT_DIR)
    p.add_argument(
        "--backfill",
        nargs="*",
        metavar="DATE",
        help="all sessions in range; 0 args → 2026-08-10 → latest; else START END",
    )
    p.add_argument(
        "--legacy",
        action="store_true",
        help="ignored here; eod_report.py uses this to keep the aggregate report",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    ticker = (a.ticker or "AAPL").upper()
    os.makedirs(a.out_dir, exist_ok=True)
    if a.backfill is not None:
        if len(a.backfill) == 0:
            start, end = BACKFILL_START, None
        elif len(a.backfill) == 2:
            start, end = a.backfill[0], a.backfill[1]
        else:
            print("error: --backfill expects 0 or 2 dates", file=sys.stderr)
            return 2
        print(f"backfill {ticker} {start} → {end or 'latest'}")
        generate_range(
            db_path=a.db, ticker=ticker, start=start, end=end, report_dir=a.out_dir,
        )
        print(f"  wrote {os.path.join(a.out_dir, 'index.html')}")
        return 0

    conn = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    session = a.session or session_date_et()
    if not a.session:
        latest = latest_session(conn, ticker)
        n = conn.execute(
            """SELECT COUNT(*) FROM flags
               WHERE ticker=? AND substr(ts_et,1,10)=? AND pool=?""",
            (ticker, session, POOL_1DTE),
        ).fetchone()[0]
        if not n and latest:
            session = latest
    rep = build_session(
        conn, ticker=ticker, session=session, db_path=a.db,
    )
    conn.close()
    html_path, txt_path = write_session(rep, a.out_dir)
    update_index(rep, a.out_dir, html_path)
    print(render_txt(rep), end="")
    print(f"  wrote {txt_path}")
    print(f"  wrote {html_path}")
    return 0 if rep.picks else 1


if __name__ == "__main__":
    raise SystemExit(main())
