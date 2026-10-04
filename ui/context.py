"""What a component needs to draw itself: the ticker and its two newest scan archives."""

from __future__ import annotations

import glob
import json
import os
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScanContext:
    ticker: str
    curr: dict[str, Any]
    prev: dict[str, Any] | None = None
    top_n: int = 5
    files: tuple[str, ...] = field(default_factory=tuple)

    @property
    def spot(self) -> float:
        return float(self.curr.get("spot") or 0)

    @property
    def vol(self) -> dict[str, Any]:
        return self.curr.get("volume") or {}

    @property
    def prev_vol(self) -> dict[str, Any] | None:
        return (self.prev.get("volume") or {}) if self.prev else None

    @property
    def pc_ratio(self) -> float:
        return float(self.vol.get("pc_ratio") or 0)

    @property
    def timestamp(self) -> str:
        return str(self.curr.get("timestamp") or "")


def _read(path: str) -> dict[str, Any] | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_scan_context(ticker: str, *, top_n: int = 5,
                      archive_dir: str = "archive") -> ScanContext | None:
    """Newest archive (and the one before it) for ``ticker``. None when there is no
    readable archive — callers show an empty state, never a default payload."""
    files = sorted(glob.glob(os.path.join(archive_dir, f"{ticker}_*.json")), reverse=True)
    if not files:
        return None
    curr = _read(files[0])
    if curr is None:
        return None
    prev = _read(files[1]) if len(files) > 1 else None
    return ScanContext(ticker=str(ticker), curr=curr, prev=prev, top_n=int(top_n),
                       files=tuple(files[:2]))
