#!/usr/bin/env python3
"""
AAPL Options Scanner — Volume-First Framework
Ali's trading system | June 2026
"""

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import sys
import json
import os
import warnings
warnings.filterwarnings("ignore")

TICKER = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"

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

# ── LOAD PREVIOUS REPORT ──────────────────────────────────────────────────────
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

    # ── Spot price change ──
    spot_chg = curr_spot - prev_spot
    spot_pct  = (spot_chg / prev_spot * 100) if prev_spot else 0
    arrow = "▲" if spot_chg >= 0 else "▼"
    color = bull if spot_chg >= 0 else bear
    prev_ts = prev.get("timestamp", "")[:16].replace("T", " ")
    changes.append(color(f"  {arrow} SPOT  ${prev_spot:.2f} → ${curr_spot:.2f}  ({spot_chg:+.2f}, {spot_pct:+.1f}%)  since {prev_ts}"))

    # ── Direction / P/C ──
    prev_pc = prev.get("volume", {}).get("pc_ratio", None)
    if prev_pc is not None:
        pc_chg = curr_pc - prev_pc
        arrow2 = "▲" if pc_chg >= 0 else "▼"
        c2 = bull if pc_chg >= 0 else bear
        changes.append(c2(f"  {arrow2} P/C Ratio  {prev_pc:.2f} → {curr_pc:.2f}  ({pc_chg:+.3f})"))

    # ── Timeframe RSI shifts ──
    prev_tfs = prev.get("timeframes", {})
    rsi_shifts = []
    for tf in ["5M","10M","15M","1H","4H","1D"]:
        if tf in prev_tfs and tf in curr_tf:
            old_r = prev_tfs[tf]["rsi"]
            new_r = curr_tf[tf]["rsi"]
            diff  = new_r - old_r
            if abs(diff) >= 3:  # only show meaningful changes
                arrow3 = "▲" if diff > 0 else "▼"
                c3 = bull if diff > 0 else bear
                rsi_shifts.append(c3(f"{tf}:{old_r}→{new_r}({diff:+.1f})"))
    if rsi_shifts:
        changes.append(f"  RSI shifts  " + "  ".join(rsi_shifts))

    # ── Magnet shift alert — uses filtered magnets same as SIGNAL section ──
    prev_top_call = prev.get("volume", {}).get("top_calls", [{}])[0]
    prev_top_put  = prev.get("volume", {}).get("top_puts",  [{}])[0]

    puts_f  = proximity_filter(curr_puts,  curr_spot)
    calls_f = proximity_filter(curr_calls, curr_spot)
    curr_call_mag = calls_f.iloc[0] if not calls_f.empty else curr_calls[curr_calls["volume"]>0].iloc[0]
    curr_put_mag  = puts_f.iloc[0]  if not puts_f.empty  else curr_puts[curr_puts["volume"]>0].iloc[0]

    if prev_top_call.get("strike") and prev_top_call["strike"] != curr_call_mag["strike"]:
        changes.append(bull(f"  ▲ CALL MAGNET shifted  ${prev_top_call['strike']:.1f} → ${curr_call_mag['strike']:.1f}  ← STRIKE CHANGE"))
    if prev_top_put.get("strike") and prev_top_put["strike"] != curr_put_mag["strike"]:
        changes.append(bear(f"  ▼ PUT  MAGNET shifted  ${prev_top_put['strike']:.1f} → ${curr_put_mag['strike']:.1f}  ← STRIKE CHANGE"))

    # ── IV Expansion / Crush Alert ──
    # Only track if the magnet strike hasn't changed between runs
    if prev_top_call.get("strike") == curr_call_mag["strike"] and "impliedVolatility" in prev_top_call:
        old_iv_c = prev_top_call["impliedVolatility"] * 100
        new_iv_c = curr_call_mag["impliedVolatility"] * 100
        iv_diff_c = new_iv_c - old_iv_c
        if abs(iv_diff_c) >= 1.0:
            color = bull if iv_diff_c > 0 else bear
            changes.append(color(f"  {'▲' if iv_diff_c > 0 else '▼'} CALL MAGNET IV  {old_iv_c:.1f}% → {new_iv_c:.1f}%  ({iv_diff_c:+.1f}%)  {'expanding ⚠️' if iv_diff_c > 0 else 'crushing ⚠️'}"))

    if prev_top_put.get("strike") == curr_put_mag["strike"] and "impliedVolatility" in prev_top_put:
        old_iv_p = prev_top_put["impliedVolatility"] * 100
        new_iv_p = curr_put_mag["impliedVolatility"] * 100
        iv_diff_p = new_iv_p - old_iv_p
        if abs(iv_diff_p) >= 1.0:
            color = bear if iv_diff_p > 0 else bull
            changes.append(color(f"  {'▲' if iv_diff_p > 0 else '▼'} PUT  MAGNET IV  {old_iv_p:.1f}% → {new_iv_p:.1f}%  ({iv_diff_p:+.1f}%)  {'expanding ⚠️' if iv_diff_p > 0 else 'crushing ⚠️'}"))

    return changes

# ── INDICATORS ────────────────────────────────────────────────────────────────
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

# ── OPENING RANGE ─────────────────────────────────────────────────────────────
def opening_range(df5m, df15m, spot):
    """Calculate 5-min and 15-min opening range from today's first candles."""
    from datetime import date
    result = {}

    for label, df, minutes in [("5M", df5m, 1), ("15M", df15m, 3)]:
        try:
            # filter to today only
            today = date.today()
            day_df = df[df.index.date == today].copy()

            if day_df.empty:
                result[label] = None
                continue

            # market open = first candle
            market_open = day_df.index[0]
            open_price  = round(float(day_df["Open"].iloc[0]), 2)

            # grab first N candles (1 for 5M = 5min range, 3 for 15M = 15min range)
            or_df = day_df.iloc[:minutes]
            or_high = round(float(or_df["High"].max()), 2)
            or_low  = round(float(or_df["Low"].min()), 2)
            or_range = round(or_high - or_low, 2)
            or_pct   = round((or_range / open_price) * 100, 2)

            # current position vs OR
            if spot > or_high:
                bias = "BULLISH BREAKOUT"
                bias_dir = "bull"
            elif spot < or_low:
                bias = "BEARISH BREAKDOWN"
                bias_dir = "bear"
            else:
                bias = "INSIDE RANGE"
                bias_dir = "neutral"

            result[label] = {
                "open": open_price,
                "high": or_high,
                "low": or_low,
                "range": or_range,
                "range_pct": or_pct,
                "bias": bias,
                "bias_dir": bias_dir,
                "candles": minutes,
                "open_time": market_open.strftime("%H:%M"),
            }
        except Exception as e:
            result[label] = None

    return result

# ── FETCH ─────────────────────────────────────────────────────────────────────
def fetch_data():
    t = yf.Ticker(TICKER)

    def clean(df):
        df.index = df.index.tz_localize(None) if df.index.tz else df.index
        return df

    df5m  = clean(t.history(period="5d",  interval="5m"))
    df10m = clean(df5m.resample("10min").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna())
    df15m = clean(t.history(period="5d",  interval="15m"))
    df1h  = clean(t.history(period="30d", interval="1h"))
    df4h  = clean(df1h.resample("4h").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna())
    dfd   = clean(t.history(period="6mo", interval="1d"))

    frames = {"5M": df5m, "10M": df10m, "15M": df15m, "1H": df1h, "4H": df4h, "1D": dfd}
    spot   = round(float(dfd["Close"].iloc[-1]), 2)

    # ALL expiries — no filter
    all_calls, all_puts = [], []
    for expiry in t.options:
        chain = t.option_chain(expiry)
        for df, lst in [(chain.calls, all_calls), (chain.puts, all_puts)]:
            tmp = df[["strike","lastPrice","volume","openInterest","impliedVolatility"]].copy()
            tmp["volume"] = tmp["volume"].fillna(0)
            tmp["openInterest"] = tmp["openInterest"].fillna(0)
            tmp["expiry"] = expiry
            # days to expiry
            tmp["dte"] = (datetime.strptime(expiry,"%Y-%m-%d").date() - datetime.today().date()).days
            lst.append(tmp)

    calls_all = pd.concat(all_calls).sort_values("volume", ascending=False).reset_index(drop=True)
    puts_all  = pd.concat(all_puts).sort_values("volume", ascending=False).reset_index(drop=True)

    return frames, spot, calls_all, puts_all

# ── TECHNICAL ─────────────────────────────────────────────────────────────────
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
    Keeps full display table intact — only used for magnet selection.
    Buffer scale:
        0 DTE  → 2.5%   (same-day realistic moves only)
        1-2    → 4.0%   (overnight gap range)
        3-7    → 6.0%   (weekly swing range)
        8-30   → 10.0%  (monthly range)
        30+    → 15.0%  (LEAPS / hedge range)
    """
    def buf(dte):
        if dte == 0:   return 0.025
        if dte <= 2:   return 0.040
        if dte <= 7:   return 0.060
        if dte <= 30:  return 0.100
        return 0.150

    mask = options_df.apply(
        lambda r: abs(r["strike"] - spot) / spot <= buf(int(r["dte"])), axis=1
    )
    filtered = options_df[mask & (options_df["volume"] > 0)]
    # also require Vol/OI >= 1.0 to exclude pure stale positioning
    filtered = filtered[
        filtered.apply(lambda r: r["openInterest"] <= 0 or
                       r["volume"] / r["openInterest"] >= 1.0, axis=1)
    ]
    return filtered


def vol_oi_lbl(volume, oi):
    """Return Vol/OI ratio with conviction label and color."""
    if oi <= 0:
        return warn(f"{'n/a':>6}")
    ratio = volume / oi
    if ratio >= 5.0:
        label = bull(f"{ratio:>5.1f}x 🔥")   # explosive new money
    elif ratio >= 2.0:
        label = bull(f"{ratio:>5.1f}x ★")    # strong fresh conviction
    elif ratio >= 1.0:
        label = warn(f"{ratio:>5.1f}x ~")    # mixed new/existing
    else:
        label = dim(f"{ratio:>5.1f}x  ")     # mostly old positioning
    return label

# ── REPORT ────────────────────────────────────────────────────────────────────
def print_report(spot, tf_data, calls_all, puts_all, or_data=None, changes=None, prev_vol=None):
    W = 68
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    # totals
    total_cv = int(calls_all["volume"].sum())
    total_pv = int(puts_all["volume"].sum())
    pc_ratio = round(total_pv / total_cv, 2) if total_cv > 0 else 0
    direction, bv, brv = direction_score(tf_data, pc_ratio)

    print()
    print(f"{C.BOLD}{C.BG_DARK}{'─'*W}{C.RESET}")
    print(f"{C.BOLD}{C.BG_DARK}  📊  {TICKER} OPTIONS SCANNER  │  {now}{C.RESET}")
    print(f"{C.BOLD}{C.BG_DARK}{'─'*W}{C.RESET}")
    # ── CHANGES vs previous run ───────────────────────────────────────────────
    if changes:
        print(hdr("┌─ CHANGES vs LAST RUN ───────────────────────────────────────┐"))
        for line in changes:
            print(line)
        print()

    print(f"  {bold('SPOT')}  ${spot:.2f}    {bold('P/C Ratio')}  ", end="")
    if pc_ratio >= 1.3:   print(bear(f"{pc_ratio}  ← BEARISH SKEW"))
    elif pc_ratio <= 0.7: print(bull(f"{pc_ratio}  ← BULLISH SKEW"))
    else:                 print(warn(f"{pc_ratio}  ← NEUTRAL"))

    dir_lbl = bull(f"▲ {direction}") if direction=="BULLISH" else bear(f"▼ {direction}") if direction=="BEARISH" else warn(f"─ {direction}")
    print(f"  {bold('DIRECTION')}  {dir_lbl}   (bull signals:{bv} | bear signals:{brv})")
    print()

    # ── Multi-TF ──────────────────────────────────────────────────────────────
    print(hdr("┌─ MULTI-TIMEFRAME ───────────────────────────────────────────┐"))
    print(f"  {'TF':<5}  {'RSI':<24}  {'MACD':<30}  {'VolSpike':<10}  {'Support':<9}  Resist")
    print("  " + "─"*(W-2))
    for tf in ["5M","10M","15M","1H","4H","1D"]:
        d = tf_data.get(tf)
        if not d: continue
        vs_str = bull(f"{d['vs']}×") if d['vs'] >= 1.5 else dim(f"{d['vs']}×")
        print(f"  {bold(tf):<14}  {rsi_lbl(d['rsi']):<44}  {macd_lbl(d['hist']):<50}  {vs_str:<20}  ${d['support']:<9}  ${d['resist']}")
    print()

    # ── VOLUME LEADERBOARD ────────────────────────────────────────────────────
    prev_put_vol  = prev_vol.get("puts",  {}) if prev_vol else {}
    prev_call_vol = prev_vol.get("calls", {}) if prev_vol else {}

    print(hdr("┌─ PUT VOLUME  (all expiries, ranked) ────────────────────────┐"))
    print(f"  {'STRIKE':<10}  {'EXPIRY':<12}  {'DTE':<5}  {'PRICE':>7}  {'VOLUME':>10}  {'CHANGE':>14}  {'VOL/OI':>10}  {'OI':>8}")
    print("  " + "─"*(W-2))
    top_puts = puts_all[puts_all["volume"] > 0].head(10)
    for i, (_, r) in enumerate(top_puts.iterrows()):
        magnet   = "  ← MAGNET" if i == 0 else ""
        vol_s    = bear(f"{int(r['volume']):>10,}")
        strike_s = bold(f"${r['strike']:.1f}") if i == 0 else f"${r['strike']:.1f}"
        price_s  = f"${r['lastPrice']:.2f}" if r['lastPrice'] > 0 else dim("  n/a")
        voi_s    = vol_oi_lbl(r["volume"], r["openInterest"])
        key      = (r["strike"], r["expiry"])
        if prev_put_vol and key in prev_put_vol:
            chg = int(r["volume"]) - int(prev_put_vol[key])
            if chg > 0:
                delta_s = bear(f"+{chg:>8,} ▲")
            elif chg < 0:
                delta_s = bull(f"{chg:>8,} ▼")
            else:
                delta_s = dim(f"{'—':>10}")
        elif prev_put_vol:
            delta_s = warn(f"{'NEW ★':>10}")
        else:
            delta_s = dim(f"{'':>10}")
        print(f"  {strike_s:<10}  {r['expiry']:<12}  {int(r['dte']):>3}d  {price_s:>7}  {vol_s}  {delta_s}  {voi_s}  {int(r['openInterest']):>8,}{bear(magnet) if i==0 else ''}")
    print()

    print(hdr("┌─ CALL VOLUME (all expiries, ranked) ────────────────────────┐"))
    print(f"  {'STRIKE':<10}  {'EXPIRY':<12}  {'DTE':<5}  {'PRICE':>7}  {'VOLUME':>10}  {'CHANGE':>14}  {'VOL/OI':>10}  {'OI':>8}")
    print("  " + "─"*(W-2))
    top_calls = calls_all[calls_all["volume"] > 0].head(10)
    for i, (_, r) in enumerate(top_calls.iterrows()):
        magnet   = "  ← MAGNET" if i == 0 else ""
        vol_s    = bull(f"{int(r['volume']):>10,}")
        strike_s = bold(f"${r['strike']:.1f}") if i == 0 else f"${r['strike']:.1f}"
        price_s  = f"${r['lastPrice']:.2f}" if r['lastPrice'] > 0 else dim("  n/a")
        voi_s    = vol_oi_lbl(r["volume"], r["openInterest"])
        key      = (r["strike"], r["expiry"])
        if prev_call_vol and key in prev_call_vol:
            chg = int(r["volume"]) - int(prev_call_vol[key])
            if chg > 0:
                delta_s = bull(f"+{chg:>8,} ▲")
            elif chg < 0:
                delta_s = bear(f"{chg:>8,} ▼")
            else:
                delta_s = dim(f"{'—':>10}")
        elif prev_call_vol:
            delta_s = warn(f"{'NEW ★':>10}")
        else:
            delta_s = dim(f"{'':>10}")
        print(f"  {strike_s:<10}  {r['expiry']:<12}  {int(r['dte']):>3}d  {price_s:>7}  {vol_s}  {delta_s}  {voi_s}  {int(r['openInterest']):>8,}{bull(magnet) if i==0 else ''}")
    print()

    # ── SIGNAL ────────────────────────────────────────────────────────────────
    print(hdr("┌─ VOLUME BY EXPIRY ──────────────────────────────────────────┐"))
    print(f"  {'EXPIRY':<12}  {'DTE':<5}  {'CALL VOL':>12}  {'PUT VOL':>12}  {'P/C':>6}  BIAS")
    print("  " + "─"*(W-2))

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
        if pc >= 1.2:    bias = bear("▼ BEARISH     ")
        elif pc >= 1.05: bias = bear("▼ MILD BEARISH")
        elif pc <= 0.80: bias = bull("▲ BULLISH     ")
        elif pc <= 0.95: bias = bull("▲ MILD BULLISH")
        else:            bias = warn("─ NEUTRAL     ")
        cv_s = bull(f"{cv:>12,}")
        pv_s = bear(f"{pv:>12,}")

        # flag expiries whose P/C diverges >0.15 from overall — notable
        divergence = abs(pc - overall_pc)
        flag = warn("  ◄ notable") if divergence >= 0.15 and (cv + pv) >= 5000 else ""
        if flag:
            notable_expiries.append((r["expiry"], dte, pc))

        print(f"  {r['expiry']:<12}  {dte:>3}d  {cv_s}  {pv_s}  {pc:>6.2f}  {bias}{flag}")
    print()

    # ── EXPIRY DRILL-DOWN — notable expiries only ──────────────────────────────
    if notable_expiries:
        print(hdr("┌─ EXPIRY DRILL-DOWN  (notable P/C divergence) ───────────────┐"))
        for expiry, dte, pc in notable_expiries:
            bias_word = "BEARISH" if pc >= 1.05 else "BULLISH"
            bias_color = bear if pc >= 1.05 else bull
            print(f"  {bold(expiry)}  ({dte}d)  P/C {pc:.2f}  {bias_color(f'← {bias_word} vs overall {overall_pc:.2f}')}")
            print(f"  {'SIDE':<5}  {'STRIKE':<9}  {'PRICE':>7}  {'VOLUME':>10}  {'VOL/OI':>10}  {'OI':>8}")
            print("  " + "─" * (W - 2))

            # top 5 calls for this expiry
            exp_calls = calls_all[(calls_all["expiry"] == expiry) & (calls_all["volume"] > 0)]\
                .sort_values("volume", ascending=False).head(5)
            for _, r in exp_calls.iterrows():
                voi_s = vol_oi_lbl(r["volume"], r["openInterest"])
                vol_v = int(r["volume"])
                print(f"  {bull('CALL'):<14}  ${r['strike']:<8.1f}  ${r['lastPrice']:>6.2f}  {bull(f'{vol_v:>10,}'):}  {voi_s}  {int(r['openInterest']):>8,}")

            # top 5 puts for this expiry
            exp_puts = puts_all[(puts_all["expiry"] == expiry) & (puts_all["volume"] > 0)]\
                .sort_values("volume", ascending=False).head(5)
            for _, r in exp_puts.iterrows():
                voi_s = vol_oi_lbl(r["volume"], r["openInterest"])
                vol_v = int(r["volume"])
                print(f"  {bear('PUT '):<14}  ${r['strike']:<8.1f}  ${r['lastPrice']:>6.2f}  {bear(f'{vol_v:>10,}'):}  {voi_s}  {int(r['openInterest']):>8,}")
            print()

    # ── Opening Range ─────────────────────────────────────────────────────────
    if or_data:
        print(hdr("┌─ OPENING RANGE BREAKOUT ────────────────────────────────────┐"))
        for label, minutes_label in [("5M", "9:30–9:35"), ("15M", "9:30–9:45")]:
            d = or_data.get(label)
            if not d:
                print(f"  {bold(label)} ({minutes_label})  {dim('No data — market may be closed')}")
                continue

            if d["bias_dir"] == "bull":
                bias_str = bull(f"▲ {d['bias']}")
            elif d["bias_dir"] == "bear":
                bias_str = bear(f"▼ {d['bias']}")
            else:
                bias_str = warn(f"─ {d['bias']} — wait for break")

            print(f"  {bold(label+' OR')} ({minutes_label})  Open: ${d['open']}  │  High: {bull(f"${d['high']}")}  │  Low: {bear(f"${d['low']}")}")
            print(f"         Range: ${d['range']} ({d['range_pct']}%)  │  Current: ${spot}  →  {bias_str}")
            print()
        print(f"  {dim('Break above OR High = call confirmation')}")
        print(f"  {dim('Break below OR Low  = put confirmation')}")
        print(f"  {dim('Inside range        = wait, no edge')}")
        print()

    print(hdr("┌─ SIGNAL ────────────────────────────────────────────────────┐"))

    # Filtered magnets — DTE-scaled proximity + Vol/OI >= 1.0
    puts_filtered  = proximity_filter(puts_all,  spot)
    calls_filtered = proximity_filter(calls_all, spot)

    pm_raw = puts_all[puts_all["volume"]  > 0].iloc[0]   # raw #1 for fallback
    cm_raw = calls_all[calls_all["volume"] > 0].iloc[0]

    pm = puts_filtered.iloc[0]  if not puts_filtered.empty  else pm_raw
    cm = calls_filtered.iloc[0] if not calls_filtered.empty else cm_raw

    # Flag if filter changed the magnet vs raw top
    pm_filtered = (not puts_filtered.empty)  and (pm["strike"] != pm_raw["strike"])
    cm_filtered = (not calls_filtered.empty) and (cm["strike"] != cm_raw["strike"])

    pm_note = warn("  ← filtered (raw was ${:.1f})".format(pm_raw["strike"])) if pm_filtered else ""
    cm_note = warn("  ← filtered (raw was ${:.1f})".format(cm_raw["strike"])) if cm_filtered else ""

    print(f"  {bear('▼ PUT  MAGNET')}  →  Strike ${pm['strike']:.1f}  │  Expiry {pm['expiry']} ({int(pm['dte'])}d)  │  Vol {int(pm['volume']):,}{pm_note}")
    print(f"  {bull('▲ CALL MAGNET')}  →  Strike ${cm['strike']:.1f}  │  Expiry {cm['expiry']} ({int(cm['dte'])}d)  │  Vol {int(cm['volume']):,}{cm_note}")
    print()

    if direction == "BEARISH":
        print(f"  {bear('▶ DIRECTION: PUT')}")
        print(f"  Volume target  : ${pm['strike']:.1f}  ({int(pm['dte'])} DTE on {pm['expiry']})")
        print(f"  Vol/OI         : {pm['volume']:.0f} / {pm['openInterest']:.0f}  = {pm['volume']/max(pm['openInterest'],1):.1f}x conviction")
        row = puts_all[(puts_all["strike"]==pm["strike"]) & (puts_all["expiry"]==pm["expiry"])]
        if not row.empty and row.iloc[0]["lastPrice"] > 0:
            prem = row.iloc[0]["lastPrice"]
            print(f"  Est. premium   : ${prem:.2f}  (×100 = ${prem*100:.0f})")
            print(f"  Stop loss      : ${prem*0.60:.2f}  (40% max loss rule)")
    elif direction == "BULLISH":
        print(f"  {bull('▶ DIRECTION: CALL')}")
        print(f"  Volume target  : ${cm['strike']:.1f}  ({int(cm['dte'])} DTE on {cm['expiry']})")
        print(f"  Vol/OI         : {cm['volume']:.0f} / {cm['openInterest']:.0f}  = {cm['volume']/max(cm['openInterest'],1):.1f}x conviction")
        row = calls_all[(calls_all["strike"]==cm["strike"]) & (calls_all["expiry"]==cm["expiry"])]
        if not row.empty and row.iloc[0]["lastPrice"] > 0:
            prem = row.iloc[0]["lastPrice"]
            print(f"  Est. premium   : ${prem:.2f}  (×100 = ${prem*100:.0f})")
            print(f"  Stop loss      : ${prem*0.60:.2f}  (40% max loss rule)")
    else:
        print(f"  {warn('─ NO CLEAR EDGE  —  volume mixed, wait for confirmation')}")

    print()
    print(f"  {dim('Volume = WHERE price is going. You decide WHEN and which expiry.')}")
    print(f"{C.BOLD}{C.BG_DARK}{'─'*W}{C.RESET}")
    print(f"{C.GRAY}  Yahoo Finance  │  Not financial advice.{C.RESET}\n")

# ── ARCHIVE ───────────────────────────────────────────────────────────────────
def save_archive(spot, tf_data, calls_all, puts_all):
    os.makedirs("archive", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"archive/{TICKER}_{ts}.json"

    payload = {
        "timestamp": datetime.now().isoformat(),
        "spot": spot,
        "timeframes": {},
        "volume": {
            "total_call_vol": int(calls_all["volume"].sum()),
            "total_put_vol":  int(puts_all["volume"].sum()),
            "pc_ratio": round(float(puts_all["volume"].sum() / calls_all["volume"].sum()), 3) if calls_all["volume"].sum() > 0 else 0,
            "top_puts":  puts_all[puts_all["volume"]>0].head(10)[
                ["strike","expiry","dte","lastPrice","volume","openInterest","impliedVolatility"]
            ].to_dict(orient="records"),
            "top_calls": calls_all[calls_all["volume"]>0].head(10)[
                ["strike","expiry","dte","lastPrice","volume","openInterest","impliedVolatility"]
            ].to_dict(orient="records"),
        }
    }

    for tf, d in tf_data.items():
        payload["timeframes"][tf] = {k: (float(v) if isinstance(v, (np.floating, np.integer)) else v) for k, v in d.items()}

    with open(fname, "w") as f:
        json.dump(payload, f, indent=2)

    return fname, fname.replace(".json", ".txt")

# ── MAIN ──────────────────────────────────────────────────────────────────────
def run():
    print(f"\n{C.CYAN}Fetching {TICKER} data...{C.RESET}", end="", flush=True)
    frames, spot, calls_all, puts_all = fetch_data()
    print(f"  spot=${spot}")
    print(f"{C.CYAN}Analyzing...{C.RESET}\n")
    tf_data = analyze_tf(frames)
    or_data  = opening_range(frames.get("5M", pd.DataFrame()), frames.get("15M", pd.DataFrame()), spot)

    # ── Load previous report and compute changes ──
    prev_result = load_previous_report()
    changes  = None
    prev_vol = None
    if prev_result:
        prev_data, prev_file = prev_result
        total_cv = int(calls_all["volume"].sum())
        total_pv = int(puts_all["volume"].sum())
        curr_pc  = round(total_pv / total_cv, 3) if total_cv > 0 else 0
        changes = diff_reports(prev_data, spot, tf_data, calls_all, puts_all, curr_pc)
        print(f"{C.GRAY}  Comparing to → {prev_file}{C.RESET}")
        # build (strike, expiry) → volume lookups for inline deltas
        prev_vol = {
            "calls": {(r["strike"], r["expiry"]): r["volume"]
                      for r in prev_data.get("volume", {}).get("top_calls", [])},
            "puts":  {(r["strike"], r["expiry"]): r["volume"]
                      for r in prev_data.get("volume", {}).get("top_puts",  [])},
        }

    fname_json, fname_txt = save_archive(spot, tf_data, calls_all, puts_all)

    # capture report as plain text
    import io
    buf = io.StringIO()
    import sys as _sys
    _old_stdout = _sys.stdout
    _sys.stdout = buf
    print_report(spot, tf_data, calls_all, puts_all, or_data, changes=changes, prev_vol=prev_vol)
    _sys.stdout = _old_stdout
    report_text = buf.getvalue()

    # save plain text report
    with open(fname_txt, "w") as f:
        f.write(strip_ansi(report_text))

    print(f"{C.GRAY}  Archived → {fname_json}{C.RESET}")
    print(f"{C.GRAY}  Report   → {fname_txt}{C.RESET}\n")
    print(report_text, end="")

if __name__ == "__main__":
    run()
