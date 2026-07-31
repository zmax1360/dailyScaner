#!/usr/bin/env python3
"""
AAPL Options Scanner - Volume-First Framework
Ali's trading system | June 2026
"""

import pandas as pd
import numpy as np
from datetime import datetime, time as dtime
import sys
import json
import os
import time
import warnings
import logging
warnings.filterwarnings("ignore")

from logging_config import setup_logging

from config import SCORING

log = logging.getLogger("dailyScaner")

def _parse_ticker() -> str:
    """Read ticker from argv[1], ignoring pytest/script paths (must be 1-5 alpha chars)."""
    for candidate in sys.argv[1:]:
        if candidate.startswith("-"):
            continue
        if candidate.isalpha() and 1 <= len(candidate) <= 5:
            return candidate.upper()
    return "AAPL"

def _parse_is_eod() -> bool:
    return "--eod" in sys.argv


def _parse_source_name() -> str | None:
    """Optional ``--source yahoo|massive|fixture`` override (CLI)."""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--source" and i + 1 < len(args):
            return str(args[i + 1]).strip().lower()
        if a.startswith("--source="):
            return str(a.split("=", 1)[1]).strip().lower()
    return None
TICKER = _parse_ticker()
IS_EOD = _parse_is_eod()

# Minimum open interest for a contract to be eligible as a magnet / signal.
# Below this, vol/OI "conviction" is a division-by-noise artifact.
MIN_OI_FOR_MAGNET = int(SCORING["min_oi_for_magnet"])


def _log_scan_attribution(
    *,
    ticker: str,
    spot: float,
    calls_all: pd.DataFrame,
    puts_all: pd.DataFrame,
    vol_curr: dict,
    vol_prev: dict | None,
    session: dict | None,
    run_kind: str = "intraday",
    eod_vol_lookup: dict | None = None,
    volume_is_session_scoped: bool = False,
    current_source: str = "yahoo",
    prev_archive_source: str | None = None,
    eod_archive_source: str | None = None,
) -> None:
    """
    Score + append attribution rows. Fail-soft: never abort a scan.
    Lives on the scan path (not Streamlit) so scheduler runs are logged.
    """
    try:
        from attribution import (
            build_control_rows,
            engine_sha,
            log_run,
            modal_flagged_expiry,
        )
        from best_value import build_best_value_df, resolve_biases_for_ticker

        daily_bias, market_state = resolve_biases_for_ticker(
            ticker, session or {}, spot,
        )
        news_bias = None
        try:
            from news_service import get_news_sentiment
            news_bias = (get_news_sentiment(ticker) or {}).get("news_bias")
        except Exception:
            pass

        bv_df = build_best_value_df(
            vol_curr,
            spot,
            vol_prev,
            daily_bias=daily_bias,
            market_state=market_state,
            news_bias=news_bias,
            eod_vol_lookup=eod_vol_lookup,
            volume_is_session_scoped=volume_is_session_scoped,
            current_source=current_source,
            prev_archive_source=prev_archive_source,
            eod_archive_source=eod_archive_source,
        )

        chain = pd.concat(
            [
                calls_all.assign(side="CALL"),
                puts_all.assign(side="PUT"),
            ],
            ignore_index=True,
        )
        exp = modal_flagged_expiry(bv_df)
        if not exp and "expiry" in chain.columns and not chain.empty:
            # No scored rows  still log ATM control on nearest chain expiry
            exp = str(chain["expiry"].astype(str).mode().iloc[0])
        ctrl = (
            build_control_rows(chain, spot, exp)
            if exp
            else build_control_rows(pd.DataFrame(), spot, "")
        )

        run_id = log_run(
            ticker=ticker,
            scored_df=bv_df,
            cfg=SCORING,
            spot=spot,
            daily_bias=daily_bias,
            market_state=market_state,
            news_bias=news_bias,
            control_rows=ctrl,
            engine_sha_val=engine_sha(),
            run_kind=run_kind,
        )
        if (run_kind or "").lower() == "eod":
            log.info(
                f"{C.GRAY}  Attribution - run_id={run_id[:8]}... "
                f"run_kind=eod (flags skipped){C.RESET}"
            )
        else:
            n = int(bv_df["Value_Score"].notna().sum()) if not bv_df.empty else 0
            log.info(
                f"{C.GRAY}  Attribution - run_id={run_id[:8]}... "
                f"scored={n} ctrl={len(ctrl)}{C.RESET}"
            )
    except Exception as exc:
        log.warning(f"{C.YELLOW}  Attribution log failed (scan continues): {exc}{C.RESET}")
        try:
            from attribution import alert_attribution_failure
            alert_attribution_failure(
                f"ticker={ticker} spot={spot}\n{type(exc).__name__}: {exc}"
            )
        except Exception:
            pass

def strip_ansi(text):
    import re
    return re.sub(r'\[[0-9;]*m', '', text)

class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; GREEN = "\033[92m"
    RED = "\033[91m"; YELLOW = "\033[93m"; CYAN = "\033[96m"
    WHITE = "\033[97m"; GRAY = "\033[90m"; BG_DARK = "\033[40m"

def hdr(t):  return f"{C.BOLD}{C.CYAN}{t}{C.RESET}"
def bull(t): return f"{C.GREEN}{t}{C.RESET}"
def bear(t): return f"{C.RED}{t}{C.RESET}"
def warn(t): return f"{C.YELLOW}{t}{C.RESET}"
def dim(t):  return f"{C.GRAY}{t}{C.RESET}"
def bold(t): return f"{C.BOLD}{C.WHITE}{t}{C.RESET}"

# ?? LOAD PREVIOUS REPORT ??????????????????????????????????????????????????????
def load_previous_report():
    """Load the most recent archived JSON for this ticker, if it exists."""
    archive_dir = "archive"
    if not os.path.exists(archive_dir):
        return None
    files = sorted([
        f for f in os.listdir(archive_dir)
        if f.startswith(TICKER + "_") and f.endswith(".json")
    ])
    if not files:
        return None
    latest = os.path.join(archive_dir, files[-1])
    try:
        with open(latest) as f:
            return json.load(f), latest
    except Exception:
        return None

def diff_reports(prev, curr_spot, curr_tf, curr_calls, curr_puts, curr_pc):
    """Compare previous snapshot to current and return list of change strings."""
    changes = []
    prev_spot = prev.get("spot", 0)

    # ?? Spot price change ??
    spot_chg = curr_spot - prev_spot
    spot_pct  = (spot_chg / prev_spot * 100) if prev_spot else 0
    arrow = "?" if spot_chg >= 0 else "?"
    color = bull if spot_chg >= 0 else bear
    prev_ts = prev.get("timestamp", "")[:16].replace("T", " ")
    changes.append(color(f"  {arrow} SPOT  ${prev_spot:.2f} ? ${curr_spot:.2f}  ({spot_chg:+.2f}, {spot_pct:+.1f}%)  since {prev_ts}"))

    # ?? Direction / P/C ??
    prev_pc = prev.get("volume", {}).get("pc_ratio", None)
    if prev_pc is not None:
        pc_chg = curr_pc - prev_pc
        arrow2 = "?" if pc_chg >= 0 else "?"
        c2 = bull if pc_chg >= 0 else bear
        changes.append(c2(f"  {arrow2} P/C Ratio  {prev_pc:.2f} ? {curr_pc:.2f}  ({pc_chg:+.3f})"))

    # ?? Timeframe RSI shifts ??
    prev_tfs = prev.get("timeframes", {})
    rsi_shifts = []
    for tf in ["5M","10M","15M","45M","1H","4H","1D"]:
        if tf in prev_tfs and tf in curr_tf:
            old_r = prev_tfs[tf]["rsi"]
            new_r = curr_tf[tf]["rsi"]
            diff  = new_r - old_r
            if abs(diff) >= 3:  # only show meaningful changes
                arrow3 = "?" if diff > 0 else "?"
                c3 = bull if diff > 0 else bear
                rsi_shifts.append(c3(f"{tf}:{old_r}?{new_r}({diff:+.1f})"))
    if rsi_shifts:
        changes.append(f"  RSI shifts  " + "  ".join(rsi_shifts))

    # ?? Magnet shift alert - QUALIFIED magnets only, like-with-like ??
    # BUGFIX 2026-07-16b: previously compared prev raw volume leader
    # (top_calls[0], often 0DTE or a far LEAP) to the current filtered
    # magnet ? phantom "STRIKE CHANGE" every run, and a diff line that
    # contradicted the SIGNAL section's "no qualified contract".
    # Older archives lack "signal_magnets"; skip the comparison then
    # rather than fall back to raw rows.
    prev_mags = prev.get("signal_magnets")

    puts_f  = proximity_filter(curr_puts,  curr_spot)
    calls_f = proximity_filter(curr_calls, curr_spot)
    curr_call_mag = calls_f.iloc[0] if not calls_f.empty else None
    curr_put_mag  = puts_f.iloc[0]  if not puts_f.empty  else None

    if prev_mags is not None:
        for side, prev_m, curr_m, fmt, arrow in [
            ("CALL", prev_mags.get("call"), curr_call_mag, bull, "?"),
            ("PUT ", prev_mags.get("put"),  curr_put_mag,  bear, "?"),
        ]:
            if prev_m is not None and curr_m is None:
                changes.append(warn(f"  {arrow} {side} MAGNET  ${prev_m['strike']:.1f} ? none qualified"))
            elif prev_m is None and curr_m is not None:
                changes.append(fmt(f"  {arrow} {side} MAGNET  none ? ${curr_m['strike']:.1f}  ? NEW"))
            elif prev_m is not None and curr_m is not None:
                if prev_m["strike"] != curr_m["strike"] or prev_m["expiry"] != str(curr_m["expiry"]):
                    changes.append(fmt(f"  {arrow} {side} MAGNET shifted  ${prev_m['strike']:.1f} ({prev_m['expiry']}) ? ${curr_m['strike']:.1f} ({curr_m['expiry']})  ? STRIKE CHANGE"))
                elif "impliedVolatility" in prev_m:
                    # IV expansion/crush - same contract across runs only
                    old_iv = prev_m["impliedVolatility"] * 100
                    new_iv = float(curr_m["impliedVolatility"]) * 100
                    iv_d = new_iv - old_iv
                    if abs(iv_d) >= 1.0:
                        color = (bull if iv_d > 0 else bear) if side == "CALL" else (bear if iv_d > 0 else bull)
                        changes.append(color(f"  {'?' if iv_d > 0 else '?'} {side} MAGNET IV  {old_iv:.1f}% ? {new_iv:.1f}%  ({iv_d:+.1f}%)  {'expanding ??' if iv_d > 0 else 'crushing ??'}"))

    return changes

# ?? INDICATORS ????????????????????????????????????????????????????????????????
def rsi(s, p=14):
    d = s.diff()
    rs = d.clip(lower=0).rolling(p).mean() / (-d.clip(upper=0)).rolling(p).mean()
    return round(float((100 - 100/(1+rs)).iloc[-1]), 1)

def macd(s):
    m = s.ewm(span=12,adjust=False).mean() - s.ewm(span=26,adjust=False).mean()
    sig = m.ewm(span=9,adjust=False).mean()
    h = m - sig
    return round(float(m.iloc[-1]),4), round(float(sig.iloc[-1]),4), round(float(h.iloc[-1]),4)

def vol_spike(df):
    avg = df["Volume"].rolling(20).mean().iloc[-1]
    return round(df["Volume"].iloc[-1] / avg, 2) if avg > 0 else 0

def sr(df, n=20):
    return round(float(df["Low"].rolling(n).min().iloc[-1]),2), round(float(df["High"].rolling(n).max().iloc[-1]),2)

# ?? OPENING RANGE ?????????????????????????????????????????????????????????????
# BUGFIX 2026-07-16: the 15M OR was previously computed from the first 3 candles
# of the 15-MINUTE dataframe ? a 9:30-10:15 window (45 min) mislabeled "9:30-9:45",
# and the still-forming 3rd bar made OR-high track live price until 10:15.
# Regression evidence: 2026-07-15 logs show 15M OR high 321.82?323.76?324.98
# across 10:01/10:10/10:14 runs, printing INSIDE RANGE during a real breakout.
# Both windows are now built ONLY from completed 5-minute bars inside the window.

MARKET_TZ = "America/New_York"

def _now_et():
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(MARKET_TZ))

def market_is_open(now_et):
    """US regular session, ET. Weekdays 9:30-16:00. (Holidays not handled -
    a holiday just yields a MARKET CLOSED-quality stale report, which the
    diff header makes obvious.)"""
    return now_et.weekday() < 5 and dtime(9, 30) <= now_et.time() < dtime(16, 0)

def opening_range(df5m, df15m, spot, now_et=None):
    """
    5-min OR  = the single 9:30-9:35 ET bar.
    15-min OR = the three 9:30-9:45 ET 5-minute bars.
    Values are immutable once the window closes. While the window is still
    open, status is OR FORMING and no breakout is ever claimed.
    (df15m is accepted for call-site compatibility but no longer used.)
    """
    from datetime import time as dtime
    if now_et is None:
        now_et = _now_et()
    today_et = now_et.date()
    result = {}

    windows = [
        ("5M",  1, dtime(9, 35)),   # bars needed, window close (ET)
        ("15M", 3, dtime(9, 45)),
    ]

    for label, bars_needed, window_end in windows:
        try:
            day_df = df5m[df5m.index.date == today_et]
            if day_df.empty:
                result[label] = None
                continue

            market_open = day_df.index[0]
            open_price  = round(float(day_df["Open"].iloc[0]), 2)

            # Only bars whose START time is strictly inside the window.
            # A 5m bar starting at 9:45 belongs to the session, not the OR.
            window_end_dt = datetime.combine(today_et, window_end)
            in_window = day_df[[ts.replace(tzinfo=None) < window_end_dt
                                for ts in day_df.index]]
            or_df = in_window.iloc[:bars_needed]

            still_forming = (now_et.time() < window_end
                             and now_et.date() == today_et)

            or_high  = round(float(or_df["High"].max()), 2)
            or_low   = round(float(or_df["Low"].min()), 2)
            or_range = round(or_high - or_low, 2)
            or_pct   = round((or_range / open_price) * 100, 2)

            if still_forming or len(or_df) < bars_needed:
                bias, bias_dir = f"OR FORMING (closes {window_end.strftime('%H:%M')})", "forming"
            elif spot > or_high:
                bias, bias_dir = "BULLISH BREAKOUT", "bull"
            elif spot < or_low:
                bias, bias_dir = "BEARISH BREAKDOWN", "bear"
            else:
                bias, bias_dir = "INSIDE RANGE", "neutral"

            result[label] = {
                "open": open_price,
                "high": or_high,
                "low": or_low,
                "range": or_range,
                "range_pct": or_pct,
                "bias": bias,
                "bias_dir": bias_dir,
                "candles": bars_needed,
                "open_time": market_open.strftime("%H:%M"),
            }
        except Exception:
            result[label] = None

    return result

# ?? FETCH ?????????????????????????????????????????????????????????????????????
def _clean_history(df):
    # Guard: empty DataFrame has RangeIndex, not DatetimeIndex
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    if hasattr(df.index, "tz"):
        df = df.copy()
        df.index = df.index.tz_localize(None) if df.index.tz else df.index
    return df


def _legacy_option_leg(chain: pd.DataFrame, side: str) -> pd.DataFrame:
    """Map CHAIN_COLUMNS rows back to the scanner's legacy Yahoo-shaped leg."""
    sub = chain.loc[chain["side"] == side].copy()
    if sub.empty:
        return pd.DataFrame(
            columns=[
                "strike", "lastPrice", "volume", "openInterest",
                "impliedVolatility", "bid", "ask", "expiry", "dte",
            ]
        )
    out = pd.DataFrame({
        "strike": sub["strike"].astype(float),
        "lastPrice": sub["last"].astype(float),
        "volume": sub["volume"].fillna(0).astype(float),
        "openInterest": sub["openInterest"].fillna(0).astype(float),
        "impliedVolatility": sub["iv"],
        # Preserve NaN bid/ask -- never coerce missing quotes to 0 (zero-width lie).
        "bid": pd.to_numeric(sub["bid"], errors="coerce"),
        "ask": pd.to_numeric(sub["ask"], errors="coerce"),
        "expiry": sub["expiry"].astype(str),
        "dte": sub["dte"],
    })
    return out.sort_values("volume", ascending=False).reset_index(drop=True)


def fetch_data(source=None):
    """
    Fetch history + chain via MarketDataSource.

    ``source`` is constructed at the run() entry point when omitted  never at
    import time.
    """
    from sources import MarketDataSource, get_source

    if source is None:
        source = get_source(str(SCORING.get("market_data_source", "yahoo")))
    if not isinstance(source, MarketDataSource):
        raise TypeError(f"source must implement MarketDataSource, got {type(source)!r}")

    df5m = _clean_history(
        source.fetch_history(TICKER, interval="5m", period="5d")
    )

    # Early exit: if 5-minute data is empty the ticker is an index or delisted
    if df5m.empty:
        raise ValueError(
            f"{TICKER} returned no intraday price data. "
            "Indices (^VIX, ^SPX) and tickers without equity options are not "
            "supported - the scanner requires a standard options chain."
        )

    df10m = _clean_history(
        df5m.resample("10min")
        .agg({"Open": "first", "High": "max", "Low": "min",
              "Close": "last", "Volume": "sum"})
        .dropna()
    )
    df15m = _clean_history(
        source.fetch_history(TICKER, interval="15m", period="5d")
    )
    df45m = _clean_history(
        df5m.resample("45min")
        .agg({"Open": "first", "High": "max", "Low": "min",
              "Close": "last", "Volume": "sum"})
        .dropna()
    )
    df1h = _clean_history(
        source.fetch_history(TICKER, interval="1h", period="30d")
    )
    df4h = _clean_history(
        df1h.resample("4h")
        .agg({"Open": "first", "High": "max", "Low": "min",
              "Close": "last", "Volume": "sum"})
        .dropna()
    )
    dfd = _clean_history(
        source.fetch_history(TICKER, interval="1d", period="6mo")
    )

    frames = {
        "5M": df5m, "10M": df10m, "15M": df15m, "45M": df45m,
        "1H": df1h, "4H": df4h, "1D": dfd,
    }
    spot = source.fetch_spot(TICKER)
    if spot is None or spot <= 0:
        if dfd.empty or "Close" not in dfd.columns:
            raise ValueError(f"{TICKER} returned no usable spot price.")
        spot = round(float(dfd["Close"].iloc[-1]), 2)
    else:
        spot = round(float(spot), 2)

    # Wide max_dte preserves prior "ALL expiries" behaviour until Step 6 caps.
    chain = source.fetch_chain(TICKER, max_dte=3650)
    calls_all = _legacy_option_leg(chain, "CALL")
    puts_all = _legacy_option_leg(chain, "PUT")
    if calls_all.empty or puts_all.empty:
        raise ValueError(
            f"{TICKER} has no options chain data. "
            "Indices (^VIX, ^SPX) are not supported - use an ETF with options "
            "instead (e.g. SPY, QQQ, UVXY for volatility exposure)."
        )

    return frames, spot, calls_all, puts_all

# ?? TECHNICAL ?????????????????????????????????????????????????????????????????
def analyze_tf(frames):
    out = {}
    for tf, df in frames.items():
        if len(df) < 15: continue
        c = df["Close"]
        m, s, h = macd(c)
        lo, hi = sr(df)
        out[tf] = {
            "rsi": rsi(c), "macd": m, "sig": s, "hist": h,
            "vs": vol_spike(df), "support": lo, "resist": hi,
            "price": round(float(c.iloc[-1]),2)
        }
    return out

def rsi_lbl(v):
    if v <= 35: return bull(f"OVERSOLD ({v})")
    if v >= 65: return bear(f"OVERBOUGHT ({v})")
    if v < 45:  return bear(f"BEARISH ({v})")
    if v > 55:  return bull(f"BULLISH ({v})")
    return warn(f"NEUTRAL ({v})")

def macd_lbl(h):
    if h > 0: return bull(f"BULLISH [hist +{h}]")
    return bear(f"BEARISH [hist {h}]")

def direction_score(tf_data, pc_ratio):
    bull_v = bear_v = 0
    rsi_avg = np.mean([tf_data[tf]["rsi"] for tf in tf_data])
    if rsi_avg <= 40: bull_v += 1
    elif rsi_avg >= 60: bear_v += 1
    else: (bull_v if rsi_avg > 50 else bear_v).__class__  # neutral

    for tf in ["4H","1D"]:
        if tf in tf_data:
            (bull_v if tf_data[tf]["hist"] > 0 else bear_v).__class__
            if tf_data[tf]["hist"] > 0: bull_v += 1
            else: bear_v += 1

    if pc_ratio >= 1.3: bear_v += 1
    elif pc_ratio <= 0.7: bull_v += 1

    if bull_v > bear_v: return "BULLISH", bull_v, bear_v
    if bear_v > bull_v: return "BEARISH", bull_v, bear_v
    return "NEUTRAL", bull_v, bear_v

def proximity_filter(options_df, spot):
    """
    Return rows within a DTE-scaled strike buffer around spot.
    Keeps full display table intact - only used for magnet selection.
    Buffer scale:
        0 DTE  ? 2.5%   (same-day realistic moves only)
        1-2    ? 4.0%   (overnight gap range)
        3-7    ? 6.0%   (weekly swing range)
        8-30   ? 10.0%  (monthly range)
        30+    ? 15.0%  (LEAPS / hedge range)
    """
    def buf(dte):
        if dte <= 2:   return 0.040
        if dte <= 7:   return 0.060
        if dte <= 30:  return 0.100
        return 0.150

    # BUGFIX 2026-07-16:
    #  (a) DTE-0 contracts are excluded from magnet/signal selection entirely.
    #      Their OI is yesterday's stale number, so vol/OI is meaningless
    #      (observed: vol 72,603 / OI 1 = "72,603x conviction") and 0DTE
    #      always won the ranking, turning the SIGNAL section into a
    #      same-day-lottery recommender.
    #  (b) OI <= 0 previously AUTO-PASSED the conviction filter. Now a
    #      minimum OI floor is required - no denominator, no conviction.
    #  0DTE rows remain visible in the display tables; they just can never
    #  be a magnet or a recommendation.
    df = options_df[(options_df["dte"] >= 1) &
                    (options_df["volume"] > 0) &
                    (options_df["openInterest"] >= MIN_OI_FOR_MAGNET)]
    mask = df.apply(
        lambda r: abs(r["strike"] - spot) / spot <= buf(int(r["dte"])), axis=1
    )
    filtered = df[mask]
    # require Vol/OI >= 1.0 to exclude pure stale positioning
    filtered = filtered[filtered["volume"] / filtered["openInterest"] >= 1.0]
    return filtered


def vol_oi_lbl(volume, oi):
    """Return Vol/OI ratio with conviction label and color."""
    if oi <= 0:
        return warn(f"{'n/a':>6}")
    ratio = volume / oi
    if ratio >= 5.0:
        label = bull(f"{ratio:>5.1f}x ??")   # explosive new money
    elif ratio >= 2.0:
        label = bull(f"{ratio:>5.1f}x ?")    # strong fresh conviction
    elif ratio >= 1.0:
        label = warn(f"{ratio:>5.1f}x ~")    # mixed new/existing
    else:
        label = dim(f"{ratio:>5.1f}x  ")     # mostly old positioning
    return label

# ?? REPORT ????????????????????????????????????????????????????????????????????
def print_report(spot, tf_data, calls_all, puts_all, or_data=None, changes=None, prev_vol=None):
    W = 68
    now = _now_et().strftime("%Y-%m-%d %H:%M")

    # totals - same rounding as save_archive / direction0 (3 dp) so
    # terminal direction matches the archived direction the dashboard shows.
    total_cv = int(calls_all["volume"].sum())
    total_pv = int(puts_all["volume"].sum())
    pc_ratio = round(total_pv / total_cv, 3) if total_cv > 0 else 0
    direction, bv, brv = direction_score(tf_data, pc_ratio)

    log.info("")
    log.info(f"{C.BOLD}{C.BG_DARK}{'?'*W}{C.RESET}")
    log.info(f"{C.BOLD}{C.BG_DARK}  ??  {TICKER} OPTIONS SCANNER  ?  {now}{C.RESET}")
    log.info(f"{C.BOLD}{C.BG_DARK}{'?'*W}{C.RESET}")
    # ?? CHANGES vs previous run ???????????????????????????????????????????????
    if changes:
        log.info(hdr("?? CHANGES vs LAST RUN ????????????????????????????????????????"))
        for line in changes:
            log.info(line)
        log.info("")

    log.info(f"  {bold('SPOT')}  ${spot:.2f}    {bold('P/C Ratio')}  ")
    if pc_ratio >= 1.3:   log.info(bear(f"{pc_ratio}  ? BEARISH SKEW"))
    elif pc_ratio <= 0.7: log.info(bull(f"{pc_ratio}  ? BULLISH SKEW"))
    else:                 log.info(warn(f"{pc_ratio}  ? NEUTRAL"))

    dir_lbl = bull(f"? {direction}") if direction=="BULLISH" else bear(f"? {direction}") if direction=="BEARISH" else warn(f"? {direction}")
    log.info(f"  {bold('DIRECTION')}  {dir_lbl}   (bull signals:{bv} | bear signals:{brv})")
    log.info("")

    # ?? Multi-TF ??????????????????????????????????????????????????????????????
    log.info(hdr("?? MULTI-TIMEFRAME ????????????????????????????????????????????"))
    log.info(f"  {'TF':<5}  {'RSI':<24}  {'MACD':<30}  {'VolSpike':<10}  {'Support':<9}  Resist")
    log.info("  " + "?"*(W-2))
    for tf in ["5M","10M","15M","45M","1H","4H","1D"]:
        d = tf_data.get(tf)
        if not d: continue
        vs_str = bull(f"{d['vs']}x") if d['vs'] >= 1.5 else dim(f"{d['vs']}x")
        log.info(f"  {bold(tf):<14}  {rsi_lbl(d['rsi']):<44}  {macd_lbl(d['hist']):<50}  {vs_str:<20}  ${d['support']:<9}  ${d['resist']}")
    log.info("")

    # ?? VOLUME LEADERBOARD ????????????????????????????????????????????????????
    prev_put_vol  = prev_vol.get("puts",  {}) if prev_vol else {}
    prev_call_vol = prev_vol.get("calls", {}) if prev_vol else {}

    log.info(hdr("?? PUT VOLUME  (all expiries, ranked) ?????????????????????????"))
    log.info(f"  {'STRIKE':<10}  {'EXPIRY':<12}  {'DTE':<5}  {'PRICE':>7}  {'VOLUME':>10}  {'CHANGE':>14}  {'VOL/OI':>10}  {'OI':>8}")
    log.info("  " + "?"*(W-2))
    top_puts = puts_all[puts_all["volume"] > 0].head(10)
    for i, (_, r) in enumerate(top_puts.iterrows()):
        magnet   = "  ? TOP VOL" if i == 0 else ""
        vol_s    = bear(f"{int(r['volume']):>10,}")
        strike_s = bold(f"${r['strike']:.1f}") if i == 0 else f"${r['strike']:.1f}"
        price_s  = f"${r['lastPrice']:.2f}" if r['lastPrice'] > 0 else dim("  n/a")
        voi_s    = vol_oi_lbl(r["volume"], r["openInterest"])
        key      = (r["strike"], r["expiry"])
        if prev_put_vol and key in prev_put_vol:
            chg = int(r["volume"]) - int(prev_put_vol[key])
            if chg > 0:
                delta_s = bear(f"+{chg:>8,} ?")
            elif chg < 0:
                delta_s = bull(f"{chg:>8,} ?")
            else:
                delta_s = dim(f"{'-':>10}")
        elif prev_put_vol:
            delta_s = warn(f"{'NEW ?':>10}")
        else:
            delta_s = dim(f"{'':>10}")
        log.info(f"  {strike_s:<10}  {r['expiry']:<12}  {int(r['dte']):>3}d  {price_s:>7}  {vol_s}  {delta_s}  {voi_s}  {int(r['openInterest']):>8,}{bear(magnet) if i==0 else ''}")
    log.info("")

    log.info(hdr("?? CALL VOLUME (all expiries, ranked) ?????????????????????????"))
    log.info(f"  {'STRIKE':<10}  {'EXPIRY':<12}  {'DTE':<5}  {'PRICE':>7}  {'VOLUME':>10}  {'CHANGE':>14}  {'VOL/OI':>10}  {'OI':>8}")
    log.info("  " + "?"*(W-2))
    top_calls = calls_all[calls_all["volume"] > 0].head(10)
    for i, (_, r) in enumerate(top_calls.iterrows()):
        magnet   = "  ? TOP VOL" if i == 0 else ""
        vol_s    = bull(f"{int(r['volume']):>10,}")
        strike_s = bold(f"${r['strike']:.1f}") if i == 0 else f"${r['strike']:.1f}"
        price_s  = f"${r['lastPrice']:.2f}" if r['lastPrice'] > 0 else dim("  n/a")
        voi_s    = vol_oi_lbl(r["volume"], r["openInterest"])
        key      = (r["strike"], r["expiry"])
        if prev_call_vol and key in prev_call_vol:
            chg = int(r["volume"]) - int(prev_call_vol[key])
            if chg > 0:
                delta_s = bull(f"+{chg:>8,} ?")
            elif chg < 0:
                delta_s = bear(f"{chg:>8,} ?")
            else:
                delta_s = dim(f"{'-':>10}")
        elif prev_call_vol:
            delta_s = warn(f"{'NEW ?':>10}")
        else:
            delta_s = dim(f"{'':>10}")
        log.info(f"  {strike_s:<10}  {r['expiry']:<12}  {int(r['dte']):>3}d  {price_s:>7}  {vol_s}  {delta_s}  {voi_s}  {int(r['openInterest']):>8,}{bull(magnet) if i==0 else ''}")
    log.info("")

    # ?? SIGNAL ????????????????????????????????????????????????????????????????
    log.info(hdr("?? VOLUME BY EXPIRY ???????????????????????????????????????????"))
    log.info(f"  {'EXPIRY':<12}  {'DTE':<5}  {'CALL VOL':>12}  {'PUT VOL':>12}  {'P/C':>6}  BIAS")
    log.info("  " + "?"*(W-2))

    # aggregate by expiry across all strikes
    call_by_exp = calls_all.groupby(["expiry","dte"])["volume"].sum().reset_index()
    put_by_exp  = puts_all.groupby(["expiry","dte"])["volume"].sum().reset_index()
    exp_summary = call_by_exp.merge(put_by_exp, on=["expiry","dte"], suffixes=("_c","_p"))
    exp_summary["pc"] = (exp_summary["volume_p"] / exp_summary["volume_c"].replace(0, np.nan)).round(2)
    exp_summary = exp_summary.sort_values("dte").head(8)  # 8 expiries covers ~1 month

    # compute overall P/C to identify divergent expiries for drill-down
    overall_pc = round(total_pv / total_cv, 2) if total_cv > 0 else 0

    notable_expiries = []  # collect for drill-down below

    for _, r in exp_summary.iterrows():
        pc  = r["pc"] if not np.isnan(r["pc"]) else 0
        cv  = int(r["volume_c"])
        pv  = int(r["volume_p"])
        dte = int(r["dte"])
        if pc >= 1.2:    bias = bear("? BEARISH     ")
        elif pc >= 1.05: bias = bear("? MILD BEARISH")
        elif pc <= 0.80: bias = bull("? BULLISH     ")
        elif pc <= 0.95: bias = bull("? MILD BULLISH")
        else:            bias = warn("? NEUTRAL     ")
        cv_s = bull(f"{cv:>12,}")
        pv_s = bear(f"{pv:>12,}")

        # flag expiries whose P/C diverges >0.15 from overall - notable
        divergence = abs(pc - overall_pc)
        flag = warn("  ? notable") if divergence >= 0.15 and (cv + pv) >= 5000 else ""
        if flag:
            notable_expiries.append((r["expiry"], dte, pc))

        log.info(f"  {r['expiry']:<12}  {dte:>3}d  {cv_s}  {pv_s}  {pc:>6.2f}  {bias}{flag}")
    log.info("")

    # ?? EXPIRY DRILL-DOWN - notable expiries only ??????????????????????????????
    if notable_expiries:
        log.info(hdr("?? EXPIRY DRILL-DOWN  (notable P/C divergence) ????????????????"))
        for expiry, dte, pc in notable_expiries:
            bias_word = "BEARISH" if pc >= 1.05 else "BULLISH"
            bias_color = bear if pc >= 1.05 else bull
            log.info(f"  {bold(expiry)}  ({dte}d)  P/C {pc:.2f}  {bias_color(f'? {bias_word} vs overall {overall_pc:.2f}')}")
            log.info(f"  {'SIDE':<5}  {'STRIKE':<9}  {'PRICE':>7}  {'VOLUME':>10}  {'VOL/OI':>10}  {'OI':>8}")
            log.info("  " + "?" * (W - 2))

            # top 5 calls for this expiry
            exp_calls = calls_all[(calls_all["expiry"] == expiry) & (calls_all["volume"] > 0)]\
                .sort_values("volume", ascending=False).head(5)
            for _, r in exp_calls.iterrows():
                voi_s = vol_oi_lbl(r["volume"], r["openInterest"])
                vol_v = int(r["volume"])
                log.info(f"  {bull('CALL'):<14}  ${r['strike']:<8.1f}  ${r['lastPrice']:>6.2f}  {bull(f'{vol_v:>10,}'):}  {voi_s}  {int(r['openInterest']):>8,}")

            # top 5 puts for this expiry
            exp_puts = puts_all[(puts_all["expiry"] == expiry) & (puts_all["volume"] > 0)]\
                .sort_values("volume", ascending=False).head(5)
            for _, r in exp_puts.iterrows():
                voi_s = vol_oi_lbl(r["volume"], r["openInterest"])
                vol_v = int(r["volume"])
                log.info(f"  {bear('PUT '):<14}  ${r['strike']:<8.1f}  ${r['lastPrice']:>6.2f}  {bear(f'{vol_v:>10,}'):}  {voi_s}  {int(r['openInterest']):>8,}")
            log.info("")

    # ?? Opening Range ?????????????????????????????????????????????????????????
    if or_data:
        log.info(hdr("?? OPENING RANGE BREAKOUT ?????????????????????????????????????"))
        for label, minutes_label in [("5M", "9:30-9:35"), ("15M", "9:30-9:45")]:
            d = or_data.get(label)
            if not d:
                log.info(f"  {bold(label)} ({minutes_label})  {dim('No data - market may be closed')}")
                continue

            if d["bias_dir"] == "bull":
                bias_str = bull(f"? {d['bias']}")
            elif d["bias_dir"] == "bear":
                bias_str = bear(f"? {d['bias']}")
            elif d["bias_dir"] == "forming":
                bias_str = warn(f"? {d['bias']} - no breakout call yet")
            else:
                bias_str = warn(f"? {d['bias']} - wait for break")

            log.info(f"  {bold(label+' OR')} ({minutes_label})  Open: ${d['open']}  ?  High: {bull(f"${d['high']}")}  ?  Low: {bear(f"${d['low']}")}")
            log.info(f"         Range: ${d['range']} ({d['range_pct']}%)  ?  Current: ${spot}  ?  {bias_str}")
            log.info("")
        log.info(f"  {dim('Break above OR High = call confirmation')}")
        log.info(f"  {dim('Break below OR Low  = put confirmation')}")
        log.info(f"  {dim('Inside range        = wait, no edge')}")
        log.info("")

    log.info(hdr("?? SIGNAL ?????????????????????????????????????????????????????"))

    # ?? Actionability guard (BUGFIX 2026-07-16): the 16:32 run on
    # 2026-07-15 recommended a 0DTE contract that had expired at 16:00.
    market_open_now = market_is_open(_now_et())

    if not market_open_now:
        log.info(f"  {warn('? MARKET CLOSED - data is end-of-day, no actionable signal')}")
        log.info(f"  {dim('Magnets and flow below are a record of today, not a recommendation.')}")
        log.info("")
        log.info(f"  {dim('Flow shows where money went. Whether it was right is a separate question.')}")
        log.info(f"{C.BOLD}{C.BG_DARK}{'?'*W}{C.RESET}")
        log.info(f"{C.GRAY}  Yahoo Finance  ?  Not financial advice.{C.RESET}\n")
        return

    # Qualified magnets only - DTE >= 1, OI >= MIN_OI_FOR_MAGNET, Vol/OI >= 1.
    # NO raw fallback: the raw volume leader is almost always 0DTE, which
    # would silently reintroduce the lottery-recommender bug.
    puts_filtered  = proximity_filter(puts_all,  spot)
    calls_filtered = proximity_filter(calls_all, spot)

    pm = puts_filtered.iloc[0]  if not puts_filtered.empty  else None
    cm = calls_filtered.iloc[0] if not calls_filtered.empty else None

    for side, m, fmt in [("? PUT  MAGNET", pm, bear), ("? CALL MAGNET", cm, bull)]:
        if m is None:
            log.info(f"  {fmt(side)}  ?  {dim('no qualified contract (DTE?1, OI?%d, vol/OI?1)' % MIN_OI_FOR_MAGNET)}")
        else:
            log.info(f"  {fmt(side)}  ?  Strike ${m['strike']:.1f}  ?  Expiry {m['expiry']} ({int(m['dte'])}d)  ?  Vol {int(m['volume']):,}")
    log.info("")

    target = cm if direction == "BULLISH" else (pm if direction == "BEARISH" else None)

    if direction not in ("BULLISH", "BEARISH"):
        log.info(f"  {warn('? NO CLEAR EDGE  -  volume mixed, wait for confirmation')}")
    elif target is None:
        log.info(f"  {warn('? DIRECTION %s but no qualified contract - no trade' % direction)}")
    else:
        # Same-day expiries are already excluded (DTE >= 1); guard anyway.
        if int(target["dte"]) < 1:
            log.info(f"  {warn('? Nearest qualified contract expires today - no trade')}")
        else:
            side_lbl = bull('? DIRECTION: CALL') if direction == "BULLISH" else bear('? DIRECTION: PUT')
            log.info(f"  {side_lbl}")
            log.info(f"  Volume target  : ${target['strike']:.1f}  ({int(target['dte'])} DTE on {target['expiry']})")
            log.info(f"  Vol/OI         : {target['volume']:.0f} / {target['openInterest']:.0f}  = {target['volume']/max(target['openInterest'],1):.1f}x conviction")

            bid_raw, ask_raw = target.get("bid"), target.get("ask")
            try:
                bid = float(bid_raw) if bid_raw is not None else float("nan")
            except (TypeError, ValueError):
                bid = float("nan")
            try:
                ask = float(ask_raw) if ask_raw is not None else float("nan")
            except (TypeError, ValueError):
                ask = float("nan")
            # Mid only from real NBBO; NaN bid/ask is not a zero-width spread.
            if bid == bid and ask == ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
            else:
                mid = float(target["lastPrice"])
            if mid > 0:
                log.info(f"  Est. premium   : ${mid:.2f}  (x100 = ${mid*100:.0f})")
                # Spread-aware stop (BUGFIX 2026-07-16): a % stop on a
                # wide-spread contract market-sells into whatever bid exists.
                if bid == bid and ask == ask and bid > 0 and ask > 0 and (ask - bid) / mid > 0.20:
                    log.info(f"  Stop loss      : {warn('spread %.0f%% of mid - too wide for a reliable stop; size as full-loss risk' % ((ask-bid)/mid*100))}")
                else:
                    log.info(f"  Stop loss      : ${mid*0.60:.2f}  (40% max loss rule)")

    log.info("")
    log.info(f"  {dim('Flow shows where money went. Whether it was right is a separate question.')}")
    log.info(f"{C.BOLD}{C.BG_DARK}{'?'*W}{C.RESET}")
    log.info(f"{C.GRAY}  Yahoo Finance  ?  Not financial advice.{C.RESET}\n")

# ?? ARCHIVE ???????????????????????????????????????????????????????????????????
def save_archive(spot, tf_data, calls_all, puts_all, or_data=None, direction=None, session=None, *, is_eod=False, settlement_converged=None, source_name: str = "yahoo", quote_source: str = "nbbo"):
    os.makedirs("archive", exist_ok=True)
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
    ts_aware = datetime.now(_ET)
    ts = ts_aware.strftime("%Y%m%d_%H%M%S")
    fname = f"archive/{TICKER}_{ts}.json"

    payload = {
        "timestamp": ts_aware.isoformat(),
        "source": str(source_name),
        "quote_source": str(quote_source),
        "is_eod": bool(is_eod),
        "settlement_converged": (
            None if settlement_converged is None else bool(settlement_converged)
        ),
        "spot": spot,
        "or_data": or_data,        # dashboard: OR band + breakout state per run
        "direction": direction,    # dashboard: scanner's direction verdict per run
        "session": session,        # dashboard: quote-strip fields (open, prev_close, high, low)
        "timeframes": {},
        # BUGFIX 2026-07-16b: persist the QUALIFIED magnets so the next
        # run's diff compares like-with-like. Previously the diff used
        # top_calls[0] (raw volume leader, usually 0DTE or a far LEAP)
        # against the current filtered magnet ? phantom STRIKE CHANGE
        # alerts on nearly every run.
        "signal_magnets": {
            side: (None if m is None else {
                "strike": float(m["strike"]),
                "expiry": str(m["expiry"]),
                "dte": int(m["dte"]),
                "volume": float(m["volume"]),
                "openInterest": float(m["openInterest"]),
                "impliedVolatility": float(m["impliedVolatility"]),
            })
            for side, m in [
                ("call", (lambda f: f.iloc[0] if not f.empty else None)(proximity_filter(calls_all, spot))),
                ("put",  (lambda f: f.iloc[0] if not f.empty else None)(proximity_filter(puts_all,  spot))),
            ]
        },
        "volume": {
            "total_call_vol": int(calls_all["volume"].sum()),
            "total_put_vol":  int(puts_all["volume"].sum()),
            "pc_ratio": round(float(puts_all["volume"].sum() / calls_all["volume"].sum()), 3) if calls_all["volume"].sum() > 0 else 0,
            "top_puts":  puts_all[puts_all["volume"]>0].head(30)[
                ["strike","expiry","dte","lastPrice","bid","ask","volume","openInterest","impliedVolatility"]
            ].to_dict(orient="records"),
            "top_calls": calls_all[calls_all["volume"]>0].head(30)[
                ["strike","expiry","dte","lastPrice","bid","ask","volume","openInterest","impliedVolatility"]
            ].to_dict(orient="records"),
        }
    }

    for tf, d in tf_data.items():
        payload["timeframes"][tf] = {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) for k, v in d.items()}

    with open(fname, "w") as f:
        json.dump(payload, f, indent=2)

    return fname, fname.replace(".json", ".txt")

# ?? MAIN ??????????????????????????????????????????????????????????????????????
def run(source=None):
    # Scan activity lands in logs/scheduler.log (subprocess of scheduler),
    # or the interactive console when run from a TTY.
    _proc = os.environ.get("OPTIONTRADING_PROCESS", "scheduler")
    setup_logging(_proc)
    from sources import get_source

    if source is None:
        source = get_source(str(SCORING.get("market_data_source", "yahoo")))

    settlement_converged = None
    if IS_EOD:
        from eod_settlement import VolumeSnapshot, await_volume_convergence
        gap_sec, max_attempts = 600.0, 3
        try:
            with open("scheduler_config.json") as _cfg_fh:
                _ecfg = json.load(_cfg_fh)
            gap_sec = float(_ecfg.get("eod_convergence_gap_sec", gap_sec))
            max_attempts = int(_ecfg.get("eod_convergence_max_attempts", max_attempts))
        except Exception:
            pass
        log.info(f"\n{C.CYAN}EOD mode -- converging settlement volumes for {TICKER}...{C.RESET}")
        last_pack = {}

        def _read_vols():
            frames, spot, calls_all, puts_all = fetch_data(source)
            snap = VolumeSnapshot(
                total_call_vol=int(calls_all["volume"].sum()),
                total_put_vol=int(puts_all["volume"].sum()),
            )
            last_pack["frames"] = frames
            last_pack["spot"] = spot
            last_pack["calls_all"] = calls_all
            last_pack["puts_all"] = puts_all
            last_pack["snap"] = snap
            log.info(
                f"{C.GRAY}  vol snapshot CALL={snap.total_call_vol:,} "
                f"PUT={snap.total_put_vol:,}{C.RESET}"
            )
            return snap

        converged, _last, _snaps = await_volume_convergence(
            _read_vols,
            gap_sec=gap_sec,
            max_attempts=max_attempts,
        )
        settlement_converged = bool(converged)
        frames = last_pack["frames"]
        spot = last_pack["spot"]
        calls_all = last_pack["calls_all"]
        puts_all = last_pack["puts_all"]
        log.info(
            f"{C.CYAN}EOD settlement_converged={settlement_converged} "
            f"spot=${spot}{C.RESET}\n"
        )
    else:
        log.info(f"\n{C.CYAN}Fetching {TICKER} data...{C.RESET}")
        frames, spot, calls_all, puts_all = fetch_data(source)
        log.info(f"  spot=${spot}")
    log.info(f"{C.CYAN}Analyzing...{C.RESET}\n")
    tf_data = analyze_tf(frames)
    or_data  = opening_range(frames.get("5M", pd.DataFrame()), frames.get("15M", pd.DataFrame()), spot)

    # ?? Load previous report and compute changes ??
    prev_result = load_previous_report()
    changes  = None
    prev_vol = None
    if prev_result:
        prev_data, prev_file = prev_result
        total_cv = int(calls_all["volume"].sum())
        total_pv = int(puts_all["volume"].sum())
        curr_pc  = round(total_pv / total_cv, 3) if total_cv > 0 else 0
        changes = diff_reports(prev_data, spot, tf_data, calls_all, puts_all, curr_pc)
        log.info(f"{C.GRAY}  Comparing to ? {prev_file}{C.RESET}")
        # build (strike, expiry) ? volume lookups for inline deltas
        prev_vol = {
            "calls": {(r["strike"], r["expiry"]): r["volume"]
                      for r in prev_data.get("volume", {}).get("top_calls", [])},
            "puts":  {(r["strike"], r["expiry"]): r["volume"]
                      for r in prev_data.get("volume", {}).get("top_puts",  [])},
        }

    total_cv0 = int(calls_all["volume"].sum())
    total_pv0 = int(puts_all["volume"].sum())
    pc0 = round(total_pv0 / total_cv0, 3) if total_cv0 > 0 else 0
    direction0, _, _ = direction_score(tf_data, pc0)

    # ?? Task A: chain-level session rollover guard ???????????????????????????
    import logging as _logging
    from chain_quality import (
        archive_source_name,
        chain_fails_quality_gate,
        chain_volume_rolled_over,
        eod_volume_lookup,
        find_prior_eod_archive,
        flag_stale_vs_eod,
        majority_stale_abort,
        rollover_detectors_active,
        should_apply_chain_rollover_check,
        stale_check_active,
    )
    from zoneinfo import ZoneInfo as _ZI
    _scan_log = _logging.getLogger("dailyScaner")
    _ET_now = datetime.now(_ZI("America/New_York"))
    _today_et = _ET_now.date()
    _session_scoped = bool(getattr(source, "volume_is_session_scoped", False))
    _curr_source = str(getattr(source, "name", "yahoo") or "yahoo")
    _provides_quotes = bool(getattr(source, "provides_quotes", True))
    _quote_source = "nbbo" if _provides_quotes else "daily_bar"
    _prev_archive_source = None
    if prev_result:
        _prev_archive_source = archive_source_name(prev_result[0])
    if not rollover_detectors_active(_session_scoped):
        log.info(
            f"{C.GRAY}  Rollover/stale-volume detectors DORMANT "
            f"(source={_curr_source} is session-scoped).{C.RESET}"
        )
    if prev_result and rollover_detectors_active(_session_scoped):
        _prev_data, _prev_file = prev_result
        _apply, _why = should_apply_chain_rollover_check(
            _prev_data, _curr_source, _today_et,
        )
        if not _apply and _why.startswith("source_mismatch"):
            _ps = archive_source_name(_prev_data)
            _msg = (
                f"previous archive written by source {_ps}, current source is "
                f"{_curr_source} -- rollover check skipped"
            )
            _scan_log.warning(_msg)
            log.warning(f"{C.YELLOW}WARN: {_msg}{C.RESET}")
        elif _apply:
            _pv = (_prev_data or {}).get("volume") or {}
            _pc = int(_pv.get("total_call_vol") or 0)
            _pp = int(_pv.get("total_put_vol") or 0)
            if chain_volume_rolled_over(_pc, _pp, total_cv0, total_pv0):
                log.info(
                    f"{C.YELLOW}ABORT: chain volume rollover detected "
                    f"(same ET session {_today_et}). "
                    f"prev call/put={_pc:,}/{_pp:,}  "
                    f"curr call/put={total_cv0:,}/{total_pv0:,}. "
                    f"No archive, no attribution.{C.RESET}"
                )
                return

    # Build a provisional volume block for the quality gate (top-30 shape)
    _vol_gate = {
        "top_calls": calls_all[calls_all["volume"] > 0].head(30)[
            ["strike", "expiry", "dte", "lastPrice", "bid", "ask",
             "volume", "openInterest", "impliedVolatility"]
        ].to_dict(orient="records"),
        "top_puts": puts_all[puts_all["volume"] > 0].head(30)[
            ["strike", "expiry", "dte", "lastPrice", "bid", "ask",
             "volume", "openInterest", "impliedVolatility"]
        ].to_dict(orient="records"),
        "total_call_vol": total_cv0,
        "total_put_vol": total_pv0,
    }
    _q_fail, _q_detail = chain_fails_quality_gate(
        _vol_gate, provides_quotes=_provides_quotes,
    )
    if _q_fail:
        _cs = _q_detail.get("calls") or {}
        log.info(
            f"{C.YELLOW}ABORT: chain quality gate failed "
            f"(unusable {_cs.get('unusable', 0)}/{_cs.get('total', 0)} "
            f"top calls, frac={_q_detail.get('frac_unusable', 0):.0%}; "
            f"zero_bid_ask={_cs.get('zero_bid_ask', 0)}, "
            f"low_iv={_cs.get('low_iv', 0)}). "
            f"No archive, no attribution.{C.RESET}"
        )
        return

    # ?? Stale volume vs prior EOD (CURSOR_STALE_VOLUME_FIX) ????????????????
    _eod_lookup = None
    _eod_archive_source = None
    if not rollover_detectors_active(_session_scoped):
        _eod_arch, _eod_reason = None, "dormant_session_scoped"
    else:
        _eod_arch, _eod_reason = find_prior_eod_archive(
            TICKER, "archive", now_et=_ET_now, required_source=_curr_source,
        )
    if _eod_arch is None:
        log.info(
            f"{C.YELLOW}WARN: EOD volume reference unavailable ({_eod_reason}); "
            f"stale-volume check skipped  decrease detector only.{C.RESET}"
        )
    elif not stale_check_active(_ET_now):
        log.info(
            f"{C.GRAY}  Stale-volume EOD check skipped (past cutoff ET).{C.RESET}"
        )
        _eod_lookup = eod_volume_lookup(_eod_arch)  # still pass through for attach after cutoff? no
        _eod_lookup = None
    else:
        _eod_lookup = eod_volume_lookup(_eod_arch)
        _eod_archive_source = archive_source_name(_eod_arch)
        _call_flags = flag_stale_vs_eod(
            _vol_gate.get("top_calls") or [], _eod_lookup, side="CALL"
        )
        _put_flags = flag_stale_vs_eod(
            _vol_gate.get("top_puts") or [], _eod_lookup, side="PUT"
        )
        _n_stale = sum(_call_flags) + sum(_put_flags)
        _n_tot = len(_call_flags) + len(_put_flags)
        log.info(
            f"{C.GRAY}  Stale-volume vs EOD: flagged {_n_stale}/{_n_tot} "
            f"top contracts (calls={sum(_call_flags)}, puts={sum(_put_flags)})."
            f"{C.RESET}"
        )
        if majority_stale_abort(_n_stale, _n_tot):
            log.info(
                f"{C.YELLOW}ABORT: majority stale volume "
                f"({_n_stale}/{_n_tot} > 50%). "
                f"No archive, no attribution.{C.RESET}"
            )
            return


    # -- Session block: open, prev_close, day_high, day_low ------------------
    # Derived from frames["1D"] - already fetched, no extra network call.
    session = None
    try:
        dfd = frames.get("1D")
        if dfd is not None and len(dfd) >= 2:
            from zoneinfo import ZoneInfo
            _ET = ZoneInfo("America/New_York")
            today_et = datetime.now(_ET).date()
            last_row  = dfd.iloc[-1]
            last_date = last_row.name
            # Normalise to a plain date for comparison
            if hasattr(last_date, "date"):
                last_date = last_date.date()
            else:
                last_date = pd.Timestamp(last_date).date()

            if last_date == today_et:
                # Today's bar exists: use its Open/High/Low; prev_close = row[-2]
                open_today  = float(last_row["Open"])
                day_high    = float(last_row["High"])
                day_low     = float(last_row["Low"])
                prev_close  = float(dfd.iloc[-2]["Close"])
            else:
                # Pre-market: today's bar not yet open; treat last row as prev close
                open_today  = None
                day_high    = None
                day_low     = None
                prev_close  = float(last_row["Close"])

            session = {
                "open":       open_today,
                "prev_close": prev_close,
                "day_high":   day_high,
                "day_low":    day_low,
            }
    except Exception:
        session = None
    # ------------------------------------------------------------------------

    fname_json, fname_txt = save_archive(
        spot, tf_data, calls_all, puts_all,
        or_data=or_data, direction=direction0,
        session=session,
        is_eod=IS_EOD,
        settlement_converged=settlement_converged if IS_EOD else None,
        source_name=_curr_source,
        quote_source=_quote_source,
    )

    # Attribution: every scored contract + ATM controls (fail-soft)
    vol_curr = {
        "top_calls": calls_all[calls_all["volume"] > 0].head(30)[
            ["strike", "expiry", "dte", "lastPrice", "bid", "ask",
             "volume", "openInterest", "impliedVolatility"]
        ].to_dict(orient="records"),
        "top_puts": puts_all[puts_all["volume"] > 0].head(30)[
            ["strike", "expiry", "dte", "lastPrice", "bid", "ask",
             "volume", "openInterest", "impliedVolatility"]
        ].to_dict(orient="records"),
    }
    vol_prev_bv = None
    if prev_result:
        vol_prev_bv = (prev_result[0] or {}).get("volume")
    _log_scan_attribution(
        ticker=TICKER,
        spot=spot,
        calls_all=calls_all,
        puts_all=puts_all,
        vol_curr=vol_curr,
        vol_prev=vol_prev_bv,
        session=session,
        run_kind="eod" if IS_EOD else "intraday",
        eod_vol_lookup=_eod_lookup,
        volume_is_session_scoped=_session_scoped,
        current_source=_curr_source,
        prev_archive_source=_prev_archive_source,
        eod_archive_source=_eod_archive_source,
    )

    # Capture report via a temporary logging handler (print_report uses log.*).
    import io
    _cap = io.StringIO()

    class _ReportCapture(logging.Handler):
        def emit(self, record):
            _cap.write(record.getMessage() + "\n")

    _rh = _ReportCapture()
    _prev_prop = log.propagate
    _prev_handlers = list(log.handlers)
    log.handlers = [_rh]
    log.propagate = False
    try:
        print_report(spot, tf_data, calls_all, puts_all, or_data, changes=changes, prev_vol=prev_vol)
    finally:
        log.handlers = _prev_handlers
        log.propagate = _prev_prop
    report_text = _cap.getvalue()

    # save plain text report
    with open(fname_txt, "w") as f:
        f.write(strip_ansi(report_text))

    log.info(f"{C.GRAY}  Archived ? {fname_json}{C.RESET}")
    log.info(f"{C.GRAY}  Report   ? {fname_txt}{C.RESET}\n")
    log.info(report_text)

if __name__ == "__main__":
    try:
        from sources import get_source
        from sources.massive import MassiveChainTruncatedError

        _src_name = _parse_source_name()
        _source = get_source(_src_name) if _src_name else None
        run(source=_source)
    except MassiveChainTruncatedError as e:
        log.error(f"{C.YELLOW}ABORT: {e}{C.RESET}")
        sys.exit(1)
    except ValueError as e:
        log.error(f"{C.RED}ERROR:{C.RESET} {e}")
        sys.exit(1)
