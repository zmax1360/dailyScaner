"""After hours Yahoo can return NaN for the live price and for today's daily close.
NaN slipped past ``spot <= 0`` and was saved as the scan's spot, so the scan had no
gamma read and the Gamma page went blank (AAPL scan 2026-10-26 20:02 ET)."""
import math

import numpy as np
import pandas as pd

from dailyScaner import _gamma_snapshot, _usable_spot


def _daily(closes):
    idx = pd.date_range("2026-10-20", periods=len(closes), freq="D")
    return pd.DataFrame({"Close": closes}, index=idx)


def test_live_price_used_when_finite():
    assert _usable_spot(343.987, _daily([340.0, 341.0])) == 343.99


def test_nan_live_falls_back_to_last_finite_close():
    assert _usable_spot(float("nan"), _daily([347.73, 343.02, np.nan])) == 343.02


def test_none_or_nonpositive_live_falls_back():
    assert _usable_spot(None, _daily([347.73])) == 347.73
    assert _usable_spot(0.0, _daily([347.73])) == 347.73


def test_no_usable_price_returns_none():
    assert _usable_spot(float("nan"), _daily([np.nan, np.nan])) is None
    assert _usable_spot(None, pd.DataFrame()) is None


def test_gamma_snapshot_skips_nan_spot_without_raising():
    empty = pd.DataFrame()
    assert _gamma_snapshot(empty, empty, float("nan"), pd.Timestamp.now(tz="America/New_York")) is None
