"""engine-v1.2 — independent 0DTE / 1DTE+ normalisation pools."""

from __future__ import annotations

import logging
from datetime import datetime

import pandas as pd
import pytest
import pytz

from attribution import build_control_rows_per_pool, log_run
from best_value import calculate_best_value
from config import SCORING
from scoring_pool import POOL_0DTE, POOL_1DTE, scoring_pool
from tests.scoring_fixtures import pad_min_pool

ET = pytz.timezone("US/Eastern")
NOW = ET.localize(datetime(2026, 8, 3, 11, 0, 0))
SPOT = 300.0


def _row(
    side, strike, dte, *, last, vol=5000, oi=1000, expiry=None, iv=0.35, **extra
):
    if expiry is None:
        expiry = "2026-08-03" if int(dte) == 0 else "2026-08-10"
    r = {
        "side": side,
        "strike": float(strike),
        "expiry": expiry,
        "dte": int(dte),
        "last": float(last),
        "bid": float(last) * 0.98,
        "ask": float(last) * 1.02,
        "volume": int(vol),
        "openInterest": int(oi),
        "iv": float(iv),
        "dVol": 100.0,
    }
    r.update(extra)
    return r


def _dual_pool_chain(*, inflate_0dte_lev: bool = True) -> pd.DataFrame:
    """≥5 contracts per pool; 0DTE premiums tiny so raw leverage dwarfs 1DTE+."""
    rows = []
    # 0DTE — cheap → huge abs(delta)*spot/price
    for i, k in enumerate((295.0, 297.5, 300.0, 302.5, 305.0, 307.5)):
        last = 0.05 + i * 0.02 if inflate_0dte_lev else 2.0
        rows.append(_row("CALL", k, 0, last=last, vol=8000 + i * 100, oi=200))
    # 1DTE+ — normal premiums; one standout on leverage (cheap relative)
    for i, k in enumerate((290.0, 295.0, 300.0, 305.0, 310.0, 315.0)):
        last = 0.80 if k == 315.0 else (3.5 + i * 0.2)
        rows.append(_row("CALL", k, 7, last=last, vol=2000 + i * 50, oi=1500))
    return pd.DataFrame(pad_min_pool(rows))


def test_pools_normalised_independently(monkeypatch):
    """Huge 0DTE raw leverage must not floor 1DTE+ leverage_norm."""
    import greeks

    def _delta(side, spot, strike, *a, **k):
        moneyness = abs(float(strike) - float(spot)) / float(spot)
        mag = max(0.15, 0.55 - moneyness * 2)
        return mag if str(side).upper() == "CALL" else -mag

    monkeypatch.setattr(greeks, "bs_delta", _delta)
    out = calculate_best_value(_dual_pool_chain(), spot_price=SPOT, now_et=NOW)
    plus = out[(out["pool"] == POOL_1DTE) & out["_nlev"].notna()]
    assert not plus.empty
    assert float(plus["_nlev"].max()) > 0.8


def test_1dte_not_compressed_by_0dte_presence(monkeypatch):
    import greeks

    monkeypatch.setattr(
        greeks, "bs_delta",
        lambda side, *a, **k: 0.4 if str(side).upper() == "CALL" else -0.4,
    )
    plus_only = [
        _row("CALL", k, 7, last=2.0 + i * 0.1, vol=3000, oi=1000)
        for i, k in enumerate((290.0, 295.0, 300.0, 305.0, 310.0, 315.0))
    ]
    with_0dte = plus_only + [
        _row("CALL", k, 0, last=0.08, vol=20000, oi=50)
        for k in (295.0, 297.5, 300.0, 302.5, 305.0, 307.5)
    ]
    target = (300.0, 7)
    out_a = calculate_best_value(
        pd.DataFrame(pad_min_pool(plus_only)), spot_price=SPOT, now_et=NOW,
    )
    out_b = calculate_best_value(
        pd.DataFrame(pad_min_pool(with_0dte)), spot_price=SPOT, now_et=NOW,
    )

    def _norms(df):
        r = df[(df["strike"] == target[0]) & (df["dte"] == target[1])].iloc[0]
        return float(r["_nlev"]), float(r["_nflow"])

    assert _norms(out_a) == pytest.approx(_norms(out_b), rel=1e-9)


def test_rank_is_within_pool():
    out = calculate_best_value(_dual_pool_chain(), spot_price=SPOT, now_et=NOW)
    for pool in (POOL_0DTE, POOL_1DTE):
        ranks = out.loc[out["pool"] == pool, "_rank"].dropna()
        assert 1 in set(int(x) for x in ranks), f"missing rank 1 in {pool}"
    # Two distinct BEST VALUE stars
    stars = out[out["Status"].astype(str).str.contains("BEST VALUE", na=False)]
    assert set(stars["pool"]) == {POOL_0DTE, POOL_1DTE}


def test_pool_column_persisted(tmp_path):
    db = str(tmp_path / "a.db")
    scored = calculate_best_value(_dual_pool_chain(), spot_price=SPOT, now_et=NOW)
    run_id = log_run(
        ticker="AAPL",
        scored_df=scored,
        cfg=SCORING,
        spot=SPOT,
        db_path=db,
        ts_et=NOW,
        control_rows=None,
    )
    import sqlite3
    con = sqlite3.connect(str(db))
    pools = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT pool FROM flags WHERE run_id=? AND is_control=0",
            (run_id,),
        )
    }
    con.close()
    assert POOL_0DTE in pools and POOL_1DTE in pools


def test_null_dte_excluded_from_both_pools(caplog):
    rows = pad_min_pool([
        _row("CALL", k, 7, last=2.0) for k in (290, 295, 300, 305, 310, 315)
    ])
    rows.append({
        "side": "CALL", "strike": 320.0, "expiry": "2026-08-10", "dte": None,
        "last": 1.50, "volume": 99999, "openInterest": 1, "iv": 0.35, "dVol": 1e6,
    })
    with caplog.at_level(logging.INFO, logger="best_value"):
        out = calculate_best_value(pd.DataFrame(rows), spot_price=SPOT, now_et=NOW)
    bad = out.loc[out["strike"] == 320.0].iloc[0]
    assert bad["pool"] is None or pd.isna(bad["pool"])
    assert pd.isna(bad["Value_Score"])
    assert pd.isna(bad["_rank"])
    assert any("null/invalid dte" in r.message for r in caplog.records)


def test_small_pool_not_ranked(caplog):
    # 3× 0DTE (below min) + 6× 1DTE+ (ranked) — 0DTE must not merge
    rows = [
        _row("CALL", k, 0, last=0.10) for k in (298.0, 300.0, 302.0)
    ] + [
        _row("CALL", k, 7, last=2.0 + i * 0.1)
        for i, k in enumerate((290.0, 295.0, 300.0, 305.0, 310.0, 315.0))
    ]
    with caplog.at_level(logging.INFO):
        out = calculate_best_value(pd.DataFrame(rows), spot_price=SPOT, now_et=NOW)
    odte = out[out["dte"] == 0]
    assert odte["Value_Score"].isna().all()
    assert odte["_rank"].isna().all()
    plus = out[(out["pool"] == POOL_1DTE) & out["Value_Score"].notna()]
    assert len(plus) >= 5
    assert any("not ranked (not merged)" in r.message for r in caplog.records)


def test_control_emitted_per_pool():
    chain = _dual_pool_chain()
    ctrl = build_control_rows_per_pool(chain, SPOT)
    assert not ctrl.empty
    assert set(ctrl["pool"]) == {POOL_0DTE, POOL_1DTE}
    for pool in (POOL_0DTE, POOL_1DTE):
        sub = ctrl[ctrl["pool"] == pool]
        assert set(sub["side"].astype(str).str.upper()) == {"CALL", "PUT"}


def test_multipliers_unchanged():
    """Pool split must not alter multiplier dict keys/values for a fixture row."""
    rows = pad_min_pool([
        _row("CALL", k, 7, last=2.5) for k in (290, 295, 300, 305, 310, 315)
    ])
    out = calculate_best_value(
        pd.DataFrame(rows), spot_price=SPOT, now_et=NOW,
        daily_bias="HEAVY BULLISH", news_bias="BULLISH",
    )
    m = out.loc[out["strike"] == 300.0, "_multipliers"].iloc[0]
    assert isinstance(m, dict)
    # Known keys from config — values must match SCORING literals
    if "news_with" in m:
        assert m["news_with"] == pytest.approx(SCORING["mult_news_with"])
    # No pool-related multiplier key invented
    assert "pool" not in m and "0dte_pool" not in m


def test_blend_weights_unchanged():
    assert float(SCORING["w_lev"]) == 0.4
    assert float(SCORING["w_flow"]) == 0.6
    out = calculate_best_value(_dual_pool_chain(), spot_price=SPOT, now_et=NOW)
    scored = out[out["Value_Score"].notna() & out["_base_score"].notna()]
    for _, r in scored.iterrows():
        base = float(r["_nlev"]) * 0.4 + float(r["_nflow"]) * 0.6
        assert float(r["_base_score"]) == pytest.approx(base, rel=1e-9)


def test_scoring_pool_boundary():
    assert scoring_pool(0) == POOL_0DTE
    assert scoring_pool(1) == POOL_1DTE
    assert scoring_pool(21) == POOL_1DTE
    assert scoring_pool(None) is None
