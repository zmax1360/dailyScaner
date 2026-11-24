"""One-time cleanup of data/volume_history.db to the config.VOLUME_HISTORY window.

Dry run by default: shows, per ticker, how many history rows fall outside the window.

    python scripts/volume_history_cleanup.py            # dry run (read-only)
    python scripts/volume_history_cleanup.py --apply    # clean up

--apply, in order:
  1. copies each ticker's newest scan (full chain) into ``latest_chain`` (Gamma page)
  2. stores each session's anchor spot in ``session_anchor`` (spot from the session's first
     scan: archive/<scan_id>.json, else put-call parity on that scan's chain)
  3. archives the rows it removes to data/archive/volume_history_pruned_<TICKER>.csv.gz
     (skip with --no-archive)
  4. deletes them
  5. VACUUM (skip with --no-vacuum) so the file actually shrinks

Stop the scheduler first and keep a backup (data/backups/). Sessions without a usable spot
are left untouched and listed.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import sqlite3
import sys
from contextlib import closing

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import pandas as pd  # noqa: E402

import volume_history as vh  # noqa: E402

COLS = ["ticker", "scan_id", "ts_et", "session_date", "side", "strike", "expiry", "volume",
        "open_interest", "bid", "ask", "last", "iv", "source"]
OUTSIDE = ("ticker=? AND session_date=? AND (strike < ? OR strike > ?"
           " OR julianday(expiry) - julianday(session_date) > ?"
           " OR julianday(expiry) < julianday(session_date))")


def session_spot(con: sqlite3.Connection, ticker: str, session: str,
                 archive_dir: str) -> tuple[float | None, str, str]:
    """(spot, first scan_id, how) for a session; spot None when nothing usable."""
    scan_id, ts = con.execute(
        "SELECT scan_id, ts_et FROM contract_scans WHERE ticker=? AND session_date=?"
        " ORDER BY ts_et LIMIT 1", (ticker, session)).fetchone()
    row = con.execute("SELECT spot FROM session_anchor WHERE ticker=? AND session_date=?",
                      (ticker, session)).fetchone() if _has(con, "session_anchor") else None
    if row:
        return float(row[0]), ts, "anchor"
    path = os.path.join(archive_dir, f"{scan_id}.json")
    try:
        with open(path) as f:
            spot = float(json.load(f).get("spot") or 0)
        if spot > 0:
            return spot, ts, "archive"
    except (OSError, ValueError, TypeError):
        pass
    chain = pd.read_sql_query(
        "SELECT side, strike, expiry, bid, ask, last FROM contract_scans WHERE ticker=? AND"
        " scan_id=?", con, params=(ticker, scan_id))
    spot = vh.parity_spot(chain)
    return (spot, ts, "parity") if spot else (None, ts, "none")


def _has(con: sqlite3.Connection, table: str) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (table,)).fetchone() is not None


def plan(con: sqlite3.Connection, archive_dir: str) -> tuple[list[dict], list[tuple]]:
    """Per-session prune plan and the sessions that are skipped (no spot)."""
    out, skipped = [], []
    for ticker, session, total in con.execute(
            "SELECT ticker, session_date, COUNT(*) FROM contract_scans"
            " GROUP BY ticker, session_date ORDER BY ticker, session_date").fetchall():
        win = vh.window(ticker)
        if not win:
            continue
        spot, ts, how = session_spot(con, ticker, session, archive_dir)
        if not spot:
            skipped.append((ticker, session))
            continue
        lo, hi = vh.prune_bounds(spot, win["strike_pct"])
        args = (ticker, session, lo, hi, win["max_dte"])
        n = con.execute(f"SELECT COUNT(*) FROM contract_scans WHERE {OUTSIDE}", args).fetchone()[0]
        out.append({"ticker": ticker, "session": session, "spot": spot, "ts": ts, "how": how,
                    "args": args, "total": total, "prune": n})
    return out, skipped


def report(rows: list[dict], skipped: list[tuple], db_bytes: int, total_rows: int) -> None:
    df = pd.DataFrame(rows)
    if df.empty:
        print("nothing to prune")
        return
    g = df.groupby("ticker").agg(sessions=("session", "count"), rows=("total", "sum"),
                                 prune=("prune", "sum"))
    g["keep"] = g["rows"] - g["prune"]
    g["prune_%"] = (100 * g["prune"] / g["rows"]).round(1)
    print(g.to_string())
    pruned = int(df["prune"].sum())
    est = db_bytes * pruned / total_rows if total_rows else 0
    print(f"\ntotal: prune {pruned:,} of {total_rows:,} rows "
          f"({100 * pruned / total_rows:.1f}%), ~{est / 1e9:.2f} GB freed after VACUUM "
          f"(file now {db_bytes / 1e9:.2f} GB)")
    print("spot source:", df["how"].value_counts().to_dict())
    if skipped:
        print(f"skipped (no spot, left untouched): {skipped}")


def apply(con: sqlite3.Connection, rows: list[dict], archive_dir_out: str | None) -> None:
    con.executescript(vh._SCHEMA)                      # latest_chain / session_anchor
    for t in sorted({r["ticker"] for r in rows}):
        con.execute("DELETE FROM latest_chain WHERE ticker=?", (t,))
        con.execute(
            "INSERT INTO latest_chain (ticker, scan_id, ts_et, side, strike, expiry, volume,"
            " open_interest, bid, ask, last, iv, source) SELECT ticker, scan_id, ts_et, side,"
            " strike, expiry, volume, open_interest, bid, ask, last, iv, source"
            " FROM contract_scans WHERE ticker=? AND ts_et=(SELECT MAX(ts_et) FROM"
            " contract_scans WHERE ticker=?)", (t, t))
    con.commit()
    writers = {}
    try:
        for r in rows:
            con.execute("INSERT OR IGNORE INTO session_anchor (ticker, session_date, spot, ts_et)"
                        " VALUES (?,?,?,?)", (r["ticker"], r["session"], r["spot"], r["ts"]))
            if r["prune"] and archive_dir_out:
                if r["ticker"] not in writers:
                    os.makedirs(archive_dir_out, exist_ok=True)
                    p = os.path.join(archive_dir_out, f"volume_history_pruned_{r['ticker']}.csv.gz")
                    new = not os.path.exists(p)
                    fh = gzip.open(p, "at", newline="")
                    w = csv.writer(fh)
                    if new:
                        w.writerow(COLS)
                    writers[r["ticker"]] = (fh, w)
                w = writers[r["ticker"]][1]
                for rec in con.execute(f"SELECT {', '.join(COLS)} FROM contract_scans"
                                       f" WHERE {OUTSIDE}", r["args"]):
                    w.writerow(rec)
            if r["prune"]:
                con.execute(f"DELETE FROM contract_scans WHERE {OUTSIDE}", r["args"])
            con.commit()
            print(f"  {r['ticker']} {r['session']}: removed {r['prune']:,}")
    finally:
        for fh, _ in writers.values():
            fh.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--db", default=vh.DB_PATH)
    ap.add_argument("--archive-dir", default=os.path.join(_ROOT, "archive"),
                    help="scan JSON archive (for each session's spot)")
    ap.add_argument("--apply", action="store_true", help="delete (default: dry run)")
    ap.add_argument("--no-archive", action="store_true", help="do not keep removed rows")
    ap.add_argument("--no-vacuum", action="store_true")
    a = ap.parse_args(argv)

    db_bytes, = (os.path.getsize(a.db),)
    uri = f"file:{a.db}?mode=ro" if not a.apply else f"file:{a.db}"
    with closing(sqlite3.connect(uri, uri=True, timeout=60)) as con:
        total = con.execute("SELECT COUNT(*) FROM contract_scans").fetchone()[0]
        rows, skipped = plan(con, a.archive_dir)
        report(rows, skipped, db_bytes, total)
        if not a.apply:
            print("\ndry run: nothing changed. Re-run with --apply (scheduler stopped, backup made).")
            return 0
        out = None if a.no_archive else os.path.join(os.path.dirname(a.db), "archive")
        apply(con, rows, out)
        if not a.no_vacuum:
            print("VACUUM ...")
            con.execute("VACUUM")
    print(f"done: {db_bytes / 1e9:.2f} GB -> {os.path.getsize(a.db) / 1e9:.2f} GB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
