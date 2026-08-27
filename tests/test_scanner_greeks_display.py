"""Display-only Delta and Theta/Prem — must not touch ranking."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from best_value_ui import (
    attach_chain_greeks,
    delta_cell_tone,
    format_abs_delta,
    format_delta_cell,
    format_theta_prem,
    greeks_display_columns,
    ranking_identity_bytes,
)
from dailyScaner import _legacy_option_leg, _volume_block_records


def _fixture_top5() -> pd.DataFrame:
    """Fixed ranked table: Value_Score varies; Signal is uniformly '(0)'."""
    return pd.DataFrame(
        [
            {
                "side": "CALL",
                "strike": 307.5,
                "expiry": "2026-08-21",
                "last": 0.20,
                "iv": 0.55,
                "delta": 0.40,  # scoring / BS overwrite — must not be displayed
                "Value_Score": 0.2867,
                "Action_Signal": "(0)",
                "Optimal Strategy": "LONG CALL",
            },
            {
                "side": "PUT",
                "strike": 305.0,
                "expiry": "2026-08-21",
                "last": 1.10,
                "iv": 0.48,
                "delta": -0.35,
                "Value_Score": 0.1900,
                "Action_Signal": "(0)",
                "Optimal Strategy": "LONG CALL",
            },
            {
                "side": "CALL",
                "strike": 310.0,
                "expiry": "2026-08-21",
                "last": 0.45,
                "iv": 0.42,
                "delta": 0.28,
                "Value_Score": 0.0767,
                "Action_Signal": "(0)",
                "Optimal Strategy": "LONG CALL",
            },
        ]
    )


def _fixture_vol_curr() -> dict:
    """Same chain response the scanner already fetched (provider greeks)."""
    return {
        "top_calls": [
            {
                "strike": 307.5,
                "expiry": "2026-08-21",
                "lastPrice": 0.20,
                "delta": 0.082,
                "theta": -0.020,
            },
            {
                "strike": 310.0,
                "expiry": "2026-08-21",
                "lastPrice": 0.45,
                "delta": 0.180,
                "theta": -0.018,
            },
        ],
        "top_puts": [
            {
                "strike": 305.0,
                "expiry": "2026-08-21",
                "lastPrice": 1.10,
                "delta": -0.310,
                "theta": -0.040,
            },
        ],
    }


def test_rows_render_delta_and_theta_prem_from_chain():
    top5 = _fixture_top5()
    shown = greeks_display_columns(top5, _fixture_vol_curr())
    assert list(shown["Delta"]) == ["🔴 0.082", "0.310", "🟠 0.180"]
    assert shown["Theta/Prem"].iloc[0] == format_theta_prem(-0.020, 0.20)
    assert shown["Theta/Prem"].iloc[1] == format_theta_prem(-0.040, 1.10)
    assert shown["Theta/Prem"].iloc[2] == format_theta_prem(-0.018, 0.45)
    assert shown["Theta/Prem"].iloc[0] == "-10.0%"
    # Scoring delta must not leak into the display column.
    assert "0.400" not in list(shown["Delta"])
    assert "0.350" not in list(shown["Delta"])


def test_missing_greeks_render_em_dash_without_raising():
    top5 = _fixture_top5()
    vol = {
        "top_calls": [
            {"strike": 307.5, "expiry": "2026-08-21", "lastPrice": 0.20},
        ],
        "top_puts": [],
    }
    shown = greeks_display_columns(top5, vol)
    assert list(shown["Delta"]) == ["—", "—", "—"]
    assert list(shown["Theta/Prem"]) == ["—", "—", "—"]

    shown_empty = greeks_display_columns(top5, {})
    assert list(shown_empty["Delta"]) == ["—", "—", "—"]

    shown_none = greeks_display_columns(top5, None)
    assert list(shown_none["Theta/Prem"]) == ["—", "—", "—"]

    assert format_abs_delta(float("nan")) == "—"
    assert format_abs_delta(None) == "—"
    assert format_theta_prem(None, 0.20) == "—"
    assert format_theta_prem(-0.02, 0) == "—"
    assert format_theta_prem(-0.02, float("nan")) == "—"


def test_ranking_identity_byte_identical_on_fixed_fixture():
    top5 = _fixture_top5()
    before = ranking_identity_bytes(top5)
    attached = attach_chain_greeks(top5, _fixture_vol_curr())
    shown = greeks_display_columns(top5, _fixture_vol_curr())
    after = ranking_identity_bytes(attached)
    after_display = ranking_identity_bytes(
        pd.concat([top5, shown], axis=1)
    )
    assert after == before
    assert after_display == before
    assert attached["Value_Score"].to_numpy().tobytes() == (
        top5["Value_Score"].to_numpy().tobytes()
    )
    assert attached["Action_Signal"].to_numpy().tobytes() == (
        top5["Action_Signal"].to_numpy().tobytes()
    )
    assert list(attached.index) == list(top5.index)
    assert list(attached["strike"]) == list(top5["strike"])
    assert list(attached["side"]) == list(top5["side"])
    assert list(attached["expiry"]) == list(top5["expiry"])
    assert len(attached) == len(top5)
    assert len(shown) == len(top5)


def test_delta_tone_thresholds_display_only():
    assert delta_cell_tone(0.082) == "red"
    assert delta_cell_tone(-0.149) == "red"
    assert delta_cell_tone(0.15) == "amber"
    assert delta_cell_tone(0.249) == "amber"
    assert delta_cell_tone(0.25) == "plain"
    assert delta_cell_tone(0.40) == "plain"
    assert delta_cell_tone(None) is None
    assert format_abs_delta(-0.082) == "0.082"
    assert format_delta_cell(0.082) == "🔴 0.082"
    assert format_delta_cell(0.18) == "🟠 0.180"
    assert format_delta_cell(0.31) == "0.310"


def test_legacy_leg_and_volume_block_persist_provider_greeks():
    chain = pd.DataFrame(
        [
            {
                "side": "CALL",
                "strike": 100.0,
                "expiry": "2026-08-21",
                "dte": 5,
                "last": 0.50,
                "volume": 10,
                "openInterest": 20,
                "iv": 0.3,
                "bid": 0.4,
                "ask": 0.6,
                "delta": 0.082,
                "theta": -0.02,
            }
        ]
    )
    leg = _legacy_option_leg(chain, "CALL")
    assert float(leg.iloc[0]["delta"]) == pytest.approx(0.082)
    assert float(leg.iloc[0]["theta"]) == pytest.approx(-0.02)
    recs = _volume_block_records(leg)
    assert recs[0]["delta"] == pytest.approx(0.082)
    assert recs[0]["theta"] == pytest.approx(-0.02)


def test_legacy_leg_missing_greeks_no_raise():
    chain = pd.DataFrame(
        [
            {
                "side": "PUT",
                "strike": 90.0,
                "expiry": "2026-08-21",
                "dte": 5,
                "last": 1.0,
                "volume": 5,
                "openInterest": 10,
                "iv": 0.3,
                "bid": math.nan,
                "ask": math.nan,
            }
        ]
    )
    leg = _legacy_option_leg(chain, "PUT")
    assert "delta" not in leg.columns
    assert "theta" not in leg.columns
    recs = _volume_block_records(leg)
    assert "delta" not in recs[0]
    assert "theta" not in recs[0]
