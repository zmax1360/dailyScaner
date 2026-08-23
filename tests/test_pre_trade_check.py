"""Pre-trade check calculation suite (T1–T10). UI is not exercised here."""

from __future__ import annotations

import math

import pytest

from pre_trade_check import (
    DISTANCE_ERROR,
    LOSS_CLAMP_NOTE,
    NO_CONTRACT_REASON,
    PreTradeInputs,
    compute_pre_trade,
    format_loss,
    format_money,
    format_pct,
    format_ratio,
    min_dte_for_hold,
)

MONEY = 0.01
PCT = 0.001  # 0.1 percentage points as a decimal
RATIO = 0.05
ACCOUNT_HINT = "enter account size."


def _failed_blob(r: dict) -> str:
    return " ".join(r["failed"]).lower()


def _gate(r: dict, name_substr: str) -> dict:
    key = name_substr.lower()
    for g in r["gates"]:
        if key in g["name"].lower():
            return g
    raise AssertionError(f"gate {name_substr!r} not found in {[g['name'] for g in r['gates']]}")


def _assert_finite(obj, path=""):
    if isinstance(obj, float):
        assert math.isfinite(obj), f"non-finite at {path}: {obj!r}"
    elif isinstance(obj, dict):
        for k, v in obj.items():
            _assert_finite(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            _assert_finite(v, f"{path}[{i}]")


def _t1(**kw) -> PreTradeInputs:
    base = dict(
        direction="CALL",
        underlying=178.00,
        target_distance=2.00,
        invalidation_distance=1.00,
        hold_hours=0.75,
        entry_window="09:45-11:30",
        strike=180.00,
        dte=0,
        bid=1.05,
        ask=1.10,
        delta=0.400,
        theta=0.1200,
        theta_units="per day",
        open_interest=0,
        account_size=0,
    )
    base.update(kw)
    return PreTradeInputs(**base)


def _t2(**kw) -> PreTradeInputs:
    base = dict(
        direction="CALL",
        underlying=315.00,
        target_distance=2.50,
        invalidation_distance=0.80,
        hold_hours=1.5,
        entry_window="09:45-11:30",
        strike=316.00,
        dte=2,
        bid=2.20,
        ask=2.25,
        delta=0.440,
        theta=0.1500,
        theta_units="per day",
        open_interest=4200,
        account_size=50000,
    )
    base.update(kw)
    return PreTradeInputs(**base)


def test_t1_baseline_nvda():
    r = compute_pre_trade(_t1())
    d = r["derived"]
    _assert_finite(r)
    assert d["mid"] == pytest.approx(1.075, abs=MONEY)
    assert d["spread"] == pytest.approx(0.05, abs=MONEY)
    assert d["spread_pct"] == pytest.approx(0.047, abs=PCT)
    assert format_pct(d["spread_pct"]) == "4.7%"
    assert d["intrinsic"] == pytest.approx(0.00, abs=MONEY)
    assert d["extrinsic_pct"] == pytest.approx(1.0, abs=PCT)
    assert format_pct(d["extrinsic_pct"]) == "100.0%"
    assert d["theta_hr"] == pytest.approx(0.01846, abs=1e-5)
    assert d["move_to_target"] == pytest.approx(2.00, abs=MONEY)
    assert d["move_to_stop"] == pytest.approx(1.00, abs=MONEY)
    assert d["gain_per_contract"] == pytest.approx(0.7362, abs=MONEY)
    assert d["loss_per_contract"] == pytest.approx(-0.4783, abs=MONEY)
    assert d["loss_per_contract"] < 0
    assert format_loss(d["loss_per_contract"]).startswith("−")
    assert d["value_at_target"] == pytest.approx(1.8112, abs=MONEY)
    assert d["value_at_stop"] == pytest.approx(0.5967, abs=MONEY)
    assert d["value_at_stop"] >= 0
    assert d["gain_pct"] == pytest.approx(0.685, abs=PCT)
    assert d["loss_pct"] == pytest.approx(0.445, abs=PCT)
    assert format_pct(d["gain_pct"]) == "68.5%"
    assert format_pct(d["loss_pct"]) == "44.5%"
    assert d["ratio"] == pytest.approx(1.5, abs=RATIO)
    assert format_ratio(d["ratio"]) == "1.5 : 1"
    assert d["breakeven_move"] == pytest.approx(0.16, abs=MONEY)
    assert d["breakeven_win_rate"] == pytest.approx(0.394, abs=PCT)
    assert d["time_stop_minutes"] == 29
    assert d["risk_per_contract"] == pytest.approx(-47.83, abs=MONEY)
    assert d["risk_per_contract"] < 0
    assert r["verdict"] == "SKIP"
    blob = _failed_blob(r)
    assert "spread" in blob
    assert "liquidity" in blob
    assert "reward" in blob
    assert "dte" in blob
    assert "Position size" not in r["failed"]
    assert _gate(r, "Position size")["passed"] is None
    assert _gate(r, "Position size")["badge"] == "—"
    assert ACCOUNT_HINT in (_gate(r, "Position size")["fail_msg"] or "")


def test_t2_take_every_gate():
    r = compute_pre_trade(_t2())
    d = r["derived"]
    _assert_finite(r)
    assert d["mid"] == pytest.approx(2.225, abs=MONEY)
    assert d["spread_pct"] == pytest.approx(0.022, abs=PCT)
    assert format_pct(d["spread_pct"]) == "2.2%"
    assert d["gain_per_contract"] == pytest.approx(1.0154, abs=MONEY)
    assert d["loss_per_contract"] == pytest.approx(-0.4428, abs=MONEY)
    assert d["gain_pct"] == pytest.approx(0.456, abs=PCT)
    assert d["loss_pct"] == pytest.approx(0.199, abs=PCT)
    assert d["ratio"] == pytest.approx(2.3, abs=RATIO)
    assert format_ratio(d["ratio"]) == "2.3 : 1"
    assert d["risk_per_contract"] == pytest.approx(-44.28, abs=MONEY)
    assert d["max_risk"] == pytest.approx(500.00, abs=MONEY)
    assert d["contracts"] == 11
    assert d["capital_deployed"] == pytest.approx(2447.50, abs=MONEY)
    assert d["pct_of_account"] == pytest.approx(0.049, abs=PCT)
    assert r["verdict"] == "TAKE"
    assert r["failed"] == []
    assert all(g["passed"] is True for g in r["gates"])
    assert r["plan"]
    assert "CALL" in r["plan"]
    assert "Ratio" in r["plan"]
    assert format_money(d["implied_target_price"]) in r["plan"] or "317.50" in r["plan"]


def test_t3_put_negative_greeks():
    r = compute_pre_trade(PreTradeInputs(
        direction="PUT",
        underlying=315.24,
        target_distance=3.00,
        invalidation_distance=1.50,
        hold_hours=2.0,
        strike=314.00,
        dte=2,
        bid=2.40,
        ask=2.48,
        delta=-0.420,
        theta=-0.1800,
        theta_units="per day",
        open_interest=3400,
        account_size=50000,
        entry_window="09:45-11:30",
    ))
    d = r["derived"]
    _assert_finite(r)
    assert d["abs_delta"] == pytest.approx(0.420, abs=1e-6)
    assert d["abs_theta"] == pytest.approx(0.1800, abs=1e-6)
    assert d["implied_target_price"] == pytest.approx(312.24, abs=MONEY)
    assert d["implied_stop_price"] == pytest.approx(316.74, abs=MONEY)
    assert d["implied_target_price"] < 315.24
    assert d["implied_stop_price"] > 315.24
    assert d["intrinsic"] == pytest.approx(0.00, abs=MONEY)
    assert d["mid"] == pytest.approx(2.44, abs=MONEY)
    assert d["spread_pct"] == pytest.approx(0.033, abs=PCT)
    assert d["gain_per_contract"] == pytest.approx(1.1246, abs=MONEY)
    assert d["loss_per_contract"] == pytest.approx(-0.7632, abs=MONEY)
    assert d["ratio"] == pytest.approx(1.5, abs=RATIO)
    assert r["verdict"] == "SKIP"


def test_t4_in_the_money_intrinsic():
    r = compute_pre_trade(PreTradeInputs(
        direction="CALL",
        underlying=178.00,
        strike=175.00,
        bid=3.60,
        ask=3.70,
        target_distance=2.0,
        invalidation_distance=1.0,
    ))
    d = r["derived"]
    assert d["intrinsic"] == pytest.approx(3.00, abs=MONEY)
    assert d["extrinsic"] == pytest.approx(0.65, abs=MONEY)
    assert d["extrinsic_pct"] == pytest.approx(0.178, abs=PCT)
    assert format_pct(d["extrinsic_pct"]) == "17.8%"
    assert d["fully_extrinsic"] is False


def test_t5_loss_clamps_at_premium():
    r = compute_pre_trade(PreTradeInputs(
        direction="CALL",
        underlying=100.00,
        invalidation_distance=10.00,
        target_distance=2.00,
        delta=0.400,
        bid=0.98,
        ask=1.02,
    ))
    d = r["derived"]
    assert d["mid"] == pytest.approx(1.00, abs=MONEY)
    assert d["loss_per_contract"] == pytest.approx(-1.00, abs=MONEY)
    assert d["value_at_stop"] == pytest.approx(0.00, abs=MONEY)
    assert d["loss_clamped"] is True
    assert d["loss_clamp_note"] == LOSS_CLAMP_NOTE


def test_t6_theta_units_equivalence():
    per_day = compute_pre_trade(_t1(theta=0.1200, theta_units="per day"))
    per_hour = compute_pre_trade(_t1(theta=0.018462, theta_units="per hour"))
    a, b = per_day["derived"], per_hour["derived"]
    skip = {"abs_theta"}  # input magnitude differs by construction
    for k in a:
        if k in skip:
            continue
        va, vb = a[k], b[k]
        if isinstance(va, float) and isinstance(vb, float):
            assert va == pytest.approx(vb, abs=1e-4), k
        else:
            assert va == vb, k


def test_t7_dte_gate_boundaries():
    cases = [
        (0.4, 0, True),
        (0.5, 0, False),
        (0.5, 1, True),
        (2.9, 1, True),
        (3.0, 1, False),
        (3.0, 2, True),
        (8.0, 2, False),
        (8.0, 5, True),
    ]
    for hold, dte, expect_pass in cases:
        r = compute_pre_trade(_t2(hold_hours=hold, dte=dte))
        g = _gate(r, "DTE")
        assert g["passed"] is expect_pass, (
            f"hold={hold} dte={dte}: expected {expect_pass}, got {g['passed']} "
            f"(min_dte={min_dte_for_hold(hold)})"
        )


def test_t8_empty_and_zero_inputs():
    r = compute_pre_trade(PreTradeInputs())
    _assert_finite(r)
    d = r["derived"]
    for key in (
        "mid", "spread", "spread_pct", "intrinsic", "extrinsic", "extrinsic_pct",
        "theta_hr", "move_to_target", "move_to_stop", "gain_per_contract",
        "loss_per_contract", "value_at_target", "value_at_stop",
        "gain_pct", "loss_pct", "ratio", "breakeven_move", "breakeven_win_rate",
        "risk_per_contract", "max_risk", "contracts", "capital_deployed",
        "pct_of_account",
    ):
        assert d[key] is None, key
        if key == "ratio":
            assert format_ratio(d[key]) == "—"
        elif key.endswith("_pct") or key == "breakeven_win_rate":
            assert format_pct(d[key]) == "—"
        elif key in ("loss_per_contract", "risk_per_contract"):
            assert format_loss(d[key]) == "—"
        elif key != "contracts":
            assert format_money(d[key]) == "—"
    assert r["verdict"] == "SKIP"
    assert r["skip_reason"] == NO_CONTRACT_REASON


def test_t9_spread_gate_boundary():
    pass_case = compute_pre_trade(_t2(bid=1.00, ask=1.03))
    fail_case = compute_pre_trade(_t2(bid=1.00, ask=1.04))
    assert pass_case["derived"]["mid"] == pytest.approx(1.015, abs=MONEY)
    assert pass_case["derived"]["spread_pct"] == pytest.approx(0.0296, abs=PCT)
    assert _gate(pass_case, "Spread")["passed"] is True
    assert fail_case["derived"]["mid"] == pytest.approx(1.020, abs=MONEY)
    assert fail_case["derived"]["spread_pct"] == pytest.approx(0.0392, abs=PCT)
    assert _gate(fail_case, "Spread")["passed"] is False


def test_t10_distance_field_validation():
    ok = compute_pre_trade(_t2(target_distance=2.00, invalidation_distance=1.00))
    assert "target_distance" not in ok["field_errors"]
    assert "invalidation_distance" not in ok["field_errors"]

    neg_t = compute_pre_trade(_t2(target_distance=-2.00, invalidation_distance=1.00))
    assert neg_t["field_errors"].get("target_distance") == DISTANCE_ERROR
    assert "invalidation_distance" not in neg_t["field_errors"]
    assert neg_t["verdict"] == "SKIP"
    assert neg_t["derived"]["move_to_target"] is None

    zero_i = compute_pre_trade(_t2(target_distance=2.00, invalidation_distance=0))
    assert zero_i["field_errors"].get("invalidation_distance") == DISTANCE_ERROR
    assert zero_i["verdict"] == "SKIP"

    neg_i = compute_pre_trade(_t2(target_distance=2.00, invalidation_distance=-1.00))
    assert neg_i["field_errors"].get("invalidation_distance") == DISTANCE_ERROR
    assert neg_i["verdict"] == "SKIP"
