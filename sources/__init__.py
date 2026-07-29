"""Market data source adapters (Yahoo, Massive, fixtures)."""

from __future__ import annotations

from sources.base import CHAIN_COLUMNS, MarketDataSource, validate_chain
from sources.fixture import FixtureSource
from sources.massive import MassiveSource
from sources.yahoo import YahooSource

__all__ = [
    "CHAIN_COLUMNS",
    "FixtureSource",
    "MarketDataSource",
    "MassiveSource",
    "YahooSource",
    "get_source",
    "validate_chain",
]


def get_source(name: str) -> MarketDataSource:
    """
    Construct a MarketDataSource by config name.

    Call at the CLI / scan entry point — never at import time, never as a
    module-level singleton. ``fixture`` requires ``FixtureSource(path_or_dict)``
    explicitly (no implicit archive resolution).
    """
    key = str(name or "yahoo").strip().lower()
    if key == "yahoo":
        return YahooSource()
    if key == "massive":
        return MassiveSource()
    if key == "fixture":
        raise ValueError(
            "fixture source must be constructed as FixtureSource(path_or_dict); "
            "get_source('fixture') is not supported"
        )
    raise ValueError(f"unknown market_data_source: {name!r}")
