"""Shared helpers so thin fixtures still meet min_pool_size (engine-v1.2)."""

from __future__ import annotations

from typing import Any

from config import SCORING
from scoring_pool import DEFAULT_MIN_POOL_SIZE, scoring_pool


def pad_min_pool(
    rows: list[dict[str, Any]],
    *,
    min_n: int | None = None,
    price: float = 2.0,
    vol: int = 5000,
    oi: int = 5000,
    iv: float = 0.30,
) -> list[dict[str, Any]]:
    """
    Append filler contracts so each present DTE pool has ≥ min_n survivors.

    Fillers use far strikes and do not replace the caller's rows. Same pad on
    both sides of a before/after comparison keeps relative assertions valid.
    """
    need = int(min_n if min_n is not None else SCORING.get(
        "min_pool_size", DEFAULT_MIN_POOL_SIZE,
    ))
    out = [dict(r) for r in rows]
    used = {float(r["strike"]) for r in out if r.get("strike") is not None}
    strike = 400.0

    # Count current membership per pool
    def _count(pool: str) -> int:
        return sum(1 for r in out if scoring_pool(r.get("dte")) == pool)

    pools_present = {scoring_pool(r.get("dte")) for r in out}
    pools_present.discard(None)

    for pool in sorted(pools_present):
        template = next(r for r in out if scoring_pool(r.get("dte")) == pool)
        dte = int(template.get("dte") or (0 if pool == "0DTE" else 28))
        expiry = str(template.get("expiry") or "2026-08-21")
        side = str(template.get("side") or "CALL")
        while _count(pool) < need:
            while strike in used:
                strike += 2.5
            used.add(strike)
            out.append({
                "side": side,
                "strike": float(strike),
                "expiry": expiry,
                "dte": dte,
                "last": float(price),
                "volume": int(vol),
                "openInterest": int(oi),
                "iv": float(iv),
            })
            strike += 2.5
    return out
