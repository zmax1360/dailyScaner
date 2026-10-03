#!/usr/bin/env python3
"""
delta_repair_reach.py — how many contracts would engine-v1.5 delta repair touch?

Read-only replay. For every stored contract snapshot in data/volume_history.db it
computes delta twice — vendor IV (<= engine-v1.4) and quote-repaired IV (engine-v1.5,
greeks.quote_repaired_iv) — and reports how often and how far they differ.

Reads:  data/volume_history.db (opened read-only), archive/{scan_id}.json (spot only).
Writes: nothing. No network. Not a scoring input; does not touch config.SCORING.

0DTE and 1DTE+ are reported separately and never pooled. Counts are given per snapshot
row and per distinct contract-session (a contract is "repaired" if any snapshot was).

Usage:
    python scripts/delta_repair_reach.py                       # all sessions, AAPL
    python scripts/delta_repair_reach.py --ticker NVDA --since 2026-09-28
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import sys
from contextlib import closing
from datetime import date, datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config import SCORING  # noqa: E402
from greeks import bs_delta, effective_dte_days, quote_repaired_iv  # noqa: E402
from scoring_pool import POOL_0DTE, POOL_1DTE  # noqa: E402

SATURATED = 0.9995          # rounds to 1.000 at three decimals
GATE_LOW = 0.15             # attribution-derived |delta| gate (handover 8.2)
BAND = (0.35, 0.50)         # Best Value display band

UNCHANGED = "unchanged"
REPAIRED = "repaired"
DROPPED = "repair_unsolvable"       # repair needed, no IV fits the mid -> NaN
NO_VENDOR_DELTA = "no_vendor_delta"  # vendor IV missing / below floor (as before)


def load_spot(archive_dir: str, scan_id: str, cache: dict[str, float | None]) -> float | None:
    """Spot for one scan from its archive JSON. None when absent — never a default."""
    if scan_id in cache:
        return cache[scan_id]
    spot: float | None = None
    try:
        with open(os.path.join(archive_dir, f"{scan_id}.json"), encoding="utf-8") as fh:
            v = float(json.load(fh).get("spot"))
        if v > 0:
            spot = v
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        spot = None
    cache[scan_id] = spot
    return spot


def classify(side, spot, strike, t_days, iv, bid, ask, r):
    """Return (status, vendor_delta, repaired_delta)."""
    d_vendor = bs_delta(side, spot, strike, t_days, iv, r=r) if iv is not None else None
    if d_vendor is None:
        return NO_VENDOR_DELTA, None, None
    new_iv = quote_repaired_iv(side, spot, strike, t_days, iv, bid, ask, r=r)
    if new_iv is None:
        return DROPPED, d_vendor, None
    if new_iv == iv:
        return UNCHANGED, d_vendor, d_vendor
    return REPAIRED, d_vendor, bs_delta(side, spot, strike, t_days, new_iv, r=r)


def _in_band(d: float) -> bool:
    return BAND[0] <= abs(d) <= BAND[1]


def replay(db_path: str, archive_dir: str, *, ticker: str, since: str | None,
           min_volume: float) -> dict:
    r = float(SCORING.get("risk_free_rate", 0.045))
    sql = (
        "SELECT scan_id, ts_et, session_date, side, strike, expiry, volume, bid, ask, iv "
        "FROM contract_scans WHERE ticker = ?"
    )
    params: list = [ticker.upper()]
    if since:
        sql += " AND session_date >= ?"
        params.append(since)

    pools = {
        p: {"rows": 0, "status": {}, "abs_shift": [], "unsaturated": 0, "gate_up": 0,
            "band_in": 0, "band_out": 0, "contracts": set(), "contracts_touched": set()}
        for p in (POOL_0DTE, POOL_1DTE)
    }
    skipped = {"no_spot": 0, "below_min_volume": 0, "expired": 0}
    sessions: set[str] = set()
    spot_cache: dict[str, float | None] = {}

    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as con:
        for scan_id, ts_et, sess, side, strike, expiry, volume, bid, ask, iv in con.execute(
            sql, params
        ):
            if volume is None or volume < min_volume:
                skipped["below_min_volume"] += 1
                continue
            spot = load_spot(archive_dir, scan_id, spot_cache)
            if spot is None:
                skipped["no_spot"] += 1
                continue
            dte = (date.fromisoformat(expiry) - date.fromisoformat(sess)).days
            if dte < 0:
                skipped["expired"] += 1
                continue
            now = datetime.fromisoformat(ts_et)
            t_days = effective_dte_days(dte, expiry=expiry, now_et=now)
            status, d0, d1 = classify(side, spot, strike, t_days, iv, bid, ask, r)

            p = pools[POOL_0DTE if dte == 0 else POOL_1DTE]
            key = (sess, side, strike, expiry)
            sessions.add(sess)
            p["rows"] += 1
            p["status"][status] = p["status"].get(status, 0) + 1
            p["contracts"].add(key)
            if status in (REPAIRED, DROPPED):
                p["contracts_touched"].add(key)
            if status == REPAIRED:
                p["abs_shift"].append(abs(d1 - d0))
                if abs(d0) >= SATURATED and abs(d1) < SATURATED:
                    p["unsaturated"] += 1
                if abs(d0) < GATE_LOW <= abs(d1):
                    p["gate_up"] += 1
                if not _in_band(d0) and _in_band(d1):
                    p["band_in"] += 1
                if _in_band(d0) and not _in_band(d1):
                    p["band_out"] += 1
    return {"pools": pools, "skipped": skipped, "sessions": sorted(sessions)}


def _pct(n: int, d: int) -> str:
    return f"{100.0 * n / d:5.1f}%" if d else "   n/a"


def _quantile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def format_report(res: dict, *, ticker: str, min_volume: float) -> str:
    out = [
        f"Delta repair reach — {ticker.upper()}  (read-only replay, nothing written)",
        f"sessions: {len(res['sessions'])}"
        + (f"  ({res['sessions'][0]} .. {res['sessions'][-1]})" if res["sessions"] else ""),
        f"min volume: {min_volume:g}   skipped rows: {res['skipped']}",
    ]
    for name, p in res["pools"].items():
        n = p["rows"]
        out += ["", f"== {name} =="]
        if not n:
            out.append("  no rows")
            continue
        st = p["status"]
        out.append(f"  snapshot rows: {n}")
        for k in (UNCHANGED, REPAIRED, DROPPED, NO_VENDOR_DELTA):
            out.append(f"    {k:<18} {st.get(k, 0):>8}  {_pct(st.get(k, 0), n)}")
        nc = len(p["contracts"])
        out.append(
            f"  distinct contract-sessions: {nc}   touched in any snapshot: "
            f"{len(p['contracts_touched'])}  {_pct(len(p['contracts_touched']), nc)}"
        )
        sh = p["abs_shift"]
        if sh:
            out.append(
                "  |delta| shift on repaired rows: "
                f"median {statistics.median(sh):.3f}   p90 {_quantile(sh, 0.90):.3f}   "
                f"max {max(sh):.3f}"
            )
            out.append(f"    left saturation (|d| >= {SATURATED} -> below): {p['unsaturated']}")
            out.append(f"    crossed up through the {GATE_LOW} gate:        {p['gate_up']}")
            out.append(
                f"    entered / left the {BAND[0]}-{BAND[1]} band:    "
                f"{p['band_in']} / {p['band_out']}"
            )
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--ticker", default="AAPL")
    ap.add_argument("--since", help="first session date, YYYY-MM-DD")
    ap.add_argument("--min-volume", type=float, default=float(SCORING["min_volume"]),
                    help="ignore snapshots below this session volume "
                         "(default: SCORING['min_volume'], the scoring universe gate)")
    ap.add_argument("--db", default=os.path.join(_ROOT, "data", "volume_history.db"))
    ap.add_argument("--archive-dir", default=os.path.join(_ROOT, "archive"))
    a = ap.parse_args(argv)
    if not os.path.exists(a.db):
        print(f"no database at {a.db}", file=sys.stderr)
        return 2
    res = replay(a.db, a.archive_dir, ticker=a.ticker, since=a.since,
                 min_volume=a.min_volume)
    print(format_report(res, ticker=a.ticker, min_volume=a.min_volume))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
