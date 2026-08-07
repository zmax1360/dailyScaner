#!/usr/bin/env python3
"""
orb_vwap_probe.py — does the VWAP-pullback / ORB-extension setup ever fire?

Runs against archive/*.json. Read-only: touches nothing else, changes no config,
writes no database rows. This is a feasibility probe, not a strategy.

    python orb_vwap_probe.py
    python orb_vwap_probe.py --ticker AAPL --ext 1.0 --touch-pct 0.15

WHAT IT MEASURES

The setup as described requires four things IN ORDER within one session:

    1. price breaks cleanly outside the 15M opening range
    2. price extends >= EXT x the range width beyond the break level
    3. no scan between (1) and (2) closes back through VWAP
    4. price later pulls back to TOUCH VWAP without closing through it

Then it records what happened next: did price resume toward the trend, and by
how much, at the last scan of the session.

WHAT IT CANNOT MEASURE — read this before believing any output

  * Your archives are ~5 minute snapshots, not candles. "no candle closed
    through VWAP" is approximated as "no SNAPSHOT was on the wrong side". A
    move that crossed VWAP and came back between two scans is invisible.
    This makes the filter LOOSER than the real rule, so the hit rate here is
    an UPPER BOUND.
  * A "touch" is a proximity band, not a wick. Same direction of error.
  * Outcome is measured to the last scan of the session, not to a target or
    stop. There is no exit rule here, so "worked" means only "price was
    further along the trend at the close".
  * No transaction costs, no spread, no slippage.

The number worth reading is the SETUP COUNT, not the win rate. If the setup
fires twice a week you cannot evaluate it in any reasonable time, and that
answers the question of whether to build it — regardless of how the small
sample happens to look.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import defaultdict


def load_sessions(pattern: str, ticker: str | None):
    """Group archive snapshots into (ticker, session_date) -> [snapshot, ...]."""
    sessions: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for path in sorted(glob.glob(pattern)):
        base = os.path.basename(path)
        parts = base.split("_")
        if len(parts) < 3:
            continue
        tkr, day = parts[0], parts[1]
        if ticker and tkr.upper() != ticker.upper():
            continue
        try:
            d = json.load(open(path))
        except (json.JSONDecodeError, OSError):
            continue
        d["_path"] = base
        sessions[(tkr, day)].append(d)
    for k in sessions:
        sessions[k].sort(key=lambda x: x.get("timestamp", ""))
    return sessions


def vwap_of(snap: dict) -> float | None:
    """VWAP is stored under different keys across archive versions."""
    for tf in ("5M", "10M", "15M"):
        v = (snap.get("timeframes") or {}).get(tf, {}).get("vwap")
        if v:
            return float(v)
    v = snap.get("vwap")
    return float(v) if v else None


def evaluate(snaps: list[dict], ext_mult: float, touch_pct: float):
    """Walk one session in order. Return a result dict or None if no ORB."""
    orb = None
    for s in snaps:
        o = (s.get("or_data") or {}).get("15M")
        if o and (o.get("range") or 0) > 0 and o.get("bias_dir") != "forming":
            orb = o
            break
    if not orb:
        return None

    hi, lo, rng = float(orb["high"]), float(orb["low"]), float(orb["range"])
    res = {"orb_high": hi, "orb_low": lo, "orb_range": rng,
           "stage": "no_break", "direction": None}

    # ── stage 1: first clean break ───────────────────────────────────────────
    brk_i = brk_dir = None
    for i, s in enumerate(snaps):
        spot = s.get("spot")
        if spot is None:
            continue
        spot = float(spot)
        if spot > hi:
            brk_i, brk_dir = i, "long"
            break
        if spot < lo:
            brk_i, brk_dir = i, "short"
            break
    if brk_i is None:
        return res
    res.update(stage="break_only", direction=brk_dir, break_idx=brk_i)

    # ── stage 2: extension of ext_mult x range beyond the break level ────────
    target = hi + ext_mult * rng if brk_dir == "long" else lo - ext_mult * rng
    ext_i = None
    for i in range(brk_i, len(snaps)):
        spot = snaps[i].get("spot")
        if spot is None:
            continue
        spot = float(spot)
        if (brk_dir == "long" and spot >= target) or \
           (brk_dir == "short" and spot <= target):
            ext_i = i
            break
    if ext_i is None:
        return res
    res.update(stage="extended", ext_idx=ext_i, target=target)

    # ── stage 3: VWAP integrity between break and extension ──────────────────
    # APPROXIMATION: snapshot-level, not candle-close. See module docstring.
    for i in range(brk_i, ext_i + 1):
        spot, vw = snaps[i].get("spot"), vwap_of(snaps[i])
        if spot is None or vw is None:
            continue
        spot = float(spot)
        if (brk_dir == "long" and spot < vw) or (brk_dir == "short" and spot > vw):
            res["stage"] = "vwap_violated"
            return res
    res["stage"] = "integrity_ok"

    # ── stage 4: pullback that TOUCHES vwap without closing through ──────────
    touch_i = None
    for i in range(ext_i + 1, len(snaps)):
        spot, vw = snaps[i].get("spot"), vwap_of(snaps[i])
        if spot is None or vw is None:
            continue
        spot, band = float(spot), abs(vw) * touch_pct / 100.0
        if abs(spot - vw) <= band:
            touch_i = i
            break
        if (brk_dir == "long" and spot < vw - band) or \
           (brk_dir == "short" and spot > vw + band):
            res["stage"] = "broke_vwap_before_touch"
            return res
    if touch_i is None:
        res["stage"] = "no_pullback"
        return res

    # ── outcome: entry at touch, measured to the LAST scan of the session ────
    entry = float(snaps[touch_i]["spot"])
    last = None
    for s in reversed(snaps):
        if s.get("spot") is not None:
            last = float(s["spot"])
            break
    if last is None:
        res["stage"] = "no_close"
        return res

    move = (last - entry) if brk_dir == "long" else (entry - last)
    res.update(stage="SETUP", touch_idx=touch_i, entry=entry, close=last,
               move=move, move_pct=move / entry, win=move > 0,
               n_scans=len(snaps))
    return res


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pattern", default="archive/*.json")
    p.add_argument("--ticker")
    p.add_argument("--ext", type=float, default=1.0,
                   help="range multiples required beyond the break")
    p.add_argument("--touch-pct", type=float, default=0.15,
                   help="VWAP touch band, percent of price")
    a = p.parse_args()

    sessions = load_sessions(a.pattern, a.ticker)
    if not sessions:
        print("no archives matched")
        return

    stages: dict[str, int] = defaultdict(int)
    setups = []
    for (tkr, day), snaps in sorted(sessions.items()):
        r = evaluate(snaps, a.ext, a.touch_pct)
        if r is None:
            stages["no_orb"] += 1
            continue
        stages[r["stage"]] += 1
        if r["stage"] == "SETUP":
            setups.append((tkr, day, r))

    n = len(sessions)
    print("=" * 62)
    print(f"  ORB / VWAP PULLBACK PROBE — {n} ticker-sessions")
    print(f"  ext={a.ext}x range   touch band={a.touch_pct}%"
          f"{'   ticker=' + a.ticker.upper() if a.ticker else ''}")
    print("=" * 62)

    print("\n  FUNNEL")
    order = ["no_orb", "no_break", "break_only", "extended", "vwap_violated",
             "integrity_ok", "broke_vwap_before_touch", "no_pullback",
             "no_close", "SETUP"]
    for s in order:
        if stages.get(s):
            print(f"    {s:<26} {stages[s]:>4}  {stages[s]/n:>5.1%}")

    ns = len(setups)
    print(f"\n  QUALIFYING SETUPS: {ns} of {n} sessions ({ns/n:.1%})")
    if not ns:
        print("\n  The setup never fired. That is the answer — do not build it.")
        return

    wins = sum(1 for _, _, r in setups if r["win"])
    moves = [r["move_pct"] for _, _, r in setups]
    print(f"  win rate (price further along trend at close): {wins}/{ns} = {wins/ns:.0%}")
    print(f"  mean move {sum(moves)/len(moves):+.2%}   "
          f"best {max(moves):+.2%}   worst {min(moves):+.2%}")

    print("\n  SETUPS")
    for tkr, day, r in setups:
        print(f"    {tkr:<5} {day}  {r['direction']:<5} "
              f"entry {r['entry']:.2f} -> close {r['close']:.2f}  "
              f"{r['move_pct']:+.2%}")

    print("\n  " + "-" * 58)
    print("  Read the SETUP COUNT, not the win rate. At this frequency you")
    print("  would need months to evaluate the edge. Snapshot data also makes")
    print("  the VWAP-integrity filter looser than the real rule, so this is")
    print("  an UPPER BOUND on how often it fires.")


if __name__ == "__main__":
    main()
