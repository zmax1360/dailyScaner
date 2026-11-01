"""plan — the strategy card for the Game Plan: which rule is active for the ticker today,
what it says right now, and (when it signals) the exact spread with its cost, reward and
odds. Built once per scan and saved in the archive, so the app and Telegram show the same.

Odds use the contracts' own implied volatility (average of the two legs) and the time left
to the expiry's close; they are the market's odds, not a forecast.
"""

from __future__ import annotations

import math
from datetime import datetime, time
from typing import Any

import pandas as pd

from ticker_rules import recorder, rules, store

TRADING_HOURS_YEAR = 6.5 * 252


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def years_left(now: datetime, expiry: str) -> float:
    """Trading time from ``now`` to 16:00 ET on ``expiry``, in years (6.5 h x 252 days)."""
    close = datetime.combine(datetime.fromisoformat(expiry).date(), time(16, 0))
    n = now.replace(tzinfo=None) if now.tzinfo else now
    hours_today = max((datetime.combine(n.date(), time(16, 0)) - n).total_seconds() / 3600, 0)
    days = pd.bdate_range(n.date(), close.date()).size - 1        # whole sessions after today
    return (min(hours_today, 6.5) + 6.5 * max(days, 0)) / TRADING_HOURS_YEAR


def prob_beyond(spot: float, level: float, iv: float, t: float, above: bool) -> float | None:
    """Market (lognormal) chance the stock finishes above / below ``level``."""
    if not (iv and t and iv > 0 and t > 0 and spot > 0 and level > 0):
        return None
    s = iv * math.sqrt(t)
    d2 = (math.log(spot / level) - 0.5 * s * s) / s
    return _ncdf(d2) if above else _ncdf(-d2)


def _iv(chain: pd.DataFrame, side: str, strike: float, expiry: str) -> float | None:
    if chain is None or chain.empty or "iv" not in chain.columns:
        return None
    c = chain[(chain["side"] == side) & (chain["strike"].astype(float) == float(strike))
              & (chain["expiry"].astype(str).str[:10] == expiry)]
    v = recorder._num(c["iv"].iloc[0]) if len(c) else None
    return v if v and v > 0 else None


def spread_card(chain: pd.DataFrame, side: str, spot: float, now: datetime, ex: dict) -> dict | None:
    """The debit spread the recorder would buy, with cost, reward and odds."""
    sp = recorder.build_spread(chain, side, spot, now.date().isoformat(), ex)
    if not sp:
        return None
    call = side == "CALL"
    cost, w = sp["entry_cost"], sp["width"]
    be = sp["long_strike"] + cost if call else sp["long_strike"] - cost
    ivs = [v for v in (_iv(chain, side, sp["long_strike"], sp["expiry"]),
                       _iv(chain, side, sp["short_strike"], sp["expiry"])) if v]
    iv = sum(ivs) / len(ivs) if ivs else None
    t = years_left(now, sp["expiry"])
    return {
        **sp, "side": side, "risk": round(cost * 100, 2), "max_gain": round((w - cost) * 100, 2),
        "reward_risk": round((w - cost) / cost, 2), "break_even": round(be, 2), "iv": iv,
        "p_profit": prob_beyond(spot, be, iv, t, call),
        "p_max": prob_beyond(spot, sp["short_strike"], iv, t, call),
        "expected_move": round(spot * iv * math.sqrt(t), 2) if iv and t > 0 else None,
        "target_gain": round((sp["target_value"] - cost) * 100, 2),
        "stop_loss": round((sp["stop_value"] - cost) * 100, 2),
    }


def build(ticker: str, ctx, chain: pd.DataFrame, spot: float | None, now: datetime,
          *, settings: dict | None = None, db_path: str | None = None) -> dict | None:
    """The strategy card for this scan, or None when the ticker has no rules."""
    settings = settings if settings is not None else store.current(ticker, db_path=db_path)
    if not settings or spot is None:
        return None
    ex = dict(store.DEFAULT_EXIT)
    ex.update((settings.get("exit") or {}).get("params") or {})
    ip = settings.get("in_play")
    rvol = getattr(ctx, "rvol", None)
    thr = float(((ip or {}).get("params") or {}).get("rvol_min", 1.5))
    in_play = bool(ip and ip["status"] != store.OFF and rvol is not None and rvol >= thr)
    slot = "in_play" if in_play else "normal"
    s = settings.get(slot)
    out: dict[str, Any] = {
        "ticker": ticker.upper(), "as_of": now.isoformat(timespec="minutes"), "spot": spot,
        "in_play": in_play, "rvol": rvol, "rvol_min": thr, "slot": slot, "exit": ex,
        "settings_id": (s or {}).get("id"), "rule": (s or {}).get("rule"),
        "label": rules.RULES.get((s or {}).get("rule"), {}).get("label"),
        "status": (s or {}).get("status"), "params": (s or {}).get("params") or {},
        "signal": None, "spread": None, "waiting": None,
    }
    if not s or s["status"] == store.OFF:
        out["waiting"] = "No strategy is on for this kind of day."
        return out
    spec = rules.RULES.get(s["rule"], {})
    p = dict(spec.get("defaults") or {})
    p.update(s.get("params") or {})
    sig = rules.evaluate(s["rule"], s.get("params"), ctx) if ctx is not None else None
    if not sig:
        win = p.get("window", "")
        out["waiting"] = (f"No signal yet. {spec.get('label', s['rule'])} decides between "
                          f"{win.replace('-', ' and ')}; checked at every scan."
                          if win else "No signal yet.")
        return out
    out["signal"] = sig
    out["spread"] = spread_card(chain, sig["side"], spot, now, ex)
    if out["spread"] is None:
        out["waiting"] = ("Signal, but no spread passes the fill check (quotes missing or a "
                          f"leg wider than ${ex['max_leg_spread']:.2f}).")
    elif sig.get("stop_spot"):
        out["spread"]["stop_spot"] = sig["stop_spot"]
    return out


def telegram_lines(card: dict | None) -> list[str]:
    """Plain lines for the scan message (escaped by the caller)."""
    if not card or not card.get("rule"):
        return []
    badge = {"paper": "PAPER", "live": "LIVE", "off": "OFF"}.get(card.get("status"), "")
    head = f"Strategy: {card.get('label')} [{badge}]" + (" · in play" if card.get("in_play") else "")
    sp = card.get("spread")
    if not sp:
        return [head, card.get("waiting") or ""]
    leg = "C" if sp["side"] == "CALL" else "P"
    return [head,
            f"{sp['long_strike']:g}/{sp['short_strike']:g}{leg} {sp['expiry'][5:]} for "
            f"{sp['entry_cost']:.2f}: risk ${sp['risk']:.0f}, max ${sp['max_gain']:.0f}, "
            f"break-even {sp['break_even']:.2f}"
            + (f", {sp['p_profit'] * 100:.0f}% to profit" if sp.get("p_profit") else ""),
            f"Why: {card['signal']['reason']}"]
