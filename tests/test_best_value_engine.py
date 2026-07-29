"""
test_best_value_engine.py — characterisation + defect tests for best_value.py

The scoring engine ranks every contract the system recommends and had ZERO
tests before this file. Tests marked `xfail(strict=True)` encode the behaviour
the engine SHOULD have; they fail today and are the remediation backlog.
Delete the xfail marker as each defect is fixed — CI will then guard it.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest
import pytz

from best_value import attach_dvol, build_best_value_df, calculate_best_value

ET = pytz.timezone("US/Eastern")
NOW = ET.localize(datetime(2026, 7, 24, 11, 0))
SPOT = 250.0


# ── helpers ──────────────────────────────────────────────────────────────────

def contract(strike, price, vol=5000, oi=5000, side="CALL",
             expiry="2026-08-21", dte=28, iv=0.30, **extra):
    row = {
        "side": side, "strike": float(strike), "expiry": expiry, "dte": dte,
        "last": float(price), "volume": int(vol), "openInterest": int(oi),
        "iv": iv,
    }
    row.update(extra)
    return row


def score(rows, **kw):
    kw.setdefault("now_et", NOW)
    kw.setdefault("spot_price", SPOT)
    out = calculate_best_value(pd.DataFrame(rows), **kw)
    return out.set_index("strike")["Value_Score"]


# ── 1. Contract / schema invariants ──────────────────────────────────────────

def test_output_columns_are_stable():
    """Downstream (app.py, telegram_bot.py, best_value_archive.py) reads these."""
    out = calculate_best_value(pd.DataFrame([contract(250, 5.0)]),
                               spot_price=SPOT, now_et=NOW)
    for col in ("Value_Score", "Status", "Optimal_Strategy", "Strategy_Tag"):
        assert col in out.columns


def test_empty_frame_does_not_raise():
    out = calculate_best_value(pd.DataFrame(columns=["side", "strike", "expiry",
                                                     "dte", "last", "volume",
                                                     "openInterest", "iv"]),
                               spot_price=SPOT, now_et=NOW)
    assert out.empty or out["Value_Score"].isna().all()


def test_min_volume_gate_excludes_thin_contracts():
    s = score([contract(250, 5.0, vol=499), contract(255, 5.0, vol=5000)],
              min_volume=500)
    assert pd.isna(s[250.0])
    assert not pd.isna(s[255.0])


@pytest.mark.parametrize("hour,minute,should_keep_0dte", [
    (11, 0, True), (16, 14, True), (16, 15, False), (17, 0, False),
])
def test_0dte_dropped_after_1615_et(hour, minute, should_keep_0dte):
    now = ET.localize(datetime(2026, 7, 24, hour, minute))
    rows = [contract(250, 5.0, expiry="2026-07-24", dte=0),
            contract(255, 5.0, expiry="2026-08-21", dte=28)]
    s = score(rows, now_et=now)
    assert (not pd.isna(s[250.0])) is should_keep_0dte


def test_expired_contracts_always_dropped():
    s = score([contract(250, 5.0, expiry="2026-07-23", dte=-1),
               contract(255, 5.0)])
    assert pd.isna(s[250.0])


# ── 2. The leverage term (Black-Scholes delta) ───────────────────────────────

def test_delta_column_is_produced_by_the_pipeline():
    """Task C: build_best_value_df emits a real BS delta (never hardcoded 0.5)."""
    df = build_best_value_df(
        {"top_calls": [{"strike": 250, "lastPrice": 5.0, "volume": 5000,
                        "openInterest": 5000, "expiry": "2026-08-21",
                        "dte": 28, "impliedVolatility": 0.3,
                        "bid": 4.9, "ask": 5.1}],
         "top_puts": []},
        spot=SPOT, vol_prev=None, now_et=NOW,
    )
    assert "delta" in df.columns
    assert pd.notna(df["delta"].iloc[0])
    assert 0.0 < float(df["delta"].iloc[0]) < 1.0


def test_leverage_leg_should_not_be_monotonic_in_price_alone():
    """
    Two CALLs, identical flow. Far-OTM cheap contract used to beat near-ATM
    under delta=0.5 (pure 1/price). With BS delta the near-ATM must not lose.
    """
    s = score([contract(252.5, 3.00), contract(280.0, 0.60)])
    assert s[252.5] >= s[280.0]


# ── 3. ΔVol: the phantom-flow fix over-corrected ─────────────────────────────

def test_attach_dvol_marks_new_entrants_as_nan_not_zero():
    df = pd.DataFrame([contract(250, 5.0), contract(255, 5.0)])
    prev = {"top_calls": [{"strike": 250.0, "expiry": "2026-08-21",
                           "volume": 4000}], "top_puts": []}
    out = attach_dvol(df, prev)
    assert out.loc[out["strike"] == 250.0, "dVol"].iloc[0] == 1000.0
    assert pd.isna(out.loc[out["strike"] == 255.0, "dVol"].iloc[0])


@pytest.mark.xfail(strict=True, reason="DEFECT: NaN -> 1.0 is not neutral. On a "
                                       "scale where dVol is thousands, 1.0 is "
                                       "effectively zero, so brand-new sweeps "
                                       "are ranked LAST.")
def test_new_entrant_is_not_systematically_penalised():
    """
    Contract A added 1,000 lots to an existing position.
    Contract B printed 5,000 lots from nothing — the sweep you actually want.
    B must not score below A.
    """
    df = pd.DataFrame([contract(250, 5.0, vol=5000), contract(255, 5.0, vol=5000)])
    prev = {"top_calls": [{"strike": 250.0, "expiry": "2026-08-21",
                           "volume": 4000}], "top_puts": []}
    out = calculate_best_value(attach_dvol(df, prev), spot_price=SPOT, now_et=NOW)
    s = out.set_index("strike")["Value_Score"]
    assert s[255.0] >= s[250.0]


def test_volume_collapse_scores_lower_than_volume_surge():
    """Identical volume and OI; only the SIGN of dVol differs."""
    df = pd.DataFrame([contract(250, 5.0, vol=5000), contract(255, 5.0, vol=5000)])
    prev = {"top_calls": [
        {"strike": 250.0, "expiry": "2026-08-21", "volume": 1000},   # +4000 build
        {"strike": 255.0, "expiry": "2026-08-21", "volume": 9000},   # -4000 unwind
    ], "top_puts": []}
    # Task A: curr < prev is treated as rollover (dVol=NaN), not signed unwind.
    # Inject signed dVol after attach to pin the no-abs() scoring behaviour.
    attached = attach_dvol(df, prev)
    attached.loc[attached["strike"] == 250.0, "dVol"] = 4000.0
    attached.loc[attached["strike"] == 255.0, "dVol"] = -4000.0
    attached["dvol_suspect"] = False
    out = calculate_best_value(attached, spot_price=SPOT, now_et=NOW)
    s = out.set_index("strike")["Value_Score"]
    assert s[250.0] > s[255.0]


# ── 4. Score comparability across runs ───────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DEFECT: min-max is computed within each "
                                       "snapshot, so Value_Score is a rank, not "
                                       "a level. best_value_archive.py persists "
                                       "it and derives Score_Velocity from it.")
def test_score_is_stable_when_an_unrelated_contract_joins_the_universe():
    """
    Contract 250 is unchanged between run A and run B. Only an unrelated
    contract appears. Its persisted Value_Score must not move — otherwise
    Score_Velocity measures universe churn, not conviction.
    """
    base = [contract(250, 5.0, vol=5000), contract(255, 5.0, vol=6000)]
    run_a = score(base)
    run_b = score(base + [contract(300, 0.10, vol=90000, oi=100)])
    assert run_a[250.0] == pytest.approx(run_b[250.0], rel=1e-9)


@pytest.mark.xfail(strict=True, reason="DEFECT: multipliers are applied AFTER "
                                       "normalisation and compound without a "
                                       "cap, so Value_Score escapes [0, 1]")
def test_value_score_stays_within_its_documented_range():
    s = score([contract(252.5, 3.0), contract(255, 3.0), contract(257.5, 3.0)],
              daily_bias="HEAVY BULLISH", news_bias="BULLISH",
              vwap_state="RECLAIMED UP", optimal_strategy="(+2) BULLISH")
    assert s.dropna().between(0.0, 1.0).all()


def test_two_row_universe_produces_degenerate_scores():
    """
    Characterisation, not a defect claim: with N=2 min-max always yields
    exactly {0.0, 1.0} on each leg, so the 40/60 weighting is meaningless.
    Documented so nobody trusts BEST VALUE on a thin chain.
    """
    s = score([contract(250, 5.0, vol=5000), contract(255, 1.0, vol=5000)])
    assert set(round(v, 6) for v in s.dropna()) <= {0.3, 0.7, 0.09, 0.21}


# ── 5. Directional multipliers ───────────────────────────────────────────────

@pytest.mark.parametrize("bias,penalised", [
    ("HEAVY BEARISH", "CALL"), ("HEAVY BULLISH", "PUT"),
])
@pytest.mark.xfail(strict=True, reason="DEFECT: daily_bias feeds recommend_strategy, "
                                       "which swaps the strategy multiplier. "
                                       "bias=None resolves to '(0)' and crushes ALL "
                                       "directional contracts x0.3, so a HEAVY BEARISH "
                                       "day scores CALLs HIGHER than an unknown-bias day.")
def test_daily_bias_penalises_the_opposing_side(bias, penalised):
    rows = [contract(255, 3.0, side="CALL"), contract(245, 3.0, side="PUT")]
    neutral = score(rows)
    biased = score(rows, daily_bias=bias)
    k = 255.0 if penalised == "CALL" else 245.0
    assert biased[k] < neutral[k]


@pytest.mark.xfail(strict=True, reason="DEFECT: the strategy-multiplier block only "
                                       "branches on '(+2)', '(+1)' and '(0)'. "
                                       "'(-1)' and '(-2)' have no branch, so bearish "
                                       "regimes get no strategy adjustment at all.")
def test_bearish_strategy_boosts_puts_the_way_bullish_boosts_calls():
    rows = [contract(255, 3.0, side="CALL"), contract(245, 3.0, side="PUT")]
    bull = score(rows, optimal_strategy="(+1) BULL")
    bear = score(rows, optimal_strategy="(-1) BEAR")
    assert bear[245.0] > bull[245.0]


def test_multipliers_compound_multiplicatively():
    """Regression guard: bias x market_state x news must all apply, not last-wins."""
    rows = [contract(255, 3.0, side="CALL"), contract(245, 3.0, side="PUT")]
    one = score(rows, daily_bias="HEAVY BEARISH")
    two = score(rows, daily_bias="HEAVY BEARISH", market_state="BEARISH DRAG")
    assert two[255.0] == pytest.approx(one[255.0] * 0.3, rel=1e-9)


def test_strike_outside_1sd_is_penalised():
    rows = [contract(255, 3.0), contract(280, 3.0)]
    s = score(rows, upper_1sd=260.0)
    assert s[280.0] < s[255.0]


# ── 6. Input robustness (fuzz-lite) ──────────────────────────────────────────

@pytest.mark.parametrize("bad", [
    {"last": 0.0}, {"last": -1.0}, {"volume": 0}, {"openInterest": 0},
    {"iv": float("nan")}, {"expiry": ""}, {"dte": -5}, {"volume": -1},
])
def test_degenerate_rows_never_raise(bad):
    rows = [contract(250, 5.0), contract(255, 5.0, **bad)]
    calculate_best_value(pd.DataFrame(rows), spot_price=SPOT, now_et=NOW)


@pytest.mark.parametrize("spot", [0.0, -10.0, float("nan")])
def test_degenerate_spot_never_raises(spot):
    calculate_best_value(pd.DataFrame([contract(250, 5.0), contract(255, 5.0)]),
                         spot_price=spot, now_et=NOW)


def test_naive_datetime_is_localised_to_et():
    naive = datetime(2026, 7, 24, 16, 30)   # after close, tz-naive
    s = score([contract(250, 5.0, expiry="2026-07-24", dte=0),
               contract(255, 5.0)], now_et=naive)
    assert pd.isna(s[250.0]), "naive datetime must be treated as ET, not UTC"
