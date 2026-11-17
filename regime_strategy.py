"""regime_strategy — today's 0DTE options structure, chosen by simulation on our own history.

Fixed rules, no AI (Game Plan section "Strategy for today"). Status PAPER.

Gates (any one gives "no trade", with the reason):
    the gamma expiry is not today          no 0DTE: structures held overnight lost money
    before ENTRY_WINDOW[0] (12:00)         11:00 entries were the worst in the backtest
    after ENTRY_WINDOW[1] (14:30)          too little time left for a new entry
    price at the gamma flip (UNSETTLED)    the map gives no read on range vs trend
    no usable gamma / price / strikes

Candidates:
    positive gamma   short iron condor at gamma support/resistance, short iron condor
                     outside ± the implied move, long call fly at the nearest strike and
                     at gamma support. After a move ≥ BIG_MOVE_PCT from the open: none of
                     these (range trades lost after big moves).
    negative gamma   with an EMA trend: debit vertical in the trend direction (only for
                     TREND_SPREAD_TICKERS; it lost on AAPL). Without a trend: nothing.

Choice: every candidate is priced at the scan's quotes (fill halfway from mid to worst
case, plus fees) and replayed over the 0DTE move history built by
``scripts/build_move_samples.py`` (data/research/move_samples.json): each past move,
measured in implied-move units, is scaled by today's implied move (ATM straddle). The
candidate with the highest average P&L whose max loss fits MAX_RISK is the pick, if that
average is above zero; the others are listed. With too little history the module says so and picks nothing.

Payoffs assume closing by 15:45 (these options settle in shares).
"""

from __future__ import annotations

import json
import math
import os
from datetime import datetime, time
from statistics import mean
from typing import Any

import pandas as pd

POSITIVE, NEGATIVE, UNSETTLED, UNKNOWN = "POSITIVE", "NEGATIVE", "UNSETTLED", "UNKNOWN"
CALLS, PUTS = "CALLS", "PUTS"

STATUS = "PAPER"
ENTRY_WINDOW = (time(12, 0), time(14, 30))
BIG_MOVE_PCT = 1.4                 # % from today's open (≈ $5 on AAPL at 350)
WING_STEPS = {"default": 1, "AAPL": 2, "TSLA": 2, "SPY": 2}   # wing width in strike steps
TREND_SPREAD_TICKERS = {"SPY"}
MAX_RISK = 40.0                    # $ per trade the owner allows: a bigger max loss is never picked
FILL = 0.5                         # 0 = mid, 1 = worst case (ask / bid)
FEE = 0.75                         # $ per contract, charged to open and to close
MIN_SAMPLES = 12                   # fewer past entries than this: no simulation
SAMPLES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "data", "research", "move_samples.json")

NO_TRADE = "NO_TRADE"
IC_WALLS = "IC_WALLS"
IC_IMPLIED = "IC_IMPLIED"
FLY_ATM = "FLY_ATM"
FLY_SUPPORT = "FLY_SUPPORT"
BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
LABEL = {NO_TRADE: "No trade",
         IC_WALLS: "Short iron condor at the gamma walls",
         IC_IMPLIED: "Short iron condor outside the implied move",
         FLY_ATM: "Long call butterfly at the money",
         FLY_SUPPORT: "Long call butterfly at gamma support",
         BULL_CALL_SPREAD: "Bull call spread", BEAR_PUT_SPREAD: "Bear put spread"}


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _usd(v: float) -> str:
    return f"${v:,.2f}".replace(".00", "")


# ── history ─────────────────────────────────────────────────────────────────

_CACHE: dict[str, tuple[float, list]] = {}


def load_samples(path: str | None = None) -> list[dict]:
    """The 0DTE move history; [] when the file is missing or unreadable."""
    p = path or SAMPLES_PATH
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return []
    hit = _CACHE.get(p)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(p, encoding="utf-8") as fh:
            rows = json.load(fh).get("samples") or []
    except (OSError, ValueError, AttributeError):
        return []
    _CACHE[p] = (mtime, rows)
    return rows


def pick_samples(samples, ticker: str, regime: str) -> tuple[list[float], str]:
    """Past z values for this ticker: same regime when there are enough, else all."""
    mine = [s for s in samples or [] if s.get("ticker") == ticker]
    same = [s["z"] for s in mine if s.get("regime") == regime]
    if len(same) >= MIN_SAMPLES:
        return same, f"{len(same)} past {ticker} 0DTE entries in {regime.lower()} gamma " \
                     f"({len({s['day'] for s in mine if s.get('regime') == regime})} days)"
    if len(mine) >= MIN_SAMPLES:
        return [s["z"] for s in mine], f"{len(mine)} past {ticker} 0DTE entries, all " \
                                       f"regimes (only {len(same)} in {regime.lower()} gamma)"
    return [], f"only {len(mine)} past {ticker} 0DTE entries"


# ── chain helpers ───────────────────────────────────────────────────────────

def _strikes(gamma: dict) -> list[float]:
    raw = gamma.get("by_strike") or []
    return sorted(float(k) for k, *_ in (raw.items() if isinstance(raw, dict) else raw))


def strike_step(gamma: dict | None, spot: float) -> float | None:
    ks = [k for k in _strikes(gamma or {}) if abs(k - spot) <= spot * 0.05]
    diffs = sorted({round(b - a, 4) for a, b in zip(ks, ks[1:]) if b > a})
    return diffs[0] if diffs else None


def _book(quotes: pd.DataFrame | None, expiry: str | None) -> dict:
    if quotes is None or getattr(quotes, "empty", True):
        return {}
    q = quotes
    if expiry and "expiry" in q:
        q = q[q["expiry"].astype(str).str[:10] == str(expiry)[:10]]
    out = {}
    for r in q.itertuples():
        b, a = _f(getattr(r, "bid", None)), _f(getattr(r, "ask", None))
        if b is None or a is None or a <= 0 or a < b or b < 0:
            continue
        out[(str(r.side).upper(), float(r.strike))] = (b, a)
    return out


def implied_move(book: dict, spot: float) -> float | None:
    ks = sorted({k for _, k in book}, key=lambda k: abs(k - spot))
    for k in ks[:1]:
        c, p = book.get(("CALL", k)), book.get(("PUT", k))
        if c and p:
            return 0.5 * (c[0] + c[1]) + 0.5 * (p[0] + p[1])
    return None


def _payoff(legs, s: float) -> float:
    return sum(q * (max(0.0, s - k) if sd == "CALL" else max(0.0, k - s)) for sd, k, q in legs)


def price(legs, book: dict) -> dict[str, Any] | None:
    """$ per 1 lot: cost at mid, worst case and our fill (negative = credit), fees, max
    loss and max profit at expiry. None when a leg has no usable quote."""
    if not legs or not book:
        return None
    mid = worst = 0.0
    for side, k, qty in legs:
        q = book.get((side, float(k)))
        if q is None or (qty < 0 and q[0] <= 0):
            return None
        b, a = q
        mid += qty * 0.5 * (a + b)
        worst += qty * (a if qty > 0 else b)
    fill = mid + FILL * (worst - mid)
    fees = sum(abs(q) for *_, q in legs) * FEE * 2
    ks = sorted({float(k) for _, k, _ in legs})
    pay = [_payoff(legs, s) for s in [ks[0] - 1000] + ks + [ks[-1] + 1000]]
    return {"cost_mid": round(mid * 100, 2), "cost_worst": round(worst * 100, 2),
            "cost_fill": round(fill * 100, 2), "fees": round(fees, 2),
            "max_loss": round((fill - min(pay)) * 100 + fees, 2),
            "max_profit": round((max(pay) - fill) * 100 - fees, 2)}


def simulate(legs, priced: dict, spot: float, implied: float, zs: list[float]) -> dict:
    """Average P&L and win rate of ``legs`` over past moves scaled to today's implied move."""
    pnl = [_payoff(legs, spot + z * implied) * 100 - priced["cost_fill"] - priced["fees"]
           for z in zs]
    return {"sim_avg": round(mean(pnl), 2), "sim_win": round(sum(p > 0 for p in pnl) / len(pnl), 3),
            "sim_n": len(pnl)}


# ── the choice ──────────────────────────────────────────────────────────────

def _no(reason: str, **extra) -> dict[str, Any]:
    return {"code": NO_TRADE, "label": LABEL[NO_TRADE], "reason": reason, "status": STATUS,
            "legs": [], "alternatives": [], **extra}


def candidates(*, ticker: str, regime: str, trend_side: str, px: float, gamma: dict,
               w: float, implied: float | None, step: float, big: bool) -> dict:
    ks = _strikes(gamma)
    atm = min(ks, key=lambda k: abs(k - px))
    out: dict[str, tuple] = {}
    if regime == POSITIVE and not big:
        sup, res = _f(gamma.get("support")), _f(gamma.get("resistance"))
        if sup is not None and res is not None and sup < px < res:
            out[IC_WALLS] = (("PUT", sup - w, 1), ("PUT", sup, -1),
                             ("CALL", res, -1), ("CALL", res + w, 1))
        if implied:
            sp = math.floor((px - implied) / step) * step
            sc = math.ceil((px + implied) / step) * step
            out[IC_IMPLIED] = (("PUT", sp - w, 1), ("PUT", sp, -1),
                               ("CALL", sc, -1), ("CALL", sc + w, 1))
        out[FLY_ATM] = (("CALL", atm - w, 1), ("CALL", atm, -2), ("CALL", atm + w, 1))
        if sup is not None and sup != atm:
            out[FLY_SUPPORT] = (("CALL", sup - w, 1), ("CALL", sup, -2), ("CALL", sup + w, 1))
    elif regime == NEGATIVE and trend_side in (CALLS, PUTS) and ticker in TREND_SPREAD_TICKERS:
        if trend_side == CALLS:
            out[BULL_CALL_SPREAD] = (("CALL", atm, 1), ("CALL", atm + w, -1))
        else:
            out[BEAR_PUT_SPREAD] = (("PUT", atm, 1), ("PUT", atm - w, -1))
    return out


def choose(*, ticker: str, regime: str, trend_side: str, spot: Any, session_open: Any,
           gamma: dict | None, quotes: pd.DataFrame | None = None,
           now: datetime | None = None, samples: list[dict] | None = None) -> dict[str, Any]:
    """Today's structure as a JSON-safe dict. ``now`` None skips the 0DTE and time gates
    (for tests and replays); ``samples`` None skips the simulation."""
    t = str(ticker or "").upper()
    px = _f(spot)
    if px is None or px <= 0:
        return _no("No price in this scan.")
    if regime == UNKNOWN or not gamma:
        return _no("No usable gamma read, so no regime to pick a structure from.")
    expiry = gamma.get("expiry")
    if now is not None:
        if str(expiry)[:10] != f"{now:%Y-%m-%d}":
            return _no(f"No {t} expiry today (nearest {expiry}). Strategies run on 0DTE "
                       "days only: held overnight they lost money in the backtest.")
        if now.time() < ENTRY_WINDOW[0]:
            return _no(f"0DTE day: wait until {ENTRY_WINDOW[0]:%H:%M}. Entries before noon "
                       "were the worst in the backtest.")
        if now.time() > ENTRY_WINDOW[1]:
            return _no(f"Past {ENTRY_WINDOW[1]:%H:%M}: too late for a new 0DTE entry.")
    if regime == UNSETTLED:
        return _no("Price is at the gamma flip: no read on range vs trend.")
    step = strike_step(gamma, px)
    if not step:
        return _no("Strike spacing near the price is unknown.")
    w = step * WING_STEPS.get(t, WING_STEPS["default"])
    opn = _f(session_open)
    moved = None if opn is None else round(px - opn, 2)
    big = moved is not None and abs(moved) >= px * BIG_MOVE_PCT / 100
    book = _book(quotes, expiry)
    implied = implied_move(book, px)
    extra = {"expiry": expiry, "width": w, "moved": moved, "big_move": big,
             "implied_move": None if implied is None else round(implied, 2)}
    cands = candidates(ticker=t, regime=regime, trend_side=trend_side, px=px, gamma=gamma,
                       w=w, implied=implied, step=step, big=big)
    if not cands:
        if regime == POSITIVE and big:
            why = (f"Positive gamma, but the stock already moved {_usd(abs(moved))} "
                   f"({BIG_MOVE_PCT:g}%+) from the open: range trades lost after big moves.")
        elif regime == NEGATIVE and trend_side not in (CALLS, PUTS):
            why = "Negative gamma with no EMA trend: moves can run either way."
        elif regime == NEGATIVE:
            why = f"Negative gamma: the trend spread lost on {t} in the backtest."
        else:
            why = "No structure fits this regime."
        return _no(why, **extra)

    zs, basis = pick_samples(samples, t, regime) if samples is not None else ([], "")
    alts = []
    for code, legs in cands.items():
        row = {"code": code, "label": LABEL[code], "legs": [list(x) for x in legs]}
        p = price(legs, book)
        if p:
            row.update(p, over_budget=p["max_loss"] > MAX_RISK)
            if zs and implied:
                row.update(simulate(legs, p, px, implied, zs))
        alts.append(row)
    simmed = [a for a in alts if "sim_avg" in a]
    if simmed:
        simmed.sort(key=lambda a: a["sim_avg"], reverse=True)
        alts = simmed + [a for a in alts if "sim_avg" not in a]
        best = simmed[0]
        if best["sim_avg"] <= 0:
            return _no(f"No structure made money over {basis}: best was {best['label']} "
                       f"at {_usd(best['sim_avg'])} average.", alternatives=alts,
                       basis=basis, **extra)
        affordable = [a for a in simmed if a["sim_avg"] > 0 and not a["over_budget"]]
        if not affordable:
            return _no(f"The best structure over {basis} was {best['label']} "
                       f"({_usd(best['sim_avg'])} average), but its max loss "
                       f"{_usd(best['max_loss'])} is over your {_usd(MAX_RISK)} limit.",
                       alternatives=alts, basis=basis, **extra)
        best = affordable[0]
        reason = (f"Best of {len(simmed)} structures over {basis}: average "
                  f"{_usd(best['sim_avg'])}, won {best['sim_win']:.0%}.")
    else:
        best = alts[0]
        if samples is not None and not zs:
            return _no(f"Not enough history to simulate ({basis}). Candidates are listed, "
                       "none is picked.", alternatives=alts, basis=basis, **extra)
        if samples is not None:
            return _no("No usable quote for every leg (or no implied move) in this scan, "
                       "so nothing could be priced or simulated.", alternatives=alts,
                       basis=basis, **extra)
        reason = "Not simulated (no quotes or history): first candidate for this regime."
    regime_txt = {POSITIVE: "Positive gamma: dealers dampen moves.",
                  NEGATIVE: "Negative gamma: moves tend to extend."}[regime]
    move_txt = "" if moved is None else f" Moved {_usd(abs(moved))} {'up' if moved > 0 else 'down'} from the open."
    return {**{k: v for k, v in best.items()}, "status": STATUS,
            "reason": f"{regime_txt} {reason}{move_txt}", "alternatives": alts,
            "basis": basis, **extra}


# ── text ────────────────────────────────────────────────────────────────────

def legs_text(pick: dict[str, Any]) -> str:
    return " / ".join(f"{'Buy' if q > 0 else 'Sell'} {abs(q)} {_usd(k)} {sd.lower()}"
                      for sd, k, q in pick.get("legs") or [])


def lines(pick: dict[str, Any] | None) -> list[str]:
    """Telegram (HTML) lines."""
    if not pick:
        return []
    out = [f"🧩 Strategy ({pick.get('status', STATUS).lower()}): <b>{pick['label']}</b>"]
    if pick.get("legs"):
        out.append(legs_text(pick) + (f" · exp {pick['expiry']}" if pick.get("expiry") else ""))
    if pick.get("cost_mid") is not None:
        c = pick["cost_mid"]
        word = f"credit {_usd(-c)}" if c < 0 else f"debit {_usd(c)}"
        out.append(f"Mid {word} · max loss {_usd(pick['max_loss'])} · "
                   f"max profit {_usd(pick['max_profit'])}")
        if pick.get("over_budget"):
            out.append(f"⚠️ Max loss is over your {_usd(MAX_RISK)} limit: skip, or paper only.")
    out.append(pick["reason"])
    others = [a for a in pick.get("alternatives") or [] if a.get("code") != pick.get("code")
              and "sim_avg" in a]
    if others:
        out.append("Others: " + " · ".join(f"{a['label']} {_usd(a['sim_avg'])}" for a in others))
    return out
