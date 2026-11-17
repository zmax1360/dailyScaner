#!/usr/bin/env python3
"""Build the 0DTE move history the Game Plan strategy simulates with.

    python scripts/build_move_samples.py                     # all tickers with history
    python scripts/build_move_samples.py --root ~/works/optionTrading

For every stored session whose nearest expiry is that same day (0DTE) and every entry
time in ENTRY_TIMES, it takes the chain snapshot nearest that time and records:

    spot      the scan's price          implied   ATM straddle mid (the market's move)
    regime    POSITIVE / NEGATIVE / UNSETTLED from that snapshot's own gamma read
    close     the official close (data/research/official_closes.csv), else the price of
              the last scan at or before 16:00
    z         (close − spot) / implied: the move that followed, in implied-move units

``regime_strategy`` replays today's candidate structures over these z values, scaled
by today's implied move and priced at today's quotes. Writes
data/research/move_samples.json. Read-only on the databases.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from contextlib import closing
from datetime import date, datetime, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

import game_plan  # noqa: E402
import gex  # noqa: E402

ENTRY_TIMES = ("12:00", "12:30", "13:00", "13:30", "14:00", "14:30")
TOLERANCE_MIN = 15
CLOSE_BY = time(16, 0, 59)


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _spot(archive: str, scan_id: str):
    try:
        with open(os.path.join(archive, f"{scan_id}.json")) as f:
            return _num(json.load(f).get("spot"))
    except (OSError, ValueError):
        return None


def official_closes(path: str) -> dict:
    out = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                out[(r["ticker"].upper(), r["date"])] = float(r["close"])
    return out


def build(root: Path, tickers: list[str] | None = None) -> list[dict]:
    db = root / "data" / "volume_history.db"
    archive = str(root / "archive")
    closes = official_closes(str(root / "data" / "research" / "official_closes.csv"))
    out = []
    with closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)) as con:
        names = tickers or [r[0] for r in con.execute(
            "SELECT DISTINCT ticker FROM contract_scans")]
        for t in names:
            scans = defaultdict(list)
            for day, sid, ts in con.execute(
                    "SELECT DISTINCT session_date, scan_id, ts_et FROM contract_scans "
                    "WHERE ticker=? ORDER BY ts_et", (t,)):
                scans[day].append((sid, datetime.fromisoformat(ts)))
            for day, day_scans in sorted(scans.items()):
                if date.fromisoformat(day).weekday() >= 5:
                    continue
                close = closes.get((t, day))
                if close is None:
                    last = [s for s in day_scans if s[1].time() <= CLOSE_BY]
                    close = _spot(archive, last[-1][0]) if last else None
                if close is None:
                    continue
                used = set()
                for hhmm in ENTRY_TIMES:
                    h, m = map(int, hhmm.split(":"))
                    best = min(day_scans, key=lambda s: abs(s[1].hour * 60 + s[1].minute - h * 60 - m))
                    if abs(best[1].hour * 60 + best[1].minute - h * 60 - m) > TOLERANCE_MIN \
                            or best[0] in used:
                        continue
                    used.add(best[0])
                    sid, as_of = best
                    exp = con.execute("SELECT MIN(expiry) FROM contract_scans WHERE ticker=? "
                                      "AND scan_id=? AND expiry>=?", (t, sid, day)).fetchone()[0]
                    if exp != day:
                        continue                                  # 0DTE only
                    spot = _spot(archive, sid)
                    if not spot:
                        continue
                    df = pd.read_sql_query(
                        "SELECT side, strike, expiry, volume, open_interest, bid, ask, last, iv "
                        "FROM contract_scans WHERE ticker=? AND scan_id=? AND expiry=?",
                        con, params=(t, sid, exp))
                    s = gex.summary(df, spot=spot, as_of=as_of, today=date.fromisoformat(day))
                    if not s:
                        continue
                    regime = game_plan.regime_from_gamma(s, spot)[0]
                    implied = straddle(df, spot)
                    if not implied:
                        continue
                    out.append({"ticker": t, "day": day, "entry": hhmm, "spot": round(spot, 2),
                                "close": close, "implied": round(implied, 3),
                                "z": round((close - spot) / implied, 4), "regime": regime})
    return out


def straddle(df: pd.DataFrame, spot: float) -> float | None:
    """ATM call + put mid: the move the market prices to the expiry."""
    best = None
    for k in sorted(df["strike"].unique(), key=lambda k: abs(k - spot))[:1]:
        legs = df[df["strike"] == k]
        mids = []
        for side in ("CALL", "PUT"):
            r = legs[legs["side"].str.upper() == side]
            if r.empty:
                return None
            b, a = _num(r.iloc[0]["bid"]), _num(r.iloc[0]["ask"])
            if b is None or a is None or a <= 0 or a < b:
                return None
            mids.append(0.5 * (a + b))
        best = sum(mids)
    return best


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--tickers", nargs="*")
    args = ap.parse_args()
    root = Path(os.path.expanduser(args.root))
    rows = build(root, [t.upper() for t in args.tickers] if args.tickers else None)
    out = root / "data" / "research" / "move_samples.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"built_at": datetime.now().isoformat(timespec="seconds"),
                               "entry_times": ENTRY_TIMES, "samples": rows}, indent=1))
    by = defaultdict(lambda: [0, set()])
    for r in rows:
        by[(r["ticker"], r["regime"])][0] += 1
        by[(r["ticker"], r["regime"])][1].add(r["day"])
    print(f"{len(rows)} samples → {out}")
    for (t, reg), (n, days) in sorted(by.items()):
        print(f"  {t:<5} {reg:<10} {n:>3} samples over {len(days):>2} days")


if __name__ == "__main__":
    main()
