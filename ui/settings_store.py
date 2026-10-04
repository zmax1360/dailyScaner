"""Saved UI settings on disk, so they survive a browser refresh or an app restart.

One small JSON file under data/ (which is not in version control). Writes are locked
and atomic (safe_io), like the other local state files.
"""

from __future__ import annotations

import json
import os
from typing import Any

from safe_io import locked, write_json_atomic
from ui.services import _SCANNER_DIR

PATH = os.path.join(_SCANNER_DIR, "data", "ui_settings.json")


def load(path: str | None = None) -> dict[str, Any]:
    """Saved settings, or {} when the file is missing, unreadable or not an object."""
    path = path or PATH
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(values: dict[str, Any], path: str | None = None) -> None:
    path = path or PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with locked(path):
        write_json_atomic(path, dict(values))
