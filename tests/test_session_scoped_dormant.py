"""Step 7 — session-scoped sources put rollover detectors to sleep."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytz

from best_value import attach_dvol
from chain_quality import rollover_detectors_active

ET = pytz.timezone("US/Eastern")


def test_rollover_detectors_dormant_when_session_scoped():
    assert rollover_detectors_active(False) is True
    assert rollover_detectors_active(True) is False


def test_attach_dvol_skips_detectors_when_session_scoped():
    now = ET.localize(datetime(2026, 7, 28, 9, 31))
    df = pd.DataFrame([{
        "side": "CALL", "strike": 340.0, "expiry": "2026-07-29",
        "volume": 36654, "last": 1.0, "openInterest": 1000, "dte": 1, "iv": 0.3,
    }])
    prev = {"top_calls": [{"strike": 340.0, "expiry": "2026-07-29", "volume": 35000}],
            "top_puts": []}
    eod = {("CALL", 340.0, "2026-07-29"): 35033}
    out = attach_dvol(
        df, prev, eod_vol_lookup=eod, now_et=now,
        volume_is_session_scoped=True,
    )
    assert out.attrs.get("rollover_detectors") == "dormant_session_scoped"
    assert bool(out.loc[0, "stale_volume"]) is False
    assert bool(out.loc[0, "dvol_suspect"]) is False
