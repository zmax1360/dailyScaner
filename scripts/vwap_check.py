#!/usr/bin/env python3
"""Compare the scanner's VWAP with a regular-hours chart for one day.

    python scripts/vwap_check.py AAPL 2026-10-15

Prints Yahoo's first 5-minute bars of the day (look at the 09:30 volume), the VWAP the
scanner computes at a few times, and the same VWAP rebuilt from 1-minute bars. Compare
the 13:45 line with TradingView (RTH) at 13:45. Read-only: no writes, no orders.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

from sources import get_source  # noqa: E402
from volume_analysis import compute_intraday_vwap, fetch_intraday_vwap_df, \
    opening_bar_warning, regular_session_only  # noqa: E402


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "AAPL").upper()
    day = sys.argv[2] if len(sys.argv) > 2 else None
    src = get_source("yahoo")
    five = fetch_intraday_vwap_df(ticker, timeframe="5M", last_session_only=False, source=src)
    if five.empty:
        print("No 5-minute bars from Yahoo.")
        return
    dates = sorted({d for d in five.index.date})
    day = pd.Timestamp(day).date() if day else dates[-1]
    d5 = five[five.index.date == day]
    print(f"{ticker} {day} · 5-minute bars from Yahoo: {len(d5)}")
    print(d5.head(6)[["Open", "High", "Low", "Close", "Volume"]].to_string())
    print("Opening-bar check:", opening_bar_warning(d5) or "looks normal")

    one = src.fetch_history(ticker, interval="1m", period="7d")
    one = regular_session_only(one)
    d1 = one[one.index.date == day] if one is not None and not one.empty else pd.DataFrame()
    if not d1.empty:
        d1 = d1.assign(VWAP=compute_intraday_vwap(d1))
        print("\nFirst 1-minute bars:")
        print(d1.head(3)[["Close", "Volume"]].to_string())

    print("\ntime   scanner VWAP (5m)   VWAP from 1m bars")
    for hhmm in ("10:00", "11:00", "12:00", "13:00", "13:45", "15:55"):
        a = d5[d5.index.strftime("%H:%M") == hhmm]["VWAP"]
        b = d1[d1.index.strftime("%H:%M") == hhmm]["VWAP"] if not d1.empty else pd.Series()
        fa = f"{a.iloc[0]:.2f}" if len(a) else "-"
        fb = f"{b.iloc[0]:.2f}" if len(b) else "-"
        print(f"{hhmm}  {fa:>17}   {fb:>17}")
    print("\nCompare 13:45 with TradingView VWAP Session (RTH) at 13:45.")


if __name__ == "__main__":
    main()
