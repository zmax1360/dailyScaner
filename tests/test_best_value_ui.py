"""Best Value table selection → pending add-position payload (UI helper)."""

from __future__ import annotations

import pandas as pd

from best_value_ui import pending_add_pos_payload


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
