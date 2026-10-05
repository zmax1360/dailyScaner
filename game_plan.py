"""game_plan — turn one scan's numbers into a plan, by fixed rules.

Pure: no network, no Streamlit, no writes, no AI. Same inputs, same plan. Display only;
nothing here feeds scoring.

    allowed side   <- the 15-minute EMA 9/21/50 trend rule (ema_stack)
    type of day    <- the sign of net gamma exposure for the nearest expiry (gex.summary)
    levels         <- gamma support / resistance / put wall, VWAP, 1SD expected range
    candidate      <- the top-ranked 1DTE+ Best Value pick on the allowed side
    what changes it

A missing input is reported as unknown or left out. It is never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

import ema_stack
from scoring_pool import POOL_1DTE

CALLS, PUTS, STAND_ASIDE, UNKNOWN = "CALLS", "PUTS", "STAND_ASIDE", "UNKNOWN"
POSITIVE, NEGATIVE = "POSITIVE", "NEGATIVE"

SIDE_LABEL = {CALLS: "Calls only", PUTS: "Puts only", STAND_ASIDE: "Stand aside",
              UNKNOWN: "Unknown"}
REGIME_LABEL = {POSITIVE: "Positive gamma", NEGATIVE: "Negative gamma", UNKNOWN: "Unknown"}
_OPTION_SIDE = {CALLS: "CALL", PUTS: "PUT"}


@dataclass(frozen=True)
class Level:
    name: str
    price: float
    kind: str            # resistance | support | put_wall | flip | vwap | range | spot


@dataclass(frozen=True)
class Plan:
    ticker: str
    spot: float | None
    as_of: datetime | None
    stale: bool
    side: str
    side_reason: str
    regime: str
    regime_note: str
    net_gex: float | None
    gamma_expiry: str | None
    levels: tuple[Level, ...]
    candidate: dict[str, Any] | None
    candidate_note: str
    expect: tuple[str, ...]
    changes: tuple[str, ...]
    missing: tuple[str, ...] = field(default_factory=tuple)


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _usd(v: float) -> str:
    return f"${v:,.2f}".rstrip("0").rstrip(".") if v != int(v) else f"${int(v):,}"


def side_from_trend(trend: dict | None) -> tuple[str, str]:
    state = (trend or {}).get("state")
    reason = str((trend or {}).get("reason") or "")
    if state == ema_stack.BULL:
        return CALLS, reason
    if state == ema_stack.BEAR:
        return PUTS, reason
    if state == ema_stack.NO_TRADE:
        return STAND_ASIDE, reason
    return UNKNOWN, reason or "No 15-minute EMA data in this scan."


def regime_from_gamma(gamma: dict | None) -> tuple[str, str, float | None]:
    net = _f((gamma or {}).get("net"))
    if net is None:
        return UNKNOWN, "No usable gamma snapshot.", None
    if net >= 0:
        return (POSITIVE, "Dealer hedging tends to dampen moves: expect a contained range "
                          "and modest targets.", net)
    return (NEGATIVE, "Dealer hedging tends to amplify moves: expect wider swings, and "
                      "trends that can keep going.", net)


def build_levels(spot: float | None, gamma: dict | None, vwap: Any = None,
                 expected: dict | None = None) -> tuple[Level, ...]:
    """Every level with a price, highest first, with spot placed among them. Levels that
    share a price are merged into one row."""
    raw: list[Level] = []

    def add(name, price, kind):
        p = _f(price)
        if p is not None and p > 0:
            raw.append(Level(name, p, kind))

    g = gamma or {}
    add("Call resistance", g.get("resistance"), "resistance")
    add("Gamma support", g.get("support"), "support")
    add("Put wall", g.get("put_wall"), "put_wall")
    add("First negative strike", g.get("first_negative_below_spot"), "flip")
    add("VWAP", vwap, "vwap")
    add("Expected range high", (expected or {}).get("Upper_1SD"), "range")
    add("Expected range low", (expected or {}).get("Lower_1SD"), "range")
    add("Spot", spot, "spot")

    merged: dict[float, Level] = {}
    for lv in raw:
        key = round(lv.price, 2)
        if key in merged and lv.kind != "spot" and merged[key].kind != "spot":
            old = merged[key]
            merged[key] = Level(f"{old.name} / {lv.name}", old.price, old.kind)
        elif key in merged:                      # spot sharing a price keeps its own row
            merged[key + (0.001 if lv.kind == "spot" else -0.001)] = lv
        else:
            merged[key] = lv
    return tuple(sorted(merged.values(), key=lambda lv: (-lv.price, lv.kind != "spot")))


def pick_candidate(picks: pd.DataFrame | None, side: str) -> dict[str, Any] | None:
    """Highest-scoring ranked 1DTE+ contract on the allowed side, or None."""
    want = _OPTION_SIDE.get(side)
    if want is None or picks is None or getattr(picks, "empty", True):
        return None
    need = {"side", "pool", "Value_Score", "strike", "expiry"}
    if not need <= set(picks.columns):
        return None
    df = picks[(picks["side"].astype(str).str.upper() == want)
               & (picks["pool"] == POOL_1DTE) & picks["Value_Score"].notna()]
    if df.empty:
        return None
    b = df.sort_values("Value_Score", ascending=False).iloc[0]
    oi = _f(b.get("openInterest"))
    vol = _f(b.get("volume"))
    return {
        "side": want, "strike": float(b["strike"]), "expiry": str(b["expiry"])[:10],
        "dte": None if _f(b.get("dte")) is None else int(b["dte"]),
        "last": _f(b.get("last")), "score": float(b["Value_Score"]),
        "delta": _f(b.get("delta")),
        "vol_oi": None if not oi or vol is None else vol / oi,
    }


def _expect(side: str, regime: str, gamma: dict | None) -> tuple[str, ...]:
    g = gamma or {}
    sup, res = _f(g.get("support")), _f(g.get("resistance"))
    out: list[str] = []
    if side == STAND_ASIDE:
        return ("The 15-minute EMAs are not stacked, so the trend rule allows no trade.",)
    if side == UNKNOWN:
        return ("The trend rule has no data, so no side is allowed yet.",)
    word = "calls" if side == CALLS else "puts"
    if regime == POSITIVE:
        out.append(f"The trend rule allows {word} only, on a day when moves tend to be contained.")
        if side == CALLS and sup is not None:
            out.append(f"Dips toward gamma support at {_usd(sup)} tend to be bought.")
        if side == CALLS and res is not None:
            out.append(f"Rallies tend to stall near call resistance at {_usd(res)}.")
        if side == PUTS and res is not None:
            out.append(f"Rallies toward call resistance at {_usd(res)} tend to stall.")
        if side == PUTS and sup is not None:
            out.append(f"Declines tend to slow near gamma support at {_usd(sup)}.")
    elif regime == NEGATIVE:
        out.append(f"The trend rule allows {word} only, on a day when moves tend to be amplified.")
        out.append("Trends can extend past the usual levels, and swings are wider both ways.")
    else:
        out.append(f"The trend rule allows {word} only. The type of day is unknown "
                   "(no gamma snapshot).")
    return tuple(out)


def _changes(side: str, regime: str, gamma: dict | None, stale: bool) -> tuple[str, ...]:
    out: list[str] = []
    if side in (CALLS, PUTS):
        out.append("The 15-minute EMAs lose their stack: the allowed side changes or goes "
                   "to stand aside.")
    elif side == STAND_ASIDE:
        out.append("The 15-minute EMAs line up 9 above 21 above 50, or the reverse: a side "
                   "becomes allowed.")
    flip = _f((gamma or {}).get("first_negative_below_spot"))
    if regime == POSITIVE and flip is not None:
        out.append(f"Price falls to {_usd(flip)} or below: the map turns negative there and "
                   "moves can speed up.")
    if regime == NEGATIVE:
        out.append("Net gamma turns positive: moves tend to be contained again.")
    if stale:
        out.append("This scan is over 30 minutes old. Re-check after the next scan.")
    return tuple(out)


def build_plan(*, ticker: str, spot: Any, trend: dict | None, gamma: dict | None = None,
               vwap: Any = None, expected: dict | None = None,
               picks: pd.DataFrame | None = None) -> Plan:
    side, side_reason = side_from_trend(trend)
    regime, regime_note, net = regime_from_gamma(gamma)
    candidate = pick_candidate(picks, side)
    if side not in (CALLS, PUTS):
        note = "No candidate while no side is allowed."
    elif candidate is None:
        note = f"No ranked 1DTE+ {'call' if side == CALLS else 'put'} in this scan."
    else:
        note = "Top-ranked 1DTE+ Best Value pick on the allowed side."
    stale = bool((trend or {}).get("stale"))
    missing = tuple(name for name, absent in (
        ("trend", side == UNKNOWN), ("gamma", gamma is None),
        ("VWAP", _f(vwap) is None),
        ("expected range", _f((expected or {}).get("Upper_1SD")) is None),
    ) if absent)
    return Plan(
        ticker=str(ticker or "").upper(), spot=_f(spot), as_of=(trend or {}).get("as_of"),
        stale=stale, side=side, side_reason=side_reason, regime=regime,
        regime_note=regime_note, net_gex=net, gamma_expiry=(gamma or {}).get("expiry"),
        levels=build_levels(_f(spot), gamma, vwap, expected),
        candidate=candidate, candidate_note=note,
        expect=_expect(side, regime, gamma), changes=_changes(side, regime, gamma, stale),
        missing=missing,
    )


def candidate_text(c: dict[str, Any] | None) -> str:
    if not c:
        return "None"
    parts = [f"{c['side']} {_usd(c['strike'])} {c['expiry']}"]
    if c.get("dte") is not None:
        parts[0] += f" ({c['dte']}d)"
    if c.get("last") is not None:
        parts.append(f"{_usd(c['last'])}")
    parts.append(f"score {c['score']:.2f}")
    return " · ".join(parts)


def plan_lines(plan: Plan) -> list[str]:
    """Plain lines for a Telegram (HTML) message."""
    lines = [
        f"🎯 <b>GAME PLAN · {plan.ticker}</b>",
        f"Side: <b>{SIDE_LABEL[plan.side]}</b>" + (" (scan is stale)" if plan.stale else ""),
        f"Day: <b>{REGIME_LABEL[plan.regime]}</b>",
        f"Candidate: {candidate_text(plan.candidate)}",
    ]
    ladder = [f"{lv.name} {_usd(lv.price)}" for lv in plan.levels]
    if ladder:
        lines.append("Levels: " + " &gt; ".join(ladder))
    lines += [f"• {text}" for text in plan.changes]
    return lines
