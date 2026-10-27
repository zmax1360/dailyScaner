"""Lookups for the newest scan and weekly archives of a ticker."""

from __future__ import annotations

import glob
import json
import math
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


def usable_spot(value) -> float | None:
    """``value`` as a float when it is a finite positive price, else None.
    NaN passes ``not spot or spot <= 0`` (NaN compares False), so check finiteness."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def latest_usable_spot(ticker: str = "AAPL", *, limit: int = 50) -> tuple[float | None, str | None]:
    """(spot, archive file name) from the newest scan archive whose saved spot is a real
    price. An after-hours scan can save spot as NaN; the pages then fall back to the
    last scan that had one instead of going blank."""
    files = sorted(glob.glob(f"archive/{ticker}_*.json"), reverse=True)[:limit]
    for path in files:
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        spot = usable_spot(data.get("spot")) if isinstance(data, dict) else None
        if spot is not None:
            return spot, os.path.basename(path)
    return None, None
