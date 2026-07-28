"""Best Value table selection → pending add-position payload (UI helper)."""

from __future__ import annotations

import logging

import pandas as pd

from best_value_ui import (
    CONTRACT_KEY_COL,
    attach_contract_keys,
    contract_key,
    pending_add_pos_payload,
)


def _sample_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    top5 with a non-trivial index; disp reindexed to the same row order after
    reset — selection index 2 must map to the third contract, not iloc of the
    original index label.
    """
    top5 = pd.DataFrame(
        [
            {"side": "CALL", "strike": 100.0, "expiry": "2026-08-01", "last": 1.10,
             "Status": "BEST VALUE"},
            {"side": "PUT", "strike": 95.0, "expiry": "2026-08-01", "last": 2.20,
             "Status": ""},
            {"side": "CALL", "strike": 105.0, "expiry": "2026-08-08", "last": 0.85,
             "Status": ""},
            {"side": "PUT", "strike": 90.0, "expiry": "2026-08-08", "last": 3.40,
             "Status": ""},
        ],
        index=[10, 3, 7, 1],  # non-contiguous original index
    )
    disp = top5.reset_index(drop=True).rename(
        columns={
            "side": "Side",
            "strike": "Strike",
            "expiry": "Expiry",
            "last": "Price",
        }
    )
    return top5, disp


def test_selected_row_maps_to_correct_contract():
    top5, _disp = _sample_frames()
    # Select visual / positional row 2 → CALL $105 · 2026-08-08 @ 0.85
    payload = pending_add_pos_payload("AAPL", top5, [2])
    assert payload is not None
    assert payload["Side"] == "CALL"
    assert payload["Strike"] == 105.0
    assert payload["Expiry"] == "2026-08-08"
    assert payload["default_price"] == 0.85
    # Must not accidentally resolve via original index label 2 (missing) or
    # first row
    assert payload["Strike"] != 100.0


def test_pending_payload_keys_unchanged():
    top5, _ = _sample_frames()
    payload = pending_add_pos_payload("msft", top5, [0])
    assert payload is not None
    assert set(payload.keys()) == {
        "Ticker", "Side", "Strike", "Expiry", "default_price",
    }
    assert payload["Ticker"] == "MSFT"


def test_no_selection_writes_nothing():
    top5, _ = _sample_frames()
    assert pending_add_pos_payload("AAPL", top5, []) is None
    assert pending_add_pos_payload("AAPL", top5, None) is None
    assert pending_add_pos_payload("AAPL", top5, [99]) is None


def test_selection_survives_display_reordering():
    """
    Regression: after a column sort the display order ≠ top5 order.
    Selecting display row 0 must resolve the contract *shown* there (last of
    top5 when reversed), not top5.iloc[0].
    """
    top5, _ = _sample_frames()
    top5_r = top5.reset_index(drop=True)
    disp = top5_r.iloc[::-1].reset_index(drop=True).rename(
        columns={
            "side": "Side",
            "strike": "Strike",
            "expiry": "Expiry",
            "last": "Price",
        }
    )
    disp[CONTRACT_KEY_COL] = attach_contract_keys(top5_r).iloc[::-1].reset_index(drop=True)

    # Display row 0 is former last row: PUT $90 · 2026-08-08 @ 3.40
    payload = pending_add_pos_payload("AAPL", top5, [0], display=disp)
    assert payload is not None
    assert payload["Side"] == "PUT"
    assert payload["Strike"] == 90.0
    assert payload["Expiry"] == "2026-08-08"
    assert payload["default_price"] == 3.40
    # Positional bug would have returned CALL $100
    assert payload["Strike"] != 100.0


def test_selection_by_key_missing_returns_none():
    top5, _ = _sample_frames()
    top5_r = top5.reset_index(drop=True)
    disp = top5_r.rename(
        columns={
            "side": "Side",
            "strike": "Strike",
            "expiry": "Expiry",
            "last": "Price",
        }
    ).copy()
    disp[CONTRACT_KEY_COL] = ["MISSING|0.0000|1999-01-01"] * len(disp)
    assert pending_add_pos_payload("AAPL", top5, [0], display=disp) is None


def test_duplicate_keys_fall_back_to_position(caplog):
    top5 = pd.DataFrame(
        [
            {"side": "CALL", "strike": 100.0, "expiry": "2026-08-01", "last": 1.10},
            {"side": "CALL", "strike": 100.0, "expiry": "2026-08-01", "last": 9.99},
            {"side": "PUT", "strike": 95.0, "expiry": "2026-08-01", "last": 2.20},
        ]
    )
    disp = top5.rename(
        columns={
            "side": "Side",
            "strike": "Strike",
            "expiry": "Expiry",
            "last": "Price",
        }
    ).copy()
    disp[CONTRACT_KEY_COL] = attach_contract_keys(top5).values
    assert contract_key("CALL", 100.0, "2026-08-01") == disp.iloc[0][CONTRACT_KEY_COL]
    assert disp.iloc[0][CONTRACT_KEY_COL] == disp.iloc[1][CONTRACT_KEY_COL]

    with caplog.at_level(logging.WARNING, logger="best_value_ui"):
        # Select display row 1 → duplicate key → positional fallback → last=9.99
        payload = pending_add_pos_payload("AAPL", top5, [1], display=disp)
    assert payload is not None
    assert payload["default_price"] == 9.99
    assert any("duplicate Best Value contract key" in r.message for r in caplog.records)
