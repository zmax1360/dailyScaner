"""Lookups for the newest scan and weekly archives of a ticker."""

from __future__ import annotations

import glob
import json
import os



def _latest_archive_stamp(ticker: str) -> str | None:
    """Stable fingerprint of the newest archive for auto-refresh detection."""
    files = sorted(glob.glob(f"archive/{ticker}_*.json"), reverse=True)
    if not files:
        return None
    path = files[0]
    try:
        return f"{os.path.basename(path)}|{os.path.getmtime(path):.3f}"
    except OSError:
        return os.path.basename(path)


def _latest_weekly_archive(ticker: str) -> dict:
    files = sorted(glob.glob(f"archive_weekly/{ticker}_*.json"), reverse=True)
    if not files:
        return {}
    try:
        with open(files[0]) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _latest_archive(ticker: str = "AAPL") -> dict | None:
    files = sorted(glob.glob(f"archive/{ticker}_*.json"), reverse=True)
    if not files:
        return None
    try:
        with open(files[0]) as f:
            return json.load(f)
    except Exception:
        return None
