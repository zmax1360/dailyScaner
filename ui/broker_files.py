"""Where broker activity exports live, and which one is newest."""

from __future__ import annotations

import os

from ui.services import _SCANNER_DIR

BROKER_DIR = os.path.join(_SCANNER_DIR, "data", "broker")


def latest_export(folder: str | None = None) -> str | None:
    """Newest .csv in the broker folder, or None."""
    folder = folder or BROKER_DIR
    try:
        files = [os.path.join(folder, f) for f in os.listdir(folder) if f.lower().endswith(".csv")]
    except OSError:
        return None
    return max(files, key=os.path.getmtime) if files else None
