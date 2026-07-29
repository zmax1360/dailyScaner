"""
sources.base — MarketDataSource Protocol and chain schema validation.

Contract for every source implementation
----------------------------------------
- ``delta`` is ALWAYS a column. Sources that cannot supply it emit NaN.
  Never 0.5, never any other default.
- ``iv``, ``volume``, ``bid``, ``ask``: NaN when unavailable, never 0 as a
  stand-in for missing. (Some legacy Yahoo paths still fill bid/ask with 0;
  that is owned by chain_quality, not by inventing values here.)
- ``side`` is uppercase ``"CALL"`` / ``"PUT"``.
- ``dte`` is computed in America/New_York (ET), not the machine local date.

``volume_is_session_scoped`` lets rollover detectors in chain_quality.py
consult the source (True = clean session volume; False = may carry prior
session, e.g. Yahoo). Declared here; wiring is a later step.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pandas as pd

CHAIN_COLUMNS = [
    "side",
    "strike",
    "expiry",
    "dte",
    "bid",
    "ask",
    "last",
    "volume",
    "openInterest",
    "iv",
    "delta",
]


@runtime_checkable
class MarketDataSource(Protocol):
    name: str
    # True  = volume resets each session (clean).
    # False = may carry prior session (Yahoo).
    volume_is_session_scoped: bool

    def fetch_chain(self, ticker: str, *, max_dte: int) -> pd.DataFrame: ...

    def fetch_history(
        self, ticker: str, *, interval: str, period: str
    ) -> pd.DataFrame: ...

    def fetch_spot(self, ticker: str) -> float | None: ...

    def fetch_option_mid(
        self,
        ticker: str,
        side: str,
        strike: float,
        expiry: str,
    ) -> float | None: ...


def validate_chain(df: pd.DataFrame) -> pd.DataFrame:
    """
    Assert exact columns in exact order and stable dtypes. Return df.

    Raises ValueError on schema violations (missing/extra/wrong-order columns,
    non-CALL/PUT side, or iv==0 used as a missing sentinel).
    """
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"validate_chain expects DataFrame, got {type(df)!r}")

    cols = list(df.columns)
    if cols != CHAIN_COLUMNS:
        missing = [c for c in CHAIN_COLUMNS if c not in cols]
        extra = [c for c in cols if c not in CHAIN_COLUMNS]
        if missing or extra or cols != CHAIN_COLUMNS:
            raise ValueError(
                "chain columns must be exactly "
                f"{CHAIN_COLUMNS!r} in that order; got {cols!r}"
                + (f"; missing={missing}" if missing else "")
                + (f"; extra={extra}" if extra else "")
            )

    if df.empty:
        return df

    side = df["side"].astype(str).str.upper()
    bad_side = ~side.isin(["CALL", "PUT"]) & df["side"].notna()
    if bad_side.any():
        raise ValueError(
            "side must be uppercase CALL/PUT; "
            f"got {sorted(set(df.loc[bad_side, 'side'].astype(str)))}"
        )

    # Missing IV must be NaN — never 0 as a substitute.
    iv = pd.to_numeric(df["iv"], errors="coerce")
    if (iv == 0).any() or (iv == 0.0).any():
        raise ValueError(
            "iv must not use 0 as a missing stand-in; use NaN when unavailable"
        )

    out = df.copy()
    out["side"] = side
    for col in ("strike", "dte", "bid", "ask", "last", "volume",
                "openInterest", "iv", "delta"):
        out[col] = pd.to_numeric(out[col], errors="coerce")

    return out[CHAIN_COLUMNS]
