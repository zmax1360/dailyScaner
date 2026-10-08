"""Gamma read for a ticker from its latest full-chain snapshot (shared by the chart and
the Telegram push)."""

from __future__ import annotations

from plan_report import gamma_summary_for  # noqa: F401  (shared with the Telegram bot)


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
