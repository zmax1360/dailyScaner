"""engine-v1.1 — leverage uses abs(delta) so puts are not floored by _minmax."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
import pytz

from best_value import calculate_best_value
from config import SCORING

ET = pytz.timezone("US/Eastern")
NOW = ET.localize(datetime(2026, 7, 20, 11, 0, 0))
SPOT = 250.0


def _mirrored_chain() -> pd.DataFrame:
    """Matched CALL/PUT at mirrored strikes, similar premium."""
    rows = []
    for side, strikes in (
        ("CALL", (245.0, 250.0, 255.0, 260.0)),
        ("PUT", (240.0, 245.0, 250.0, 255.0)),
    ):
        for k in strikes:
            rows.append({
                "side": side,
                "strike": k,
                "expiry": "2026-08-21",
                "dte": 32,
                "last": abs(SPOT - k) * 0.02 + 2.0,
                "bid": 1.8,
                "ask": 2.2,
                "volume": 800,
                "openInterest": 1000,
                "iv": 0.35,
            })
    return pd.DataFrame(rows)


def test_leverage_uses_abs_delta(monkeypatch):
    """PUT delta −0.4 and CALL +0.4 at same premium/spot → same leverage_raw."""
    import greeks

    def _fixed_delta(side, *args, **kwargs):
        return 0.4 if str(side).upper() == "CALL" else -0.4

    monkeypatch.setattr(greeks, "bs_delta", _fixed_delta)

    df = pd.DataFrame([
        {
            "side": "CALL", "strike": 250.0, "expiry": "2026-08-21", "dte": 32,
            "last": 5.0, "bid": 4.9, "ask": 5.1,
            "volume": 800, "openInterest": 1000, "iv": 0.35,
        },
        {
            "side": "PUT", "strike": 250.0, "expiry": "2026-08-21", "dte": 32,
            "last": 5.0, "bid": 4.9, "ask": 5.1,
            "volume": 800, "openInterest": 1000, "iv": 0.35,
        },
    ])
    out = calculate_best_value(df, spot_price=SPOT, now_et=NOW)
    call = out.loc[out["side"] == "CALL"].iloc[0]
    put = out.loc[out["side"] == "PUT"].iloc[0]
    assert float(call["delta"]) == pytest.approx(0.4)
    assert float(put["delta"]) == pytest.approx(-0.4)
    assert float(call["_lev"]) == pytest.approx(float(put["_lev"]), rel=1e-9)
    assert float(call["_lev"]) == pytest.approx(abs(0.4) * SPOT / 5.0, rel=1e-9)


def test_leverage_raw_never_negative():
    out = calculate_best_value(_mirrored_chain(), spot_price=SPOT, now_et=NOW)
    scored = out[out["Value_Score"].notna()]
    assert not scored.empty
    assert (scored["_lev"].dropna() >= 0).all()


def test_puts_not_floored_in_leverage_norm():
    out = calculate_best_value(_mirrored_chain(), spot_price=SPOT, now_et=NOW)
    scored = out[out["Value_Score"].notna()]
    puts = scored[scored["side"] == "PUT"]
    assert not puts.empty
    median_lnorm = float(scored["_nlev"].median())
    assert (puts["_nlev"] > median_lnorm).any(), (
        f"all puts at/below median leverage_norm={median_lnorm}; "
        f"put norms={puts['_nlev'].tolist()}"
    )
    # Before abs-delta, signed minmax floored the most-negative put to ~0
    assert float(puts["_nlev"].max()) > 0.2


def test_null_delta_still_excluded_and_renormalised():
    rows = [
        {"side": "CALL", "strike": 250.0, "expiry": "2026-08-21", "dte": 28,
         "last": 5.0, "volume": 5000, "openInterest": 5000, "iv": 0.30},
        {"side": "PUT", "strike": 250.0, "expiry": "2026-08-21", "dte": 28,
         "last": 5.0, "volume": 5000, "openInterest": 5000, "iv": 0.30},
        # Degraded IV → null delta → excluded
        {"side": "CALL", "strike": 255.0, "expiry": "2026-08-21", "dte": 28,
         "last": 0.50, "volume": 5000, "openInterest": 5000, "iv": 1e-5},
    ]
    out = calculate_best_value(pd.DataFrame(rows), spot_price=SPOT, now_et=NOW)
    bad = out.loc[out["strike"] == 255.0].iloc[0]
    assert pd.isna(bad["delta"])
    assert pd.isna(bad["Value_Score"])
    assert pd.isna(bad["_lev"])
    survivors = out[(out["strike"] != 255.0) & out["Value_Score"].notna()]
    assert len(survivors) >= 1
    assert survivors["_nlev"].between(0.0, 1.0).all()


def test_flow_leg_unchanged():
    """Flow raw/norm must match independent recomputation (leg untouched)."""
    df = _mirrored_chain()
    df["dVol"] = 100.0
    out = calculate_best_value(df, spot_price=SPOT, now_et=NOW)
    scored = out[out["Value_Score"].notna()].copy()
    voi = scored["volume"].astype(float) / scored["openInterest"].clip(lower=1)
    expected_flow = (voi * scored["dVol"].fillna(1.0)).clip(lower=0)

    def _minmax(s: pd.Series) -> pd.Series:
        mn, mx = s.min(), s.max()
        if mx <= mn:
            return pd.Series(0.5, index=s.index)
        return (s - mn) / (mx - mn)

    expected_nflow = _minmax(expected_flow.astype(float))
    for idx in scored.index:
        assert float(scored.loc[idx, "_flow"]) == pytest.approx(
            float(expected_flow.loc[idx]), rel=1e-9,
        )
        assert float(scored.loc[idx, "_nflow"]) == pytest.approx(
            float(expected_nflow.loc[idx]), rel=1e-9,
        )


def test_blend_weights_unchanged():
    assert float(SCORING["w_lev"]) == 0.4
    assert float(SCORING["w_flow"]) == 0.6
    out = calculate_best_value(_mirrored_chain(), spot_price=SPOT, now_et=NOW)
    scored = out[out["Value_Score"].notna()]
    for _, r in scored.iterrows():
        base = float(r["_nlev"]) * 0.4 + float(r["_nflow"]) * 0.6
        assert float(r["_base_score"]) == pytest.approx(base, rel=1e-9)


def test_mirrored_call_put_leverage_norm_comparable(monkeypatch):
    """Ordering fix: matched |delta| + premium → comparable leverage_norm."""
    import greeks

    def _fixed_delta(side, spot, strike, *args, **kwargs):
        atm = abs(float(strike) - float(spot)) < 1.0
        mag = 0.50 if atm else 0.25
        return mag if str(side).upper() == "CALL" else -mag

    monkeypatch.setattr(greeks, "bs_delta", _fixed_delta)
    # Same premium for all — |delta| alone drives leverage rank
    df = pd.DataFrame([
        {
            "side": "CALL", "strike": 250.0, "expiry": "2026-08-21", "dte": 32,
            "last": 3.0, "volume": 800, "openInterest": 1000, "iv": 0.35,
        },
        {
            "side": "PUT", "strike": 250.0, "expiry": "2026-08-21", "dte": 32,
            "last": 3.0, "volume": 800, "openInterest": 1000, "iv": 0.35,
        },
        {
            "side": "CALL", "strike": 260.0, "expiry": "2026-08-21", "dte": 32,
            "last": 3.0, "volume": 800, "openInterest": 1000, "iv": 0.35,
        },
        {
            "side": "PUT", "strike": 240.0, "expiry": "2026-08-21", "dte": 32,
            "last": 3.0, "volume": 800, "openInterest": 1000, "iv": 0.35,
        },
    ])
    out = calculate_best_value(df, spot_price=SPOT, now_et=NOW)
    atm = out[out["strike"] == 250.0]
    assert len(atm) == 2
    assert float(atm.loc[atm["side"] == "CALL", "delta"].iloc[0]) == pytest.approx(0.50)
    assert float(atm.loc[atm["side"] == "PUT", "delta"].iloc[0]) == pytest.approx(-0.50)
    call_n = float(atm.loc[atm["side"] == "CALL", "_nlev"].iloc[0])
    put_n = float(atm.loc[atm["side"] == "PUT", "_nlev"].iloc[0])
    # Signed minmax would floor the PUT at 0; abs-delta keeps them aligned at top
    assert abs(call_n - put_n) < 0.05
    assert put_n == pytest.approx(1.0, abs=1e-9)
    assert call_n == pytest.approx(1.0, abs=1e-9)

    # Pre-fix (signed) would give put_n ~ 0 on this fixture
    signed_lev = out["delta"] * SPOT / out["last"]
    smin, smax = float(signed_lev.min()), float(signed_lev.max())
    put_signed_norm = (float(signed_lev.loc[atm.loc[atm["side"] == "PUT"].index[0]]) - smin) / (smax - smin)
    assert put_signed_norm == pytest.approx(0.0, abs=1e-9)
