"""Gamma levels for the chart, from the gamma read the scan saved (``saved_gamma``)."""

from __future__ import annotations

from plan_report import saved_gamma  # noqa: F401  (the scan's saved gamma read)


def gamma_levels(summary: dict | None) -> list[dict]:
    """Price levels worth drawing on a chart: support, resistance, the put wall, and the
    nearest expiry's gamma flip (in snapshots saved since it existed)."""
    if not summary:
        return []
    out = []
    for key, name, color in (("resistance", "Call resistance", "#C084FC"),
                             ("support", "Gamma support", "#FACC15"),
                             ("put_wall", "Put wall", "#2DD4BF"),
                             ("gamma_flip_nearest", "Gamma flip", "#FB923C")):
        price = summary.get(key)
        if price is not None:
            out.append({"key": key, "name": name, "price": float(price), "color": color})
    return out
