"""recorder — once per scan: resolve today's open paper trade, or open one.

One paper trade per ticker per day. On an in-play day (the in-play rule's volume test
passes) the ``in_play`` slot decides; otherwise the ``normal`` slot. A rule whose status
is ``off`` is skipped. Every trade is a 1-lot debit spread in the signal's direction:
long the strike nearest spot, short the next strike ``width`` away, from the nearest
expiry on or after today, priced at the ask (long) and bid (short) of this scan's chain.

Exits (``exit`` slot): value >= entry + take_profit x (width - entry)  -> TARGET
                       value <= entry x (1 - stop)                      -> STOP
                       underlying through ``stop_spot`` (breakout rule)  -> STOP
                       time >= time_stop                                -> TIME
value = long bid - short ask (what closing would actually fetch). Paper only.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, time
from typing import Any

import pandas as pd

from ticker_rules import market, rules, store

SLOT_ORDER = ("in_play", "normal")


def _num(v: Any) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _quote(chain: pd.DataFrame, side: str, strike: float, expiry: str):
    c = chain[(chain["side"] == side) & (chain["strike"].astype(float) == float(strike))
              & (chain["expiry"].astype(str).str[:10] == expiry)]
    if c.empty:
        return None
    bid, ask = _num(c["bid"].iloc[0]), _num(c["ask"].iloc[0])
    if bid is None or ask is None or ask <= 0 or bid < 0 or ask < bid:
        return None
    return bid, ask


def build_spread(chain: pd.DataFrame, side: str, spot: float, day: str, ex: dict) -> dict | None:
    """Long nearest-spot strike, short ``width`` toward the move; None when unquotable."""
    if chain is None or chain.empty:
        return None
    exps = sorted(e for e in chain["expiry"].astype(str).str[:10].unique() if e >= day)
    if not exps:
        return None
    exp = exps[0]
    strikes = sorted({float(k) for k in chain[(chain["side"] == side)
                      & (chain["expiry"].astype(str).str[:10] == exp)]["strike"]})
    if not strikes:
        return None
    width = float(ex.get("width", 2.5))
    k_long = min(strikes, key=lambda k: (abs(k - spot), k))
    target = k_long + width if side == "CALL" else k_long - width
    k_short = min(strikes, key=lambda k: abs(k - target))
    if k_short == k_long or (k_short > k_long) != (side == "CALL"):
        return None
    ql, qs = _quote(chain, side, k_long, exp), _quote(chain, side, k_short, exp)
    if not ql or not qs:
        return None
    mx = float(ex.get("max_leg_spread", 0.10))
    if ql[1] - ql[0] > mx + 1e-9 or qs[1] - qs[0] > mx + 1e-9:
        return None
    w = abs(k_short - k_long)
    cost = ql[1] - qs[0]
    if not (0 < cost < w):
        return None
    tp, sp = float(ex.get("take_profit", 0.5)), float(ex.get("stop", 0.5))
    return {"expiry": exp, "long_strike": k_long, "short_strike": k_short,
            "entry_cost": round(cost, 4), "target_value": round(cost + tp * (w - cost), 4),
            "stop_value": round(cost * (1 - sp), 4), "width": w}


def spread_value(chain: pd.DataFrame, t: dict) -> float | None:
    ql = _quote(chain, t["side"], t["long_strike"], t["expiry"])
    qs = _quote(chain, t["side"], t["short_strike"], t["expiry"])
    if not ql or not qs:
        return None
    return round(ql[0] - qs[1], 4)


def _hhmm(text: str) -> time:
    h, m = (int(x) for x in str(text).split(":"))
    return time(h, m)


def resolve(t: dict, chain: pd.DataFrame, spot: float | None, now: datetime) -> dict:
    """Changes for an OPEN trade at this scan ({} when nothing changes)."""
    val = spread_value(chain, t)
    late = now.time() >= _hhmm(t["time_stop"])
    ss = _num(t.get("stop_spot"))
    broke = ss is not None and spot is not None and (
        (t["side"] == "CALL" and spot <= ss) or (t["side"] == "PUT" and spot >= ss))
    if val is None:
        if late:
            return {"status": store.NO_QUOTE, "exit_at": now.isoformat(timespec="minutes")}
        return {}
    ch = {"last_value": val}
    status = None
    if val >= t["target_value"]:
        status = store.TARGET
    elif val <= t["stop_value"] or broke:
        status = store.STOP
    elif late:
        status = store.TIME
    if status:
        ch.update(status=status, exit_at=now.isoformat(timespec="minutes"), exit_value=val,
                  pnl=round((val - t["entry_cost"]) * 100, 2))
    return ch


def decide(ticker: str, chain: pd.DataFrame, spot: float, now: datetime, ctx,
           settings: dict[str, dict]) -> dict | None:
    """A trade row to open at this scan, or None."""
    ex_row = settings.get("exit")
    ex = dict(store.DEFAULT_EXIT)
    ex.update((ex_row or {}).get("params") or {})
    if now.time() >= _hhmm(ex["time_stop"]):
        return None
    ip = settings.get("in_play")
    in_play = False
    if ip and ip["status"] != store.OFF and ctx.rvol is not None:
        thr = float((ip.get("params") or {}).get("rvol_min", 1.5))
        in_play = ctx.rvol >= thr
    slot = "in_play" if in_play else "normal"
    s = settings.get(slot)
    if not s or s["status"] == store.OFF:
        return None
    sig = rules.evaluate(s["rule"], s.get("params"), ctx)
    if not sig:
        return None
    day = now.date().isoformat()
    sp = build_spread(chain, sig["side"], spot, day, ex)
    if not sp:
        return None
    sp.pop("width")
    return {"day": day, "ticker": ticker.upper(), "settings_id": s["id"],
            "exit_id": (ex_row or {}).get("id"), "rule": s["rule"], "slot": slot,
            "reason": sig["reason"], "side": sig["side"], "spot": spot,
            "entry_at": now.isoformat(timespec="minutes"), "stop_spot": sig.get("stop_spot"),
            "time_stop": ex["time_stop"], "status": store.OPEN, **sp}


def on_scan(chain: pd.DataFrame, *, ticker: str, now: datetime, spot: float | None,
            ctx=None, db_path: str | None = None) -> dict | None:
    """Run at every scan. Returns {"event": "OPEN"|"CLOSE", "trade": row} when this scan
    opened or closed a paper trade, else None. ``ctx`` is fetched when not given."""
    settings = store.current(ticker, db_path=db_path)
    if not settings or spot is None:
        return None
    day = now.date().isoformat()
    t = store.trade_for_day(ticker, day, db_path=db_path)
    if t is not None:
        if t["status"] != store.OPEN:
            return None
        ch = resolve(t, chain, spot, now)
        if ch:
            store.update_trade(t["id"], ch, db_path=db_path)
        if ch.get("status"):
            t.update(ch)
            return {"event": "CLOSE", "trade": t}
        return None
    if ctx is None:
        ctx = market.fetch(ticker, now)
    row = decide(ticker, chain, spot, now, ctx, settings)
    if row is None:
        return None
    row["id"] = store.open_trade(row, db_path=db_path)
    return {"event": "OPEN", "trade": row}


def message(ev: dict | None) -> str | None:
    """Telegram HTML for an OPEN/CLOSE event (paper)."""
    if ev and "plan" in ev and "trade" not in ev:      # the scan's saved block
        ev = ev.get("event")
    if not ev:
        return None
    t = ev["trade"]
    leg = "P" if t["side"] == "PUT" else "C"
    spread = f"{t['long_strike']:g}/{t['short_strike']:g}{leg} {t['expiry'][5:]}"
    label = rules.RULES.get(t["rule"], {}).get("label", t["rule"])
    if ev["event"] == "OPEN":
        risk = t["entry_cost"] * 100
        return (f"📝 <b>PAPER · {html.escape(t['ticker'])}</b> · {html.escape(label)}\n"
                f"Bought {spread} for {t['entry_cost']:.2f} (risk ${risk:.0f}) · spot "
                f"{t['spot']:.2f}\nWhy: {html.escape(t['reason'] or '')}\n"
                f"Target {t['target_value']:.2f} · stop {t['stop_value']:.2f}"
                + (f" or spot {'≤' if t['side'] == 'CALL' else '≥'} {t['stop_spot']:.2f}"
                   if t.get('stop_spot') else "")
                + f" · out by {t['time_stop']}\nPaper only, not a trade signal.")
    pnl = t.get("pnl")
    return (f"📝 <b>PAPER · {html.escape(t['ticker'])}</b> · {html.escape(label)} closed\n"
            f"{spread}: {t['status']} at {t.get('exit_value') or 0:.2f} · "
            f"P&amp;L {'n/a' if pnl is None else f'${pnl:+.0f}'}")
