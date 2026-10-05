"""Gamma read for a ticker from its latest full-chain snapshot (shared by the chart and
the Telegram push)."""

from __future__ import annotations

from datetime import datetime

import gex
import volume_history as vh
from ui.common import ET


def gamma_summary_for(ticker: str, spot) -> dict | None:
    """``gex.summary`` for the nearest live expiry of ``ticker``; None if unavailable."""
    try:
        spot_f = float(spot)
    except (TypeError, ValueError):
        return None
    latest = vh.latest_scan(str(ticker or "").upper())
    if latest.empty or spot_f <= 0:
        return None
    as_of = datetime.fromisoformat(str(latest["ts_et"].iloc[0]))
    return gex.summary(latest, spot=spot_f, as_of=as_of, today=datetime.now(ET).date())


def gamma_levels(summary: dict | None) -> list[dict]:
    """Price levels worth drawing on a chart: support, resistance and the put wall."""
    if not summary:
        return []
    out = []
    for key, name, color in (("resistance", "Call resistance", "#C084FC"),
                             ("support", "Gamma support", "#FACC15"),
                             ("put_wall", "Put wall", "#2DD4BF")):
        price = summary.get(key)
        if price is not None:
            out.append({"key": key, "name": name, "price": float(price), "color": color})
    return out
