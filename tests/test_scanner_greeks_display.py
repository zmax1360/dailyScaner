"""Display-only Delta and Theta/Prem — must not touch ranking."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from best_value_ui import (
    DISPLAY_DELTA_MAX,
    DISPLAY_DELTA_MIN,
    attach_chain_greeks,
    delta_cell_tone,
    delta_in_pretrade_band,
    filter_ranked_display,
    format_abs_delta,
    format_delta_cell,
    format_theta_prem,
    greeks_display_columns,
    hidden_delta_band_caption,
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


def _band_top5() -> pd.DataFrame:
    """Ranked list mixing in-band, out-of-band, null, and signed deltas."""
    return pd.DataFrame(
        [
            {  # scoring delta in-band, provider lottery — must HIDE
                "side": "CALL", "strike": 300.0, "expiry": "2026-08-21",
                "last": 0.20, "delta": 0.40, "Value_Score": 0.40,
                "Action_Signal": "(0)",
            },
            {  # provider |δ|=0.42 — KEEP
                "side": "PUT", "strike": 295.0, "expiry": "2026-08-21",
                "last": 1.10, "delta": -0.10, "Value_Score": 0.30,
                "Action_Signal": "(0)",
            },
            {  # provider |δ|=0.35 inclusive — KEEP
                "side": "CALL", "strike": 302.5, "expiry": "2026-08-21",
                "last": 0.80, "delta": 0.99, "Value_Score": 0.25,
                "Action_Signal": "(0)",
            },
            {  # provider |δ|=0.50 inclusive — KEEP
                "side": "PUT", "strike": 290.0, "expiry": "2026-08-21",
                "last": 2.00, "delta": 0.01, "Value_Score": 0.20,
                "Action_Signal": "(0)",
            },
            {  # provider |δ|=0.51 — HIDE
                "side": "CALL", "strike": 305.0, "expiry": "2026-08-21",
                "last": 1.50, "delta": 0.40, "Value_Score": 0.15,
                "Action_Signal": "(0)",
            },
            {  # missing from chain → null delta — HIDE
                "side": "PUT", "strike": 280.0, "expiry": "2026-08-21",
                "last": 3.00, "delta": 0.45, "Value_Score": 0.10,
                "Action_Signal": "(0)",
            },
        ]
    )


def _band_vol_curr() -> dict:
    return {
        "top_calls": [
            {"strike": 300.0, "expiry": "2026-08-21", "delta": 0.082, "theta": -0.02},
            {"strike": 302.5, "expiry": "2026-08-21", "delta": 0.35, "theta": -0.03},
            {"strike": 305.0, "expiry": "2026-08-21", "delta": 0.51, "theta": -0.04},
        ],
        "top_puts": [
            {"strike": 295.0, "expiry": "2026-08-21", "delta": -0.42, "theta": -0.05},
            {"strike": 290.0, "expiry": "2026-08-21", "delta": -0.50, "theta": -0.06},
            # 280.0 omitted → null chain_delta
        ],
    }


def test_display_band_matches_pretrade_gate():
    from pre_trade_check import DELTA_MAX, DELTA_MIN

    assert DISPLAY_DELTA_MIN == DELTA_MIN == 0.35
    assert DISPLAY_DELTA_MAX == DELTA_MAX == 0.50


def test_filtered_view_only_pretrade_delta_band():
    top5 = _band_top5()
    vol = _band_vol_curr()
    before = ranking_identity_bytes(top5)

    vis, n_hidden, n_ranked = filter_ranked_display(top5, vol, show_all=False)
    assert n_ranked == 6
    assert n_hidden == 3
    assert len(vis) == 3
    assert hidden_delta_band_caption(n_hidden, n_ranked) == (
        "3 of 6 hidden: outside delta band"
    )
    assert hidden_delta_band_caption(18, 47) == (
        "18 of 47 hidden: outside delta band"
    )

    attached = attach_chain_greeks(vis, vol)
    for d in attached["chain_delta"]:
        mag = abs(d)
        assert 0.35 <= mag <= 0.50
    strikes = list(vis["strike"])
    assert strikes == [295.0, 302.5, 290.0]
    # Scoring delta must not keep the lottery row or the null-delta row.
    assert 300.0 not in strikes
    assert 280.0 not in strikes
    assert 305.0 not in strikes
    # Original ranked frame unchanged (attribution/scoring identity).
    assert ranking_identity_bytes(top5) == before
    assert len(top5) == 6


def test_null_delta_excluded_from_display_filter():
    top5 = _band_top5()
    vol = _band_vol_curr()
    vis, n_hidden, n_ranked = filter_ranked_display(top5, vol)
    attached_all = attach_chain_greeks(top5, vol)
    null_rows = attached_all["chain_delta"].map(
        lambda v: v is None or (isinstance(v, float) and v != v)
    )
    assert null_rows.any()
    vis_keys = set(zip(vis["side"], vis["strike"], vis["expiry"]))
    for _, row in top5[null_rows.to_numpy()].iterrows():
        assert (row["side"], row["strike"], row["expiry"]) not in vis_keys
    assert not delta_in_pretrade_band(None)
    assert not delta_in_pretrade_band(float("nan"))
    assert n_hidden + len(vis) == n_ranked


def test_show_all_restores_full_ranked_list():
    top5 = _band_top5()
    vol = _band_vol_curr()
    vis, n_hidden, n_ranked = filter_ranked_display(
        top5, vol, show_all=True,
    )
    assert n_hidden == 0
    assert n_ranked == 6
    assert len(vis) == 6
    assert list(vis["strike"]) == list(top5["strike"])
    assert ranking_identity_bytes(vis) == ranking_identity_bytes(top5)
    # Filter still hides when show_all is off.
    hidden_view, n_hid, _ = filter_ranked_display(top5, vol, show_all=False)
    assert n_hid > 0
    assert len(hidden_view) < len(vis)


def test_delta_band_boundaries_and_signed():
    assert delta_in_pretrade_band(0.35) is True
    assert delta_in_pretrade_band(-0.35) is True
    assert delta_in_pretrade_band(0.50) is True
    assert delta_in_pretrade_band(-0.50) is True
    assert delta_in_pretrade_band(0.42) is True
    assert delta_in_pretrade_band(-0.42) is True
    assert delta_in_pretrade_band(0.349) is False
    assert delta_in_pretrade_band(0.501) is False
    assert delta_in_pretrade_band(0.082) is False
    assert delta_in_pretrade_band(-0.14) is False
    assert delta_in_pretrade_band(0.0) is False
