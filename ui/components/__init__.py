"""Self-contained panels. Each module exposes ``TITLE`` and ``render(ctx: ScanContext)``.

A component knows nothing about the page it sits on, so any page can place it.
"""

from __future__ import annotations

from ui.components import cost_distribution, expiry_breakdown, flow_magnets

# menu page id -> component module
REGISTRY = {
    "flow_magnets": flow_magnets,
    "expiry_breakdown": expiry_breakdown,
    "cost_distribution": cost_distribution,
}
