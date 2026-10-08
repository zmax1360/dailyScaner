"""game_plan — turn one scan's numbers into a plan, by fixed rules.

Pure: no network, no Streamlit, no writes, no AI. Same inputs, same plan. Display only;
nothing here feeds scoring.

    allowed side   <- the 15-minute EMA 9/21/50 trend rule (ema_stack)
    type of day    <- the sign of net gamma exposure for the nearest expiry (gex.summary)
    levels         <- gamma support / resistance / put wall, VWAP, 1SD expected range
    candidates     <- the top-ranked Best Value pick on the allowed side, one for each
                      DTE pool (1DTE+ and 0DTE; the pools are never mixed)
    trade plan     <- for the allowed side: stop under the nearest support (above the
                      nearest resistance for puts), targets from session high or low, the
                      gamma level and the 1SD range edge, scale-out, time exit, and the
                      reward against the risk from the current price
    what changes it

A missing input is reported as unknown or left out. It is never guessed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

import ema_stack
from scoring_pool import POOL_0DTE, POOL_1DTE
from time_stop import TIME_STOP_CAP, window_minutes_for_dte

CALLS, PUTS, STAND_ASIDE, UNKNOWN = "CALLS", "PUTS", "STAND_ASIDE", "UNKNOWN"
POSITIVE, NEGATIVE = "POSITIVE", "NEGATIVE"

SIDE_LABEL = {CALLS: "Calls only", PUTS: "Puts only", STAND_ASIDE: "Stand aside",
              UNKNOWN: "Unknown"}
REGIME_LABEL = {POSITIVE: "Positive gamma", NEGATIVE: "Negative gamma", UNKNOWN: "Unknown"}
_OPTION_SIDE = {CALLS: "CALL", PUTS: "PUT"}
POOLS = (POOL_1DTE, POOL_0DTE)          # shown in this order

STOP_BUFFER_PCT = 0.10                  # the stop sits this far beyond the level, in percent
MIN_STOP_DISTANCE_PCT = 0.15            # the stop is never closer to the price than this
MAX_STOP_DISTANCE_PCT = 1.00            # a level farther than this is too far: no trade plan
SCALE_OUT = (0.30, 0.40, 0.30)          # share closed at targets 1, 2 and 3 (2+ contracts)
MERGE_WITHIN_PCT = 0.05                 # targets this close together count as one
MIN_REWARD_TO_RISK = 1.5                # the entry zone ends where reward/risk drops below this
DELTA_BAND = (0.35, 0.50)               # the pre-trade |delta| band (same as Best Value's table)


@dataclass(frozen=True)
class Level:
    name: str
    price: float
    kind: str            # resistance | support | put_wall | flip | vwap | ema | range | spot


@dataclass(frozen=True)
class Candidate:
    pool: str
    pick: dict[str, Any] | None
    note: str


@dataclass(frozen=True)
class Target:
    name: str
    price: float
    share: float                         # fraction of the position closed here
    reward: float                        # distance from the entry price, always positive


@dataclass(frozen=True)
class TradePlan:
    side: str                            # CALLS or PUTS
    ready: bool                          # False when a stop or a target cannot be set
    entry: float | None                  # the current price
    better_entry: Level | None           # the level the stop is anchored to
    stop: float | None
    stop_note: str
    targets: tuple[Target, ...]
    risk: float | None
    reward_to_risk: float | None         # to the first target, entering at the current price
    entry_limit: float | None            # calls: enter at or below; puts: at or above
    in_zone: bool                        # the current price is inside the entry zone
    min_reward_to_risk: float
    poor: bool                           # True when the current price is outside the zone
    time_exit: str
    single_contract: str
    notes: tuple[str, ...]
    other_side: str
    cautions: tuple[str, ...] = ()       # facts worth knowing that do not block the plan


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
    candidates: tuple[Candidate, ...]
    expect: tuple[str, ...]
    changes: tuple[str, ...]
    missing: tuple[str, ...] = field(default_factory=tuple)
    trade: TradePlan | None = None
    price_note: str = ""                 # where the current price came from, and when

    def candidate_for(self, pool: str) -> Candidate | None:
        return next((c for c in self.candidates if c.pool == pool), None)


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
                 expected: dict | None = None, emas: dict | None = None) -> tuple[Level, ...]:
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
    add("15-minute EMA 21", (emas or {}).get("ema21"), "ema")
    add("15-minute EMA 50", (emas or {}).get("ema50"), "ema")
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


def pick_candidate(picks: pd.DataFrame | None, side: str,
                   pool: str = POOL_1DTE) -> dict[str, Any] | None:
    """Highest-scoring ranked contract of ``pool`` on the allowed side, or None."""
    want = _OPTION_SIDE.get(side)
    if want is None or picks is None or getattr(picks, "empty", True):
        return None
    need = {"side", "pool", "Value_Score", "strike", "expiry"}
    if not need <= set(picks.columns):
        return None
    df = picks[(picks["side"].astype(str).str.upper() == want)
               & (picks["pool"] == pool) & picks["Value_Score"].notna()]
    if df.empty:
        return None
    b = df.sort_values("Value_Score", ascending=False).iloc[0]
    oi = _f(b.get("openInterest"))
    vol = _f(b.get("volume"))
    return {
        "pool": pool,
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


def _time_exit_text() -> str:
    return (f"0DTE: {window_minutes_for_dte(0)} minutes after entry. "
            f"1DTE+: {window_minutes_for_dte(1)} minutes after entry. "
            f"Never past {TIME_STOP_CAP:%H:%M} ET.")


def _merge_targets(found: list[tuple[str, float]], ascending: bool) -> list[tuple[str, float]]:
    """Order the targets from nearest to farthest and merge any that sit together."""
    found = sorted(found, key=lambda t: t[1], reverse=not ascending)
    out: list[tuple[str, float]] = []
    for name, price in found:
        if out and abs(price - out[-1][1]) <= out[-1][1] * MERGE_WITHIN_PCT / 100.0:
            out[-1] = (f"{out[-1][0]} / {name}", out[-1][1])
        else:
            out.append((name, price))
    return out


def _shares(n: int, scale_out: tuple[float, float, float]) -> tuple[float, ...]:
    """Scale-out for n targets; the last one always closes whatever is left."""
    if n <= 0:
        return ()
    if n == 1:
        return (1.0,)
    if n == 2:
        return (scale_out[0], round(1.0 - scale_out[0], 10))
    return tuple(scale_out)


def build_trade_plan(side: str, *, spot: Any, gamma: dict | None = None, vwap: Any = None,
                     expected: dict | None = None, session_high: Any = None,
                     session_low: Any = None, stop_buffer_pct: float = STOP_BUFFER_PCT,
                     scale_out: tuple[float, float, float] = SCALE_OUT,
                     min_reward_to_risk: float = MIN_REWARD_TO_RISK,
                     emas: dict | None = None) -> TradePlan | None:
    """The plan for the allowed side, in prices of the stock (not the option premium).

    Calls: stop under the nearest support below price, chosen from VWAP, the 15-minute
    EMA 21 and EMA 50 (the lines the trend rule rests on) and gamma support. The stop is
    never closer to the price than MIN_STOP_DISTANCE_PCT. If the nearest support is
    farther than MAX_STOP_DISTANCE_PCT there is no plan, because that is not a stop.
    Targets above price: VWAP (when price is below it), session high, call resistance
    and the expected range high.
    Puts mirror it with resistance above price and targets below.

    Entry zone: from the stop's level to the price where the reward to target 1 is still
    ``min_reward_to_risk`` times the risk to the stop. Beyond that price the plan says wait.
    None when no side is allowed. A missing level is left out, never assumed.
    """
    if side not in (CALLS, PUTS):
        return None
    px = _f(spot)
    g, ex = gamma or {}, expected or {}
    calls = side == CALLS
    other = (f"{'Puts' if calls else 'Calls'}: not allowed today under the trend rule. They "
             f"become allowed only if the 15-minute EMAs stack "
             f"{'9 below 21 below 50' if calls else '9 above 21 above 50'}.")
    base = dict(side=side, entry=px, time_exit=_time_exit_text(), other_side=other,
                single_contract="With one contract, exit all of it at target 1.",
                min_reward_to_risk=float(min_reward_to_risk))

    def unready(note: str) -> TradePlan:
        return TradePlan(ready=False, better_entry=None, stop=None, stop_note=note,
                         targets=(), risk=None, reward_to_risk=None, entry_limit=None,
                         in_zone=False, poor=False, notes=(note,), **base)

    if px is None or px <= 0:
        return unready("No current price, so no stop or target can be set.")

    e = emas or {}
    v = _f(vwap)
    ema_levels = [("15-minute EMA 21", _f(e.get("ema21")), "ema"),
                  ("15-minute EMA 50", _f(e.get("ema50")), "ema")]
    cautions: list[str] = []
    if calls:
        anchors = [("VWAP", v, "vwap"), *ema_levels,
                   ("Gamma support", _f(g.get("support")), "support")]
        anchors = [a for a in anchors if a[1] is not None and a[1] < px]
        goals = [("VWAP", v), ("Session high", _f(session_high)),
                 ("Call resistance", _f(g.get("resistance"))),
                 ("Expected range high", _f(ex.get("Upper_1SD")))]
        goals = [t for t in goals if t[1] is not None and t[1] > px]
        if v is not None and v > px:
            cautions.append(f"Price is below VWAP at {_usd(round(v, 2))}: buyers have not "
                            "taken back the day's average price, and VWAP is the first "
                            "resistance overhead.")
    else:
        anchors = [("VWAP", v, "vwap"), *ema_levels,
                   ("Call resistance", _f(g.get("resistance")), "resistance")]
        anchors = [a for a in anchors if a[1] is not None and a[1] > px]
        goals = [("VWAP", v), ("Session low", _f(session_low)),
                 ("Gamma support", _f(g.get("support"))),
                 ("Expected range low", _f(ex.get("Lower_1SD")))]
        goals = [t for t in goals if t[1] is not None and t[1] < px]
        if v is not None and v < px:
            cautions.append(f"Price is above VWAP at {_usd(round(v, 2))}: sellers have not "
                            "taken back the day's average price, and VWAP is the first "
                            "support underneath.")
    base["cautions"] = tuple(cautions)

    kind_word = "support level below" if calls else "resistance level above"
    if not anchors:
        return unready(f"No {kind_word} the current price to anchor a stop to.")

    def dist_pct(level: float) -> float:
        return abs(px - level) / px * 100.0

    name, level, kind = (max(anchors, key=lambda a: a[1]) if calls
                         else min(anchors, key=lambda a: a[1]))
    if dist_pct(level) > MAX_STOP_DISTANCE_PCT:
        side_word = "support" if calls else "resistance"
        return unready(
            f"The nearest {side_word} is {name} at {_usd(level)}, "
            f"{dist_pct(level):.1f}% from the current price. That is too far to be a stop "
            f"(the limit is {MAX_STOP_DISTANCE_PCT:g}%). No {'calls' if calls else 'puts'} "
            f"entry until price sits nearer a {side_word} level.")
    buffer = level * stop_buffer_pct / 100.0
    floor = px * MIN_STOP_DISTANCE_PCT / 100.0          # keep the stop out of the noise
    stop = min(level - buffer, px - floor) if calls else max(level + buffer, px + floor)
    widened = (stop < level - buffer) if calls else (stop > level + buffer)
    risk = abs(px - stop)
    stop_note = (f"{stop_buffer_pct:g}% {'under' if calls else 'above'} {name} at {_usd(level)}, "
                 f"the nearest {'support below' if calls else 'resistance above'} price."
                 + (f" Widened to stay {MIN_STOP_DISTANCE_PCT:g}% away from the price."
                    if widened else ""))
    anchor = Level(name, level, kind)

    merged = _merge_targets(goals, ascending=calls)
    if not merged:
        return TradePlan(ready=False, better_entry=anchor, stop=stop, stop_note=stop_note,
                         targets=(), risk=risk, reward_to_risk=None, entry_limit=None,
                         in_zone=False, poor=False,
                         notes=("No target " + ("above" if calls else "below")
                                + " the current price among session "
                                + ("high" if calls else "low")
                                + ", the gamma level and the expected range.",), **base)
    targets = tuple(Target(n, p, share, abs(p - px))
                    for (n, p), share in zip(merged, _shares(len(merged), scale_out)))
    rr = targets[0].reward / risk if risk > 0 else None
    # Price at which reward to target 1 equals min_reward_to_risk times the risk to the
    # stop. Calls must enter at or below it, puts at or above it.
    k = float(min_reward_to_risk)
    limit = (targets[0].price + k * stop) / (1.0 + k)
    in_zone = px <= limit if calls else px >= limit
    notes = []
    if not in_zone:
        notes.append(
            f"Price is {'above' if calls else 'below'} the entry zone. At {_usd(round(px, 2))} "
            f"the reward to target 1 is {rr:.1f} times the risk to the stop, and the plan "
            f"needs {k:g}. Wait for {_usd(round(limit, 2))} or "
            f"{'lower' if calls else 'higher'}."
        )
    return TradePlan(ready=True, better_entry=anchor, stop=stop, stop_note=stop_note,
                     targets=targets, risk=risk, reward_to_risk=rr, entry_limit=limit,
                     in_zone=in_zone, poor=not in_zone, notes=tuple(notes), **base)


def _delta_note(pick: dict[str, Any]) -> str:
    """Say so when the pick is outside the pre-trade delta band (it stays the pick)."""
    d = _f(pick.get("delta"))
    lo, hi = DELTA_BAND
    if d is None:
        return " It has no delta, so it cannot be checked against the pre-trade band."
    if lo <= abs(d) <= hi:
        return ""
    return (f" Its delta is {abs(d):.2f}, outside the {lo:.2f} to {hi:.2f} pre-trade band"
            + (": a low-probability contract." if abs(d) < lo else "."))


def build_plan(*, ticker: str, spot: Any, trend: dict | None, gamma: dict | None = None,
               vwap: Any = None, expected: dict | None = None,
               picks: pd.DataFrame | None = None, session_high: Any = None,
               session_low: Any = None,
               stop_buffer_pct: float = STOP_BUFFER_PCT,
               min_reward_to_risk: float = MIN_REWARD_TO_RISK,
               price_note: str = "", emas: dict | None = None) -> Plan:
    side, side_reason = side_from_trend(trend)
    regime, regime_note, net = regime_from_gamma(gamma)
    candidates = []
    for pool in POOLS:
        pick = pick_candidate(picks, side, pool)
        if side not in (CALLS, PUTS):
            note = "No candidate while no side is allowed."
        elif pick is None:
            note = f"No ranked {pool} {'call' if side == CALLS else 'put'} in this scan."
        else:
            note = f"Top-ranked {pool} Best Value pick on the allowed side." + _delta_note(pick)
        candidates.append(Candidate(pool, pick, note))
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
        levels=build_levels(_f(spot), gamma, vwap, expected, emas),
        candidates=tuple(candidates),
        expect=_expect(side, regime, gamma), changes=_changes(side, regime, gamma, stale),
        missing=missing,
        trade=build_trade_plan(side, spot=spot, gamma=gamma, vwap=vwap, expected=expected,
                               session_high=session_high, session_low=session_low,
                               stop_buffer_pct=stop_buffer_pct,
                               min_reward_to_risk=min_reward_to_risk, emas=emas),
        price_note=price_note,
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


def plan_lines(plan: Plan, compact: bool = False) -> list[str]:
    """Plain lines for a Telegram (HTML) message. ``compact`` leaves out the levels ladder
    and the "what changes" notes, for a message that carries other sections too (Telegram
    cuts a message off at 4,096 characters)."""
    lines = [
        f"🎯 <b>GAME PLAN · {plan.ticker}</b>",
        f"Side: <b>{SIDE_LABEL[plan.side]}</b>" + (" (scan is stale)" if plan.stale else ""),
        f"Day: <b>{REGIME_LABEL[plan.regime]}</b>",
    ] + [f"{c.pool}: {candidate_text(c.pick)}" for c in plan.candidates]
    lines += trade_lines(plan.trade)
    if compact:
        return lines
    ladder = [f"{lv.name} {_usd(lv.price)}" for lv in plan.levels]
    if ladder:
        lines.append("Levels: " + " &gt; ".join(ladder))
    lines += [f"• {text}" for text in plan.changes]
    return lines


def trade_lines(trade: TradePlan | None) -> list[str]:
    """Compact trade-plan lines for a Telegram (HTML) message."""
    if trade is None:
        return []
    word = "Calls" if trade.side == CALLS else "Puts"
    if not trade.ready:
        return [f"{word} plan: {trade.notes[0]}"]
    out = [f"{word} plan: enter at or {'below' if trade.side == CALLS else 'above'} "
           f"<b>{_usd(round(trade.entry_limit, 2))}</b>"
           + ("" if trade.in_zone else f" (now {_usd(round(trade.entry, 2))}: wait)"),
           f"Stop <b>{_usd(round(trade.stop, 2))}</b> "
           f"({trade.better_entry.name} {_usd(trade.better_entry.price)})"]
    out.append("Targets: " + " · ".join(
        f"{_usd(t.price)} {t.name} ({t.share:.0%})" for t in trade.targets))
    if trade.reward_to_risk is not None:
        out.append(f"Reward to risk from here: <b>{trade.reward_to_risk:.1f}</b> "
                   f"(needs {trade.min_reward_to_risk:g})")
    return out
