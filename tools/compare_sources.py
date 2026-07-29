#!/usr/bin/env python3
"""
tools/compare_sources.py — A/B Yahoo vs Massive at the same moment.

Usage:
  python tools/compare_sources.py AAPL
  python tools/compare_sources.py AAPL --max-dte 45

Reports per-contract volume/bid/ask/iv/delta differences and the fingerprint
count where yahoo_volume >= massive_volume * 5 (prior-session cumulative).
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from attribution import now_et
from sources import get_source


def _key(row) -> tuple:
    return (str(row["side"]).upper(), float(row["strike"]), str(row["expiry"])[:10])


def _finite(v) -> float | None:
    try:
        f = float(v)
        if f != f or math.isinf(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def compare(ticker: str, *, max_dte: int = 45) -> int:
    yahoo = get_source("yahoo")
    massive = get_source("massive")
    print(f"asof ET: {now_et().isoformat()}")
    print(f"ticker={ticker} max_dte={max_dte}")
    print(f"yahoo.volume_is_session_scoped={yahoo.volume_is_session_scoped}")
    print(f"massive.volume_is_session_scoped={massive.volume_is_session_scoped}")

    ydf = yahoo.fetch_chain(ticker, max_dte=max_dte)
    mdf = massive.fetch_chain(ticker, max_dte=max_dte)
    ymap = {_key(r): r for _, r in ydf.iterrows()}
    mmap = {_key(r): r for _, r in mdf.iterrows()}
    keys = sorted(set(ymap) | set(mmap))

    both = 0
    yahoo_only = 0
    massive_only = 0
    fingerprint = 0  # yahoo_vol >= massive_vol * 5
    bid_ask_mismatch = 0
    iv_diffs: list[float] = []
    delta_y_only = 0
    delta_m_only = 0

    print("\nside strike expiry | y_vol m_vol | y_ba m_ba | y_iv m_iv | y_d m_d")
    for k in keys:
        y = ymap.get(k)
        m = mmap.get(k)
        if y is not None and m is None:
            yahoo_only += 1
            continue
        if m is not None and y is None:
            massive_only += 1
            continue
        both += 1
        yv = _finite(y["volume"]) or 0.0
        mv = _finite(m["volume"]) or 0.0
        if mv > 0 and yv >= mv * 5:
            fingerprint += 1
        y_bid, y_ask = _finite(y["bid"]), _finite(y["ask"])
        m_bid, m_ask = _finite(m["bid"]), _finite(m["ask"])
        y_has_ba = y_bid is not None and y_ask is not None and y_bid > 0 and y_ask > 0
        m_has_ba = m_bid is not None and m_ask is not None and m_bid > 0 and m_ask > 0
        if y_has_ba != m_has_ba:
            bid_ask_mismatch += 1
        y_iv, m_iv = _finite(y["iv"]), _finite(m["iv"])
        if y_iv is not None and m_iv is not None:
            iv_diffs.append(abs(y_iv - m_iv))
        y_d, m_d = _finite(y["delta"]), _finite(m["delta"])
        if y_d is not None and m_d is None:
            delta_y_only += 1
        if m_d is not None and y_d is None:
            delta_m_only += 1
        if fingerprint <= 15 and mv > 0 and yv >= mv * 5:
            print(
                f"{k[0]:4} {k[1]:7.1f} {k[2]} | "
                f"{yv:8.0f} {mv:8.0f} | "
                f"{int(y_has_ba)}/{int(m_has_ba)} | "
                f"{y_iv} {m_iv} | {y_d} {m_d}"
            )

    print("\n=== summary ===")
    print(f"contracts both sources : {both}")
    print(f"yahoo only             : {yahoo_only}")
    print(f"massive only           : {massive_only}")
    print(f"yahoo_vol >= 5x massive: {fingerprint}  << prior-session fingerprint")
    print(f"bid/ask presence mismatch: {bid_ask_mismatch}")
    print(f"delta only on yahoo    : {delta_y_only}")
    print(f"delta only on massive  : {delta_m_only}")
    if iv_diffs:
        print(
            f"iv |diff| mean/median  : "
            f"{sum(iv_diffs)/len(iv_diffs):.4f} / "
            f"{sorted(iv_diffs)[len(iv_diffs)//2]:.4f}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("ticker", nargs="?", default="AAPL")
    p.add_argument("--max-dte", type=int, default=45)
    args = p.parse_args(argv)
    try:
        return compare(args.ticker.upper(), max_dte=args.max_dte)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
