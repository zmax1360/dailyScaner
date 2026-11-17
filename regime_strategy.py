"""regime_strategy — the options structure for today, from gamma regime, trend and move.

Fixed rules, no AI (Game Plan section "Strategy for today"). Rule v2 from the
2026-11-17 backtests (``scripts/regime_strategy_backtest.py`` on research/butterfly-backtest,
36 AAPL / 21 SPY / 10 TSLA days, entries 11:00-14:00 held to the close):

    near the gamma flip (UNSETTLED)        → no trade: the map gives no read
    already moved ≥ BIG_MOVE_PCT from open → no range trade (condors lost after big moves)
    positive gamma                         → short iron condor at gamma support/resistance;
                                             long call fly at support when the walls do
                                             not straddle the price
    negative gamma + EMA trend             → debit vertical in the trend direction, only
                                             for tickers in TREND_SPREAD_TICKERS (it lost on
                                             AAPL: 13-18% win rate)
    negative gamma, no trend               → no trade

Status is PAPER: the rule was chosen on the same days it was measured on, so it has to
prove itself forward before money goes in. Payoffs assume closing before 16:00 (these
options are physically settled). Missing inputs give "no pick" with the reason; nothing
is guessed.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

POSITIVE, NEGATIVE, UNSETTLED, UNKNOWN = "POSITIVE", "NEGATIVE", "UNSETTLED", "UNKNOWN"
CALLS, PUTS = "CALLS", "PUTS"

BIG_MOVE_PCT = 1.4                 # % from today's open (≈ $5 on AAPL at 350)
WING_STEPS = {"default": 1, "AAPL": 2, "TSLA": 2, "SPY": 2}   # wing width in strike steps
TREND_SPREAD_TICKERS = {"SPY"}     # negative-gamma trend spreads: only where they held up
FLY_NEAR_PCT = 0.5                 # support this close to spot can centre the fly
STATUS = "PAPER"
MAX_RISK = 40.0                    # $ per trade the owner allows; a bigger max loss is flagged

NO_TRADE = "NO_TRADE"
IRON_CONDOR = "IRON_CONDOR"
CALL_FLY = "CALL_FLY"
BULL_CALL_SPREAD = "BULL_CALL_SPREAD"
BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
LABEL = {NO_TRADE: "No trade", IRON_CONDOR: "Short iron condor",
         CALL_FLY: "Long call butterfly", BULL_CALL_SPREAD: "Bull call spread",
         BEAR_PUT_SPREAD: "Bear put spread"}


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _usd(v: float) -> str:
    return f"${v:,.2f}".replace(".00", "")


def strike_step(gamma: dict | None, spot: float) -> float | None:
    """Spacing of listed strikes near spot, from the gamma read's strikes."""
    raw = (gamma or {}).get("by_strike") or []
    ks = sorted(float(k) for k, *_ in (raw.items() if isinstance(raw, dict) else raw))
    ks = [k for k in ks if abs(k - spot) <= spot * 0.05]
    diffs = sorted({round(b - a, 4) for a, b in zip(ks, ks[1:]) if b > a})
    return diffs[0] if diffs else None


def _pick(code: str, reason: str, legs=(), **extra) -> dict[str, Any]:
    return {"code": code, "label": LABEL[code], "reason": reason, "status": STATUS,
            "legs": [list(x) for x in legs], **extra}


def _quote_book(quotes: pd.DataFrame | None, expiry: str | None):
    if quotes is None or quotes.empty:
        return {}
    q = quotes
    if expiry and "expiry" in q:
        q = q[q["expiry"].astype(str).str[:10] == str(expiry)[:10]]
    book = {}
    for r in q.itertuples():
        b, a = _f(getattr(r, "bid", None)), _f(getattr(r, "ask", None))
        if b is None or a is None or a <= 0 or a < b or b < 0:
            continue
        book[(str(r.side).upper(), float(r.strike))] = (b, a)
    return book


def price(legs, quotes: pd.DataFrame | None, expiry: str | None) -> dict[str, Any] | None:
    """Mid and worst-case cost in $ per 1 lot (negative = credit), max loss and max profit
    at expiry. None when a leg has no usable quote (or nothing to sell into)."""
    book = _quote_book(quotes, expiry)
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
    ks = sorted({float(k) for _, k, _ in legs})
    grid = [ks[0] - 1000] + ks + [ks[-1] + 1000]
    pay = [sum(q * (max(0.0, s - k) if sd == "CALL" else max(0.0, k - s))
               for sd, k, q in legs) for s in grid]
    return {"cost_mid": round(mid * 100, 2), "cost_worst": round(worst * 100, 2),
            "max_loss": round((mid - min(pay)) * 100, 2),
            "max_profit": round((max(pay) - mid) * 100, 2)}


def choose(*, ticker: str, regime: str, trend_side: str, spot: Any, session_open: Any,
           gamma: dict | None, quotes: pd.DataFrame | None = None) -> dict[str, Any]:
    """Today's structure as a JSON-safe dict (see the module doc for the rule)."""
    t = str(ticker or "").upper()
    px = _f(spot)
    if px is None or px <= 0:
        return _pick(NO_TRADE, "No price in this scan.")
    if regime == UNKNOWN or not gamma:
        return _pick(NO_TRADE, "No usable gamma read, so no regime to pick a structure from.")
    if regime == UNSETTLED:
        return _pick(NO_TRADE, "Price is at the gamma flip: no read on range vs trend.")
    step = strike_step(gamma, px)
    if not step:
        return _pick(NO_TRADE, "Strike spacing near the price is unknown.")
    w = step * WING_STEPS.get(t, WING_STEPS["default"])
    expiry = gamma.get("expiry")
    opn = _f(session_open)
    moved = None if opn is None else px - opn
    big = moved is not None and abs(moved) >= px * BIG_MOVE_PCT / 100
    move_txt = "" if moved is None else f" Moved {_usd(abs(moved))} {'up' if moved > 0 else 'down'} from the open."

    def done(code, reason, legs):
        out = _pick(code, reason + move_txt, legs, expiry=expiry, width=w,
                    big_move=big, moved=None if moved is None else round(moved, 2))
        p = price(legs, quotes, expiry)
        if p:
            out.update(p, over_budget=p["max_loss"] > MAX_RISK)
        return out

    if regime == POSITIVE:
        if big:
            return done(NO_TRADE, f"Positive gamma, but the stock already moved "
                                  f"{BIG_MOVE_PCT:g}%+ today: range trades lost after big "
                                  "moves in the backtest.", ())
        sup, res = _f(gamma.get("support")), _f(gamma.get("resistance"))
        if sup is not None and res is not None and sup < px < res:
            legs = (("PUT", sup - w, 1), ("PUT", sup, -1), ("CALL", res, -1), ("CALL", res + w, 1))
            return done(IRON_CONDOR, f"Positive gamma: dealers dampen moves. Sell the range "
                                     f"between gamma support {_usd(sup)} and resistance "
                                     f"{_usd(res)}.", legs)
        center = sup if sup is not None and abs(sup - px) <= px * FLY_NEAR_PCT / 100 \
            else min((k for k in _strikes(gamma)), key=lambda k: abs(k - px), default=None)
        if center is None:
            return done(NO_TRADE, "Positive gamma, but no strike to centre a fly on.", ())
        legs = (("CALL", center - w, 1), ("CALL", center, -2), ("CALL", center + w, 1))
        where = "gamma support" if center == sup else "the strike nearest the price"
        return done(CALL_FLY, f"Positive gamma: expect a pin. Fly centred on {where} "
                              f"{_usd(center)}.", legs)

    # NEGATIVE
    if trend_side not in (CALLS, PUTS):
        return done(NO_TRADE, "Negative gamma with no EMA trend: moves can run either way.", ())
    if t not in TREND_SPREAD_TICKERS:
        return done(NO_TRADE, f"Negative gamma, trend {trend_side.lower()}: the trend spread "
                              f"lost on {t} in the backtest, so no trade.", ())
    if big and ((moved > 0) != (trend_side == CALLS)):
        return done(NO_TRADE, "The EMA trend points against today's big move.", ())
    atm = min(_strikes(gamma), key=lambda k: abs(k - px))
    if trend_side == CALLS:
        legs = (("CALL", atm, 1), ("CALL", atm + w, -1))
        return done(BULL_CALL_SPREAD, "Negative gamma and EMA trend up: moves tend to "
                                      "extend.", legs)
    legs = (("PUT", atm, 1), ("PUT", atm - w, -1))
    return done(BEAR_PUT_SPREAD, "Negative gamma and EMA trend down: moves tend to extend.", legs)


def _strikes(gamma: dict) -> list[float]:
    raw = gamma.get("by_strike") or []
    return sorted(float(k) for k, *_ in (raw.items() if isinstance(raw, dict) else raw))


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
    return out
