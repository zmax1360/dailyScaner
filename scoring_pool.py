"""
Single definition of the 0DTE / 1DTE+ scoring split.

Used by best_value normalisation, attribution.pool, eod_report buckets,
and display. Do not re-derive the boundary elsewhere.
"""

from __future__ import annotations

from typing import Any

# Labels persisted on flags.pool and shown in UI / Telegram.
POOL_0DTE = "0DTE"
POOL_1DTE = "1DTE+"
POOL_UNKNOWN = "UNKNOWN"

# Min surviving contracts (delta+flow) required to min-max-rank a pool.
# Below this, ranks are degenerate — do not score/merge into the other pool.
DEFAULT_MIN_POOL_SIZE = 5

# Same CASE expression as eod_report.DTE_BUCKET_SQL (must stay in sync).
DTE_BUCKET_SQL = """CASE
    WHEN dte IS NULL THEN 'UNKNOWN'
    WHEN dte = 0 THEN '0DTE'
    ELSE '1DTE+'
END"""


def scoring_pool(dte: Any) -> str | None:
    """
    Pool for scoring / ranking.

    Returns:
      '0DTE'  when dte == 0
      '1DTE+' when dte >= 1
      None    when dte is NULL / unparseable — excluded from ranking
    """
    if dte is None:
        return None
    try:
        # pandas NA
        if dte != dte:  # NaN
            return None
    except (TypeError, ValueError):
        pass
    try:
        d = int(dte)
    except (TypeError, ValueError):
        return None
    if d < 0:
        return None
    if d == 0:
        return POOL_0DTE
    return POOL_1DTE


def dte_bucket(dte: Any) -> str:
    """Report bucket — UNKNOWN for null (never silently pooled into 1DTE+)."""
    p = scoring_pool(dte)
    if p is None:
        return POOL_UNKNOWN
    return p
