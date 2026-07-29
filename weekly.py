#!/usr/bin/env python3
"""
AAPL Weekly Scanner — Pre-Trade Checklist Framework
Run every Monday morning before any weekly/biweekly options trade.
Ali's trading system | June 2026
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import json, os, sys, warnings
import anthropic
from attribution import now_et
from spread_gate import evaluate_spread_gate

warnings.filterwarnings("ignore")

TICKER  = sys.argv[1].upper() if len(sys.argv) > 1 else "AAPL"
W            = 70    # report width
SPREAD_WIDTH = 10.0  # target width of the ATM bull call spread in points

# ── KNOWN EARNINGS (update manually each quarter) ─────────────────────────────
EARNINGS = {
    "AAPL": datetime(2026, 7, 30),
}

# ── COLOR HELPERS (shared with daily scanner) ─────────────────────────────────
import re
def strip_ansi(t): return re.sub(r'\[[0-9;]*m', '', t)

class C:
    RESET="\033[0m"; BOLD="\033[1m"; GREEN="\033[92m"
    RED="\033[91m";  YELLOW="\033[93m"; CYAN="\033[96m"
    WHITE="\033[97m"; GRAY="\033[90m"

def hdr(t):  return f"{C.BOLD}{C.CYAN}{t}{C.RESET}"
def bull(t): return f"{C.GREEN}{t}{C.RESET}"
def bear(t): return f"{C.RED}{t}{C.RESET}"
def warn(t): return f"{C.YELLOW}{t}{C.RESET}"
def dim(t):  return f"{C.GRAY}{t}{C.RESET}"
def bold(t): return f"{C.BOLD}{C.WHITE}{t}{C.RESET}"

# ── INDICATORS ────────────────────────────────────────────────────────────────
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).rolling(period).mean()
    loss  = (-delta.clip(upper=0)).rolling(period).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def macd(series, fast=12, slow=26, signal=9):
    m     = ema(series, fast) - ema(series, slow)
    sig   = ema(m, signal)
    return m, sig, m - sig

def atr(high, low, close, period=14):
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low  - close.shift()).abs()
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()

# ── DATA FETCHERS ─────────────────────────────────────────────────────────────
def _resolve_source(source=None):
    if source is not None:
        return source
    from config import SCORING
    from sources import get_source
    return get_source(str(SCORING.get("market_data_source", "yahoo")))


def _strip_tz(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df if df is not None else pd.DataFrame()
    out = df.copy()
    if hasattr(out.index, "tz") and out.index.tz is not None:
        out.index = out.index.tz_localize(None)
    return out


def fetch_weekly_data(ticker, source=None):
    """Fetch daily + weekly OHLCV, compute key indicators."""
    src = _resolve_source(source)
    daily = _strip_tz(src.fetch_history(ticker, interval="1d", period="3mo"))
    weekly = _strip_tz(src.fetch_history(ticker, interval="1wk", period="52wk"))
    return daily, weekly


def fetch_macro(source=None):
    """Fetch SPY, QQQ, VIX daily data."""
    src = _resolve_source(source)
    results = {}
    for sym in ["SPY", "QQQ", "^VIX"]:
        hist = _strip_tz(src.fetch_history(sym, interval="1d", period="3mo"))
        results[sym] = hist
    return results


def fetch_options_30dte(ticker, source=None):
    """Fetch options chain closest to 30 DTE for OI structure analysis."""
    src = _resolve_source(source)
    today = now_et().date()
    chain = src.fetch_chain(ticker, max_dte=60)
    if chain is None or chain.empty:
        raise ValueError(f"{ticker}: empty options chain from {src.name}")

    # Prefer expiry closest to 30 DTE among rows present
    expiries = sorted(set(str(e)[:10] for e in chain["expiry"].tolist()))
    if not expiries:
        raise ValueError(f"{ticker}: no expiries in chain")
    target = today + timedelta(days=30)
    best = min(
        expiries,
        key=lambda d: abs((datetime.strptime(d, "%Y-%m-%d").date() - target).days),
    )
    sub = chain[chain["expiry"].astype(str).str[:10] == best].copy()
    dte = (datetime.strptime(best, "%Y-%m-%d").date() - today).days

    def _leg(side: str) -> pd.DataFrame:
        leg = sub[sub["side"] == side].copy()
        return pd.DataFrame({
            "strike": leg["strike"].astype(float),
            "lastPrice": leg["last"].astype(float),
            "openInterest": leg["openInterest"].fillna(0).astype(float),
            "impliedVolatility": leg["iv"],
            "volume": leg["volume"].fillna(0).astype(float),
            "bid": leg["bid"].fillna(0).astype(float),
            "ask": leg["ask"].fillna(0).astype(float),
            "expiry": best,
            "dte": dte,
        })

    return _leg("CALL"), _leg("PUT"), best

# ── ANALYSIS FUNCTIONS ────────────────────────────────────────────────────────
def analyze_ticker(daily, weekly):
    """Compute EMAs, RSI, MACD, ATR on daily and weekly."""
    result = {}

    for label, df in [("daily", daily), ("weekly", weekly)]:
        close = df["Close"]
        high  = df["High"]
        low   = df["Low"]

        e14  = ema(close, 14)
        e28  = ema(close, 28)
        e50  = ema(close, 50)
        r14  = rsi(close, 14)
        m, s, h = macd(close)
        a14  = atr(high, low, close, 14)

        spot = float(close.iloc[-1])
        result[label] = {
            "spot":     spot,
            "ema14":    round(float(e14.iloc[-1]), 2),
            "ema28":    round(float(e28.iloc[-1]), 2),
            "ema50":    round(float(e50.iloc[-1]), 2),
            "rsi":      round(float(r14.iloc[-1]), 1),
            "macd_hist":round(float(h.iloc[-1]),   4),
            "atr":      round(float(a14.iloc[-1]),  2),
            # Support/Resist — recent swing low/high over last 20 bars
            "support":  round(float(low.rolling(20).min().iloc[-1]),  2),
            "resist":   round(float(high.rolling(20).max().iloc[-1]), 2),
            # 52-week high/low
            "high52":   round(float(high.rolling(252).max().iloc[-1]), 2),
            "low52":    round(float(low.rolling(252).min().iloc[-1]),  2),
        }

    return result

def analyze_macro(macro_data):
    """Analyze SPY, QQQ trend and VIX level."""
    result = {}
    for sym, df in macro_data.items():
        close = df["Close"]
        e20   = ema(close, 20)
        e50   = ema(close, 50)
        r14   = rsi(close, 14)
        spot  = float(close.iloc[-1])

        result[sym] = {
            "spot":  round(spot, 2),
            "ema20": round(float(e20.iloc[-1]), 2),
            "ema50": round(float(e50.iloc[-1]), 2),
            "rsi":   round(float(r14.iloc[-1]), 1),
            "above_ema20": spot > float(e20.iloc[-1]),
            "above_ema50": spot > float(e50.iloc[-1]),
            # 5-day return
            "ret5d": round((spot / float(close.iloc[-6]) - 1) * 100, 2) if len(close) >= 6 else 0,
        }

    return result

def analyze_oi_structure(calls, puts, spot):
    """Find key OI levels, skew, and max pain for 30 DTE chain."""
    calls = calls[calls["openInterest"] > 0].copy()
    puts  = puts[puts["openInterest"]  > 0].copy()

    # Max pain — strike where total dollar value of expiring options is minimized
    strikes = sorted(set(calls["strike"].tolist() + puts["strike"].tolist()))
    pain    = {}
    for s in strikes:
        call_pain = ((calls[calls["strike"] < s]["strike"].apply(lambda k: max(s - k, 0)) *
                      calls[calls["strike"] < s]["openInterest"]) * 100).sum()
        put_pain  = ((puts[puts["strike"]  > s]["strike"].apply(lambda k: max(k - s, 0)) *
                      puts[puts["strike"]  > s]["openInterest"]) * 100).sum()
        pain[s]   = call_pain + put_pain
    max_pain = min(pain, key=pain.get)

    # Top 5 OI strikes each side — near spot only (±10%)
    near_calls = calls[abs(calls["strike"] - spot) / spot <= 0.10]\
        .sort_values("openInterest", ascending=False).head(5)
    near_puts  = puts[abs(puts["strike"]  - spot) / spot <= 0.10]\
        .sort_values("openInterest", ascending=False).head(5)

    total_call_oi = int(calls["openInterest"].sum())
    total_put_oi  = int(puts["openInterest"].sum())
    pc_oi         = round(total_put_oi / total_call_oi, 3) if total_call_oi > 0 else 0

    # IV skew — compare ATM IV to 5% OTM put IV
    atm_call  = calls.iloc[(calls["strike"] - spot).abs().argsort()].iloc[0]
    otm_put   = puts[(puts["strike"] < spot * 0.95)].sort_values("strike", ascending=False)
    iv_skew   = None
    if not otm_put.empty:
        iv_skew = round((float(otm_put.iloc[0]["impliedVolatility"]) -
                         float(atm_call["impliedVolatility"])) * 100, 1)

    return {
        "max_pain":       max_pain,
        "pc_oi":          pc_oi,
        "total_call_oi":  total_call_oi,
        "total_put_oi":   total_put_oi,
        "iv_skew":        iv_skew,
        "top_call_oi":    near_calls[["strike","openInterest","impliedVolatility"]].to_dict("records"),
        "top_put_oi":     near_puts[["strike","openInterest","impliedVolatility"]].to_dict("records"),
    }

def earnings_check(ticker, today=None):
    """Check days to next earnings."""
    today   = today or datetime.today()
    earndt  = EARNINGS.get(ticker)
    if not earndt:
        return None, None
    days    = (earndt - today).days
    return earndt.strftime("%Y-%m-%d"), days

def checklist(ticker_data, macro, oi, earnings_days):
    """Run the 5-point pre-trade checklist. Returns score and details."""
    checks  = []
    daily   = ticker_data["daily"]
    vix     = macro.get("^VIX", {})
    spy     = macro.get("SPY",  {})
    qqq     = macro.get("QQQ",  {})

    # 1. Macro trend — SPY + QQQ both above EMA20
    macro_ok = spy.get("above_ema20", False) and qqq.get("above_ema20", False)
    checks.append((
        "Macro Trend (SPY+QQQ above EMA20)",
        macro_ok,
        f"SPY {'✓' if spy.get('above_ema20') else '✗'} EMA20  |  QQQ {'✓' if qqq.get('above_ema20') else '✗'} EMA20"
    ))

    # 2. VIX level — below 20 = cheap options, good for buying
    vix_spot = vix.get("spot", 99)
    vix_ok   = vix_spot < 20
    vix_note = f"VIX {vix_spot:.1f}  ({'cheap options ✓' if vix_ok else 'expensive options ✗'})"
    checks.append(("VIX < 20 (options affordability)", vix_ok, vix_note))

    # 3. AAPL daily trend — price above EMA14
    aapl_ok  = daily["spot"] > daily["ema14"]
    aapl_note = f"${daily['spot']:.2f} vs EMA14 ${daily['ema14']:.2f}  ({'above ✓' if aapl_ok else 'below ✗'})"
    checks.append(("AAPL above Daily EMA14", aapl_ok, aapl_note))

    # 4. Options skew — P/C OI ratio
    pc_ok   = oi["pc_oi"] < 0.80  # call-heavy = bullish institutional lean
    pc_note = f"30 DTE P/C OI {oi['pc_oi']:.2f}  ({'bullish skew ✓' if pc_ok else 'neutral/bearish ✗'})"
    checks.append(("30 DTE OI skew bullish (P/C < 0.80)", pc_ok, pc_note))

    # 5. Earnings distance — no earnings within 7 days
    earn_ok  = earnings_days is None or earnings_days > 7
    earn_note = f"{earnings_days}d to earnings  ({'safe ✓' if earn_ok else 'DANGER — too close ✗'})" \
                if earnings_days is not None else "No earnings date known"
    checks.append(("Earnings > 7 days away", earn_ok, earn_note))

    score = sum(1 for _, passed, _ in checks if passed)
    return score, checks

# ── SPREAD CANDIDATE SELECTION ────────────────────────────────────────────────
def select_candidate_spread(calls, spot: float):
    """
    Pick ATM bull-call-spread candidate from the live chain.

    Long leg  = nearest listed call strike at or above spot (slightly OTM
                by design).
    Short leg = nearest listed strike to long_strike + SPREAD_WIDTH; must
                be strictly above the long strike and within 2.5 points of
                the target width.
    Premiums  = (bid + ask) / 2.

    Returns
    -------
    dict with keys: long_strike, short_strike, long_premium,
                    short_premium, iv
    or None if any validity check fails (prints one dim() reason).
    Any unexpected exception is caught, printed, and returns None.
    """
    try:
        import math

        atm_up = calls[calls["strike"] >= spot].sort_values("strike").reset_index(drop=True)

        if len(atm_up) < 2:
            print(dim("  [gate] candidate: fewer than 2 strikes at/above ATM — skipping gate"))
            return None

        long_row     = atm_up.iloc[0]
        long_strike  = float(long_row["strike"])

        # Short leg: nearest strike to long_strike + SPREAD_WIDTH, strictly above long
        short_candidates = atm_up[atm_up["strike"] > long_strike].copy()
        if short_candidates.empty:
            print(dim("  [gate] candidate: no strikes above long leg — skipping gate"))
            return None

        target    = long_strike + SPREAD_WIDTH
        short_row = short_candidates.iloc[
            (short_candidates["strike"] - target).abs().argsort()
        ].iloc[0]

        if abs(float(short_row["strike"]) - target) > 2.5:
            print(dim(
                f"  [gate] candidate: no strike near target width "
                f"(closest {float(short_row['strike']):.1f}, target {target:.1f}) — skipping gate"
            ))
            return None

        long_bid,  long_ask  = float(long_row["bid"]),  float(long_row["ask"])
        short_bid, short_ask = float(short_row["bid"]), float(short_row["ask"])

        if long_bid <= 0 or long_ask <= 0:
            print(dim(f"  [gate] candidate: long leg bid/ask invalid ({long_bid}/{long_ask}) — skipping gate"))
            return None
        if short_bid <= 0 or short_ask <= 0:
            print(dim(f"  [gate] candidate: short leg bid/ask invalid ({short_bid}/{short_ask}) — skipping gate"))
            return None

        long_mid  = (long_bid  + long_ask)  / 2
        short_mid = (short_bid + short_ask) / 2

        if (long_ask - long_bid) / long_mid > 0.25:
            print(dim(f"  [gate] candidate: long leg spread too wide ({(long_ask - long_bid) / long_mid:.2%}) — skipping gate"))
            return None
        if (short_ask - short_bid) / short_mid > 0.25:
            print(dim(f"  [gate] candidate: short leg spread too wide ({(short_ask - short_bid) / short_mid:.2%}) — skipping gate"))
            return None

        iv = float(long_row["impliedVolatility"])
        if math.isnan(iv) or iv <= 0.05 or iv >= 2.0:
            print(dim(f"  [gate] candidate: long leg IV={iv:.4f} out of range (0.05–2.0) — skipping gate"))
            return None

        net_debit = long_mid - short_mid
        if net_debit <= 0:
            print(dim(f"  [gate] candidate: net debit {net_debit:.2f} <= 0 (nonsense mid prices) — skipping gate"))
            return None

        return {
            "long_strike":   long_strike,
            "short_strike":  float(short_row["strike"]),
            "long_premium":  long_mid,
            "short_premium": short_mid,
            "iv":            iv,
        }

    except Exception as e:
        print(dim(f"  [gate] candidate selection error: {e} — blocking"))
        return None


# ── ANTHROPIC THESIS ──────────────────────────────────────────────────────────
def generate_thesis(ticker, ticker_data, macro, oi, checklist_score, earnings_date, earnings_days,
                    cand=None, gate=None, exit_date=None, expiration=None):
    """Call Anthropic API to generate a structured weekly trade thesis."""
    daily  = ticker_data["daily"]
    weekly = ticker_data["weekly"]
    vix    = macro.get("^VIX", {})
    spy    = macro.get("SPY",  {})

    if cand is not None and gate is not None:
        net_debit = cand["long_premium"] - cand["short_premium"]
        preamble  = (
            f"You do not choose trade direction, structure, strikes, or expiry.\n"
            f"The trade under consideration is fixed: bull call spread "
            f"{cand['long_strike']:.0f}/{cand['short_strike']:.0f}, "
            f"expiration {expiration}, hard exit {exit_date}, "
            f"entry debit ~{net_debit:.2f}, gate PoP {gate['pop']:.0%}, "
            f"gate EV ${gate['ev_per_contract']:+.2f}/contract. "
            f"Do not issue GO/NO-GO verdicts or position sizing — the decision "
            f"is made by upstream filters. Provide market context, key levels, "
            f"and invalidation conditions for THIS trade only. "
            f"Remove any 'checklist verdict' section.\n\n"
        )
        sections = (
            "Write a concise weekly trade thesis with these exact sections:\n"
            "1. MARKET CONTEXT (2-3 sentences on macro + AAPL structure)\n"
            "2. DIRECTIONAL BIAS (BULLISH / BEARISH / NEUTRAL with specific reasoning)\n"
            "3. KEY LEVELS (support and resistance levels that matter this week)\n"
            "4. INVALIDATION (what price action would make this spread thesis wrong)\n\n"
            "Be direct and specific. No hedging language."
        )
    else:
        preamble = ""
        sections = (
            "Write a concise weekly trade thesis with these exact sections:\n"
            "1. MARKET CONTEXT (2-3 sentences on macro + AAPL structure)\n"
            "2. DIRECTIONAL BIAS (BULLISH / BEARISH / NEUTRAL with specific reasoning)\n"
            "3. KEY LEVELS (support and resistance levels that matter this week)\n"
            "4. TRADE SETUP (specific strike, expiry, structure — call/put/spread, entry trigger, target, stop)\n"
            "5. INVALIDATION (what price action would make this thesis wrong)\n"
            "6. CHECKLIST VERDICT (GO / NO-GO with reason if no-go)\n\n"
            "Be direct and specific. No hedging language. If the setup is bad, say so clearly."
        )

    prompt = f"""{preamble}You are a professional options trader doing a weekly pre-trade analysis.

TICKER: {ticker}
DATE: {datetime.today().strftime('%Y-%m-%d')}

DAILY TECHNICALS:
- Spot: ${daily['spot']:.2f}
- EMA14: ${daily['ema14']:.2f}  EMA28: ${daily['ema28']:.2f}  EMA50: ${daily['ema50']:.2f}
- RSI(14): {daily['rsi']}
- MACD Histogram: {daily['macd_hist']}
- ATR(14): ${daily['atr']:.2f}
- 20-day Support: ${daily['support']:.2f}  Resistance: ${daily['resist']:.2f}

WEEKLY TECHNICALS:
- EMA14: ${weekly['ema14']:.2f}  EMA28: ${weekly['ema28']:.2f}
- RSI(14): {weekly['rsi']}
- MACD Histogram: {weekly['macd_hist']}
- 52-week High: ${weekly['high52']:.2f}  Low: ${weekly['low52']:.2f}

MACRO:
- SPY: ${spy.get('spot', 0):.2f}  5d return: {spy.get('ret5d', 0):+.1f}%  Above EMA20: {spy.get('above_ema20')}
- VIX: {vix.get('spot', 0):.1f}

30 DTE OPTIONS STRUCTURE:
- Max Pain: ${oi['max_pain']:.1f}
- P/C OI Ratio: {oi['pc_oi']:.2f}
- IV Skew (OTM put vs ATM call): {oi['iv_skew']}%
- Top Call OI strikes: {[f"${r['strike']} ({int(r['openInterest']):,})" for r in oi['top_call_oi'][:3]]}
- Top Put OI strikes:  {[f"${r['strike']} ({int(r['openInterest']):,})" for r in oi['top_put_oi'][:3]]}

PRE-TRADE CHECKLIST: {checklist_score}/5 passed
EARNINGS: {earnings_date} ({earnings_days} days away)

{sections}"""

    try:
        client   = anthropic.Anthropic()
        response = client.messages.create(
            model      = "claude-sonnet-4-6",
            max_tokens = 2000,
            messages   = [{"role": "user", "content": prompt}]
        )
        return response.content[0].text
    except Exception as e:
        return f"[Anthropic API error: {e}]"

# ── ARCHIVE ───────────────────────────────────────────────────────────────────
def save_archive(ticker, ticker_data, macro, oi, checklist_score, thesis, earnings_date, earnings_days):
    os.makedirs("archive_weekly", exist_ok=True)
    ts   = now_et().strftime("%Y%m%d_%H%M%S")
    base = f"archive_weekly/{ticker}_{ts}"

    payload = {
        "timestamp":      now_et().isoformat(timespec="seconds"),
        "ticker":         ticker,
        "checklist_score":checklist_score,
        "earnings_date":  earnings_date,
        "earnings_days":  earnings_days,
        "daily":          ticker_data["daily"],
        "weekly":         ticker_data["weekly"],
        "macro": {
            sym: {k: v for k, v in d.items()}
            for sym, d in macro.items()
        },
        "oi_structure":   oi,
        "thesis":         thesis,
    }

    with open(base + ".json", "w") as f:
        json.dump(payload, f, indent=2, default=str)
    with open(base + ".txt", "w") as f:
        f.write(strip_ansi(thesis))

    return base + ".json", base + ".txt"

# ── REPORT ────────────────────────────────────────────────────────────────────
def print_report(ticker, ticker_data, macro, oi, score, checks, thesis, earnings_date, earnings_days):
    now  = now_et().strftime("%Y-%m-%d %H:%M")
    daily  = ticker_data["daily"]
    weekly = ticker_data["weekly"]
    vix    = macro.get("^VIX", {})
    spy    = macro.get("SPY",  {})
    qqq    = macro.get("QQQ",  {})

    print(f"\n{'─'*W}")
    print(bold(f"  📅  {ticker} WEEKLY SCANNER  │  {now}"))
    print(f"{'─'*W}")
    print()

    # ── 1. PRE-TRADE CHECKLIST ────────────────────────────────────────────────
    score_color = bull if score >= 4 else (warn if score == 3 else bear)
    verdict     = bull("GO ✓") if score >= 4 else (warn("MARGINAL") if score == 3 else bear("NO-GO ✗"))
    print(hdr("┌─ PRE-TRADE CHECKLIST ───────────────────────────────────────┐"))
    print(f"  Score: {score_color(f'{score}/5')}  →  {verdict}")
    print("  " + "─" * (W - 2))
    for name, passed, note in checks:
        icon  = bull("  ✓") if passed else bear("  ✗")
        color = bull if passed else bear
        print(f"{icon}  {color(name)}")
        print(f"     {dim(note)}")
    print()

    # ── 2. MACRO OVERVIEW ─────────────────────────────────────────────────────
    print(hdr("┌─ MACRO OVERVIEW ────────────────────────────────────────────┐"))
    for sym, label in [("SPY", "S&P 500"), ("QQQ", "Nasdaq"), ("^VIX", "VIX")]:
        d = macro.get(sym, {})
        spot_s = f"${d.get('spot', 0):.2f}"
        ret5   = d.get("ret5d", 0)
        ret_s  = bull(f"{ret5:+.1f}%") if ret5 >= 0 else bear(f"{ret5:+.1f}%")
        if sym == "^VIX":
            v = d.get("spot", 0)
            status = bull("LOW — buy options ✓") if v < 15 else \
                     warn("MEDIUM — normal")      if v < 25 else \
                     bear("HIGH — options expensive ✗")
            print(f"  {bold(label):<20}  {spot_s}  5d: {ret_s}   {status}")
        else:
            a20 = bull("above EMA20 ✓") if d.get("above_ema20") else bear("below EMA20 ✗")
            a50 = bull("above EMA50 ✓") if d.get("above_ema50") else bear("below EMA50 ✗")
            print(f"  {bold(label):<20}  {spot_s}  5d: {ret_s}   {a20}  {a50}")
    print()

    # ── 3. AAPL TECHNICAL LEVELS ──────────────────────────────────────────────
    print(hdr("┌─ AAPL TECHNICAL LEVELS ─────────────────────────────────────┐"))
    spot = daily["spot"]
    print(f"  {'':>25}  {'DAILY':>10}  {'WEEKLY':>10}")
    print("  " + "─" * (W - 2))

    for label, dk, wk in [
        ("Spot",        "spot",  "spot"),
        ("EMA 14",      "ema14", "ema14"),
        ("EMA 28",      "ema28", "ema28"),
        ("EMA 50",      "ema50", "ema50"),
        ("RSI(14)",     "rsi",   "rsi"),
        ("MACD Hist",   "macd_hist", "macd_hist"),
        ("ATR(14)",     "atr",   "atr"),
        ("Support",     "support", "support"),
        ("Resistance",  "resist", "resist"),
    ]:
        dv = daily.get(dk, "—")
        wv = weekly.get(wk, "—")
        dv_s = f"${dv:.2f}" if isinstance(dv, float) and dk not in ["rsi","macd_hist"] else \
               f"{dv:.1f}"  if isinstance(dv, float) else str(dv)
        wv_s = f"${wv:.2f}" if isinstance(wv, float) and wk not in ["rsi","macd_hist"] else \
               f"{wv:.1f}"  if isinstance(wv, float) else str(wv)
        # color spot relative to EMAs
        if dk == "ema14":
            dv_s = bull(dv_s) if spot > daily["ema14"] else bear(dv_s)
        if dk == "ema28":
            dv_s = bull(dv_s) if spot > daily["ema28"] else bear(dv_s)
        print(f"  {label:<25}  {dv_s:>10}  {wv_s:>10}")
    print()

    # ── 4. 30 DTE OPTIONS STRUCTURE ───────────────────────────────────────────
    print(hdr("┌─ 30 DTE OPTIONS STRUCTURE (Institutional Positioning) ──────┐"))
    pc_col  = bull if oi["pc_oi"] < 0.80 else (warn if oi["pc_oi"] < 1.0 else bear)
    skew_s  = f"{oi['iv_skew']:+.1f}%" if oi["iv_skew"] is not None else "n/a"
    skew_c  = bear if (oi["iv_skew"] or 0) > 3 else bull
    max_pain_s = bold(f"${oi['max_pain']:.1f}")
    pc_oi_s    = pc_col(f"{oi['pc_oi']:.2f}")
    pc_lean    = 'bullish lean' if oi['pc_oi'] < 0.80 else 'neutral/bearish lean'
    skew_note  = 'put premium elevated — hedging active' if (oi['iv_skew'] or 0) > 3 else 'normal skew'
    print(f"  Max Pain        : {max_pain_s}")
    print(f"  P/C OI Ratio    : {pc_oi_s}  ({pc_lean})")
    print(f"  IV Skew         : {skew_c(skew_s)}  ({skew_note})")
    print(f"  Total Call OI   : {int(oi['total_call_oi']):>10,}")
    print(f"  Total Put  OI   : {int(oi['total_put_oi']):>10,}")
    print()
    print(f"  {'SIDE':<6}  {'STRIKE':>8}  {'OI':>10}  {'IV':>8}")
    print("  " + "─" * (W - 2))
    for r in oi["top_call_oi"]:
        iv_s = f"{r['impliedVolatility']*100:.1f}%" if r.get('impliedVolatility') else "  n/a"
        print(f"  {bull('CALL'):<15}  ${r['strike']:>7.1f}  {int(r['openInterest']):>10,}  {iv_s:>8}")
    for r in oi["top_put_oi"]:
        iv_s = f"{r['impliedVolatility']*100:.1f}%" if r.get('impliedVolatility') else "  n/a"
        print(f"  {bear('PUT'):<15}  ${r['strike']:>7.1f}  {int(r['openInterest']):>10,}  {iv_s:>8}")
    print()

    # ── 5. EARNINGS AWARENESS ─────────────────────────────────────────────────
    print(hdr("┌─ EARNINGS AWARENESS ────────────────────────────────────────┐"))
    if earnings_days is not None:
        e_color = bear if earnings_days <= 7 else (warn if earnings_days <= 14 else bull)
        e_warn  = "  ⚠️  DO NOT hold options through earnings!" if earnings_days <= 7 else \
                  "  ⚠️  Approaching — plan exit before earnings" if earnings_days <= 14 else \
                  "  ✓  Safe window for weekly/biweekly trades"
        print(f"  {ticker} Earnings  :  {bold(earnings_date)}  ({e_color(f'{earnings_days} days away')})")
        print(f"  {e_color(e_warn)}")
    else:
        print(f"  {warn('No earnings date configured — update EARNINGS dict in script')}")
    print()

    # ── 6. AI THESIS ──────────────────────────────────────────────────────────
    print(hdr("┌─ WEEKLY TRADE THESIS  (AI-Generated) ───────────────────────┐"))
    for line in thesis.split("\n"):
        stripped = line.strip()
        if not stripped:
            print()
            continue
        # Color section headers
        if stripped.startswith(("1.", "2.", "3.", "4.", "5.", "6.")):
            print(f"  {bold(stripped)}")
        elif "GO" in stripped and "NO-GO" not in stripped:
            print(f"  {bull(stripped)}")
        elif "NO-GO" in stripped:
            print(f"  {bear(stripped)}")
        elif "BULLISH" in stripped:
            print(f"  {bull(stripped)}")
        elif "BEARISH" in stripped:
            print(f"  {bear(stripped)}")
        else:
            print(f"  {stripped}")
    print()

    print(f"{'─'*W}")
    print(dim(f"  market data + Anthropic API  |  Not financial advice  |  {now}"))
    print(f"{'─'*W}\n")

# ── GATE HELPERS ──────────────────────────────────────────────────────────────
def _compute_exit_date(earnings_date, expiration: str) -> str:
    """
    Hard exit date: day before earnings if earnings falls on or before
    expiration, otherwise the expiration date itself.

    earnings_date is a "YYYY-MM-DD" string (from earnings_check) or None.
    expiration is always a "YYYY-MM-DD" string (from fetch_options_30dte).
    """
    if earnings_date is None:
        return expiration
    earn_dt = datetime.strptime(earnings_date, "%Y-%m-%d").date()
    exp_dt  = datetime.strptime(expiration,    "%Y-%m-%d").date()
    if earn_dt <= exp_dt:
        return (datetime.strptime(earnings_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    return expiration


def _format_gate_block(gate, cand, exit_date, exp, spot) -> str:
    """
    Build the NO-TRADE thesis text: verdict, reasons, and a full audit
    table. Fields that are unavailable when cand is None show 'n/a'.
    """
    lines = ["SPREAD GATE: NO-TRADE", ""]
    lines.append("Reasons:")
    for r in gate["reasons"]:
        lines.append(f"  • {r}")
    lines.append("")
    lines.append("Audit:")
    lines.append(f"  Spot           : ${spot:.2f}")
    if cand is not None:
        lines.append(f"  Long strike    : ${cand['long_strike']:.2f}")
        lines.append(f"  Short strike   : ${cand['short_strike']:.2f}")
        lines.append(f"  Long premium   : ${cand['long_premium']:.2f}")
        lines.append(f"  Short premium  : ${cand['short_premium']:.2f}")
        lines.append(f"  IV (long leg)  : {cand['iv'] * 100:.1f}%")
    else:
        for label in ("Long strike", "Short strike", "Long premium", "Short premium", "IV (long leg)"):
            lines.append(f"  {label:<15}: n/a")
    lines.append(f"  Expiration     : {exp}")
    lines.append(f"  Hard exit      : {exit_date if exit_date is not None else 'n/a'}")
    pop_s = f"{gate['pop']:.2%}"          if gate["pop"]              is not None else "n/a"
    ev_s  = f"${gate['ev_per_contract']:+.2f}" if gate["ev_per_contract"] is not None else "n/a"
    lines.append(f"  PoP            : {pop_s}")
    lines.append(f"  EV/contract    : {ev_s}")
    return "\n".join(lines)


# ── MAIN ──────────────────────────────────────────────────────────────────────
def run(source=None):
    src = _resolve_source(source)
    print(f"\n{C.CYAN}  Fetching {TICKER} weekly data...{C.RESET}")
    daily, weekly     = fetch_weekly_data(TICKER, source=src)

    print(f"{C.CYAN}  Fetching macro data (SPY, QQQ, VIX)...{C.RESET}")
    macro_raw         = fetch_macro(source=src)

    print(f"{C.CYAN}  Fetching 30 DTE options chain...{C.RESET}")
    calls, puts, exp  = fetch_options_30dte(TICKER, source=src)

    print(f"{C.CYAN}  Analyzing...{C.RESET}")
    ticker_data       = analyze_ticker(daily, weekly)
    macro             = analyze_macro(macro_raw)
    oi                = analyze_oi_structure(calls, puts, ticker_data["daily"]["spot"])
    earnings_date, earnings_days = earnings_check(TICKER)
    score, checks     = checklist(ticker_data, macro, oi, earnings_days)

    cand = select_candidate_spread(calls, ticker_data["daily"]["spot"])
    if cand is None:
        gate      = {"verdict": "NO-TRADE", "pop": None, "ev_per_contract": None,
                     "reasons": ["no valid candidate: chain too thin or failed sanity checks"]}
        exit_date = None
    else:
        exit_date = _compute_exit_date(earnings_date, exp)
        gate      = evaluate_spread_gate(
            spot=ticker_data["daily"]["spot"], iv=cand["iv"],
            long_strike=cand["long_strike"],   short_strike=cand["short_strike"],
            long_premium=cand["long_premium"], short_premium=cand["short_premium"],
            entry_date=datetime.today().strftime("%Y-%m-%d"),
            exit_date=exit_date, expiration=exp,
        )

    if gate["verdict"] == "NO-TRADE":
        thesis = _format_gate_block(gate, cand, exit_date, exp,
                                    ticker_data["daily"]["spot"])
    else:
        print(f"{C.CYAN}  Generating AI thesis..."
              f"  — PoP {gate['pop']:.0%}, EV ${gate['ev_per_contract']:+.2f}{C.RESET}")
        thesis = generate_thesis(TICKER, ticker_data, macro, oi,
                                 score, earnings_date, earnings_days,
                                 cand=cand, gate=gate, exit_date=exit_date,
                                 expiration=exp)

    fname_json, fname_txt = save_archive(TICKER, ticker_data, macro, oi,
                                          score, thesis, earnings_date, earnings_days)
    print(f"{C.GRAY}  Archived → {fname_json}{C.RESET}")
    print(f"{C.GRAY}  Report   → {fname_txt}{C.RESET}")

    print_report(TICKER, ticker_data, macro, oi, score, checks,
                 thesis, earnings_date, earnings_days)

if __name__ == "__main__":
    run()
