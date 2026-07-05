"""
test_spread_gate.py — pytest suite for spread_gate.evaluate_spread_gate.

Three tests:
  1. Known losing setup → NO-TRADE, PoP ≈ 0.35 (±0.02), EV < 0
  2. Clearly favorable synthetic setup → TRADE, PoP ≥ 0.40, EV > 0
  3. Garbage input (iv=0 and inverted strikes) → NO-TRADE with
     gate-error / failed-check reasons, no exception raised
"""

import pytest
from spread_gate import evaluate_spread_gate

# ── shared dates ──────────────────────────────────────────────────────────────
ENTRY  = "2026-07-06"
EXIT   = "2026-07-23"
EXPIRY = "2026-07-31"


# ── 1. Known losing setup must block ─────────────────────────────────────────
def test_losing_setup_is_no_trade():
    """
    spot=290, iv=28%, 295/305 spread, net debit $4.30.
    Breakeven = ~299.30, well above spot. PoP should be ~0.35; EV negative.
    """
    result = evaluate_spread_gate(
        spot=290.0,
        iv=0.28,
        long_strike=295.0,
        short_strike=305.0,
        long_premium=8.50,
        short_premium=4.20,
        entry_date=ENTRY,
        exit_date=EXIT,
        expiration=EXPIRY,
    )

    assert result["verdict"] == "NO-TRADE", (
        f"Expected NO-TRADE, got {result['verdict']}"
    )
    assert abs(result["pop"] - 0.35) <= 0.02, (
        f"Expected PoP ≈ 0.35 (±0.02), got {result['pop']:.4f}"
    )
    assert result["ev_per_contract"] < 0, (
        f"Expected negative EV, got {result['ev_per_contract']:.2f}"
    )
    assert len(result["reasons"]) > 0, "NO-TRADE must carry at least one reason"


# ── 2. Favorable synthetic setup must pass ───────────────────────────────────
def test_favorable_setup_is_trade():
    """
    spot=290, iv=28%, 285/295 spread, net debit $1.50.
    Breakeven = 286.50, already below spot — high PoP, positive EV.

    Premiums are synthetic (not market-realistic) but valid for testing
    the gate logic: a trader who obtained a $1.50 debit for a 10-point
    spread is in a strongly favorable position.
    """
    result = evaluate_spread_gate(
        spot=290.0,
        iv=0.28,
        long_strike=285.0,
        short_strike=295.0,
        long_premium=7.00,
        short_premium=5.50,
        entry_date=ENTRY,
        exit_date=EXIT,
        expiration=EXPIRY,
    )

    assert result["verdict"] == "TRADE", (
        f"Expected TRADE, got {result['verdict']} — reasons: {result['reasons']}"
    )
    assert result["pop"] >= 0.40, (
        f"Expected PoP ≥ 0.40, got {result['pop']:.4f}"
    )
    assert result["ev_per_contract"] > 0, (
        f"Expected positive EV, got {result['ev_per_contract']:.2f}"
    )
    assert result["reasons"] == [], (
        f"TRADE must have no reasons, got {result['reasons']}"
    )


# ── 3. Garbage inputs must never raise — always NO-TRADE ─────────────────────
def test_garbage_inputs_never_raise():
    """
    Two garbage variants are tested:
      a) iv=0  → Black-Scholes internal validation error → gate error
      b) short_strike < long_strike → inverted spread, deeply negative EV

    In both cases the function must return without raising and must
    signal NO-TRADE with a non-empty reasons list.
    """
    # 3a: zero IV triggers internal pydantic/BS validation error
    result_iv0 = evaluate_spread_gate(
        spot=290.0,
        iv=0.0,
        long_strike=295.0,
        short_strike=305.0,
        long_premium=8.50,
        short_premium=4.20,
        entry_date=ENTRY,
        exit_date=EXIT,
        expiration=EXPIRY,
    )
    assert result_iv0["verdict"] == "NO-TRADE"
    assert len(result_iv0["reasons"]) > 0
    assert any("gate error" in r for r in result_iv0["reasons"]), (
        f"Expected a 'gate error' reason for iv=0, got {result_iv0['reasons']}"
    )

    # 3b: inverted strikes (short < long) — wrong spread direction, both
    # checks fail (PoP near 0, EV deeply negative)
    result_inverted = evaluate_spread_gate(
        spot=290.0,
        iv=0.28,
        long_strike=305.0,   # buying the higher strike
        short_strike=295.0,  # selling the lower strike (inverted)
        long_premium=8.50,
        short_premium=4.20,
        entry_date=ENTRY,
        exit_date=EXIT,
        expiration=EXPIRY,
    )
    assert result_inverted["verdict"] == "NO-TRADE"
    assert len(result_inverted["reasons"]) > 0
