"""
sources.fixture — MarketDataSource backed by an explicit recorded archive.

No network, no globbing, no "latest file" resolution. Pass a path or dict.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from sources.base import CHAIN_COLUMNS, validate_chain

ET = ZoneInfo("America/New_York")


def _as_float(v: Any, *, zero_to_nan: bool = False) -> float:
    try:
        if v is None:
            return float("nan")
        f = float(v)
        if f != f:  # NaN
            return float("nan")
        if zero_to_nan and f == 0.0:
            return float("nan")
        return f
    except (TypeError, ValueError):
        return float("nan")


def chain_from_archive_payload(
    payload: dict[str, Any],
    *,
    max_dte: int | None = None,
) -> pd.DataFrame:
    """
    Map archive ``volume.top_calls`` / ``top_puts`` into CHAIN_COLUMNS.

    Uses the archive's stored ``dte`` when present; otherwise computes from
    expiry vs the archive timestamp in ET. IV of exactly 0 → NaN. Delta is
    always NaN (archives do not store a trusted delta).
    """
    vol = payload.get("volume") or {}
    ts = payload.get("timestamp")
    asof: datetime.date | None = None
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=ET)
            asof = dt.astimezone(ET).date()
        except Exception:
            asof = None

    rows: list[dict[str, Any]] = []
    for side_key, side in (("top_calls", "CALL"), ("top_puts", "PUT")):
        for c in vol.get(side_key) or []:
            expiry = str(c.get("expiry") or "")[:10]
            if not expiry:
                continue
            dte_raw = c.get("dte")
            try:
                dte = float(dte_raw) if dte_raw is not None else float("nan")
            except (TypeError, ValueError):
                dte = float("nan")
            if dte != dte and asof is not None:
                try:
                    dte = float(
                        (datetime.strptime(expiry, "%Y-%m-%d").date() - asof).days
                    )
                except ValueError:
                    continue
            if max_dte is not None and dte == dte and dte > float(max_dte):
                continue
            if max_dte is not None and dte == dte and dte < 0:
                continue

            last = c.get("lastPrice", c.get("last"))
            iv = _as_float(c.get("impliedVolatility", c.get("iv")), zero_to_nan=True)
            rows.append({
                "side": side,
                "strike": _as_float(c.get("strike")),
                "expiry": expiry,
                "dte": dte,
                "bid": _as_float(c.get("bid")),
                "ask": _as_float(c.get("ask")),
                "last": _as_float(last),
                "volume": _as_float(c.get("volume")),
                "openInterest": _as_float(c.get("openInterest")),
                "iv": iv,
                "delta": float("nan"),
            })

    if not rows:
        return validate_chain(pd.DataFrame(columns=CHAIN_COLUMNS))
    return validate_chain(pd.DataFrame(rows, columns=CHAIN_COLUMNS))


class FixtureSource:
    """Recorded-archive MarketDataSource. Pin the snapshot in the constructor."""

    name = "fixture"
    volume_is_session_scoped = False
    provides_quotes = True

    def __init__(self, archive: str | Path | dict[str, Any]) -> None:
        if isinstance(archive, dict):
            self._payload = dict(archive)
            self._path: Path | None = None
        else:
            path = Path(archive)
            if not path.is_file():
                raise FileNotFoundError(f"fixture archive not found: {path}")
            self._path = path
            self._payload = json.loads(path.read_text(encoding="utf-8"))

    @property
    def payload(self) -> dict[str, Any]:
        return self._payload

    def fetch_chain(self, ticker: str, *, max_dte: int) -> pd.DataFrame:
        # ticker is accepted for Protocol parity; archive is already pinned.
        _ = ticker
        return chain_from_archive_payload(self._payload, max_dte=max_dte)

    def fetch_history(
        self, ticker: str, *, interval: str, period: str
    ) -> pd.DataFrame:
        _ = (ticker, interval, period)
        # Archives store indicators, not raw OHLCV bars — empty is honest.
        return pd.DataFrame(
            columns=["Open", "High", "Low", "Close", "Volume"]
        )

    def fetch_spot(self, ticker: str) -> float | None:
        _ = ticker
        try:
            spot = float(self._payload.get("spot"))
            return spot if spot == spot and spot > 0 else None
        except (TypeError, ValueError):
            return None

    def fetch_option_mid(
        self,
        ticker: str,
        side: str,
        strike: float,
        expiry: str,
    ) -> float | None:
        _ = ticker
        want = str(side).upper()
        exp = str(expiry)[:10]
        vol = self._payload.get("volume") or {}
        key = "top_calls" if want == "CALL" else "top_puts"
        for c in vol.get(key) or []:
            if str(c.get("expiry") or "")[:10] != exp:
                continue
            try:
                if abs(float(c.get("strike")) - float(strike)) > 1e-6:
                    continue
            except (TypeError, ValueError):
                continue
            bid = _as_float(c.get("bid"))
            ask = _as_float(c.get("ask"))
            last = _as_float(c.get("lastPrice", c.get("last")))
            if bid == bid and ask == ask and bid > 0 and ask > 0:
                mid = (bid + ask) / 2.0
                return mid if mid > 0 else None
            if last == last and last > 0:
                return last
            return None
        return None
