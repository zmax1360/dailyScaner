"""Volume page: explorer table logic and Streamlit rendering against a seeded history DB."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import volume_history as vh

ET = ZoneInfo("America/New_York")


def _leg(rows):
    cols = ["strike", "lastPrice", "volume", "openInterest", "impliedVolatility", "bid", "ask", "expiry"]
    return pd.DataFrame([dict(zip(cols, r)) for r in rows])


def _seed(db: str) -> None:
    """3 sessions. 360C Jan builds OI every day; 340C Oct is flat; 335P expired by day 3."""
    days = [("2026-09-28", 1000, 5000), ("2026-09-29", 1600, 5000), ("2026-09-30", 2500, 5000)]
    for i, (d, oi_build, oi_flat) in enumerate(days):
        for hm, frac in (("10:00", 0.3), ("15:45", 1.0)):
            calls = _leg([
                (360.0, 11.8, int(900 * frac), oi_build, 0.258, 11.75, 12.0, "2027-01-15"),
                (340.0, 4.6, int(3000 * frac), oi_flat, 0.203, 4.45, 4.65, "2026-10-02"),
            ])
            puts = _leg([(335.0, 2.1, int(700 * frac), 3000, 0.21, 2.05, 2.15, "2026-09-29")])
            ts = datetime.strptime(f"{d} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=ET)
            vh.record_scan(calls, puts, ticker="AAPL", scan_id=f"AAPL_{ts:%Y%m%d_%H%M%S}", ts=ts, db_path=db)


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    db = str(tmp_path / "vh.db")
    _seed(db)
    monkeypatch.setattr(vh, "DB_PATH", db)
    return db


def test_status_and_latest_scan(seeded):
    s = vh.recording_status("aapl")
    assert s["scans"] == 6 and s["sessions"] == 3
    assert s["last_scan_id"] == "AAPL_20260930_154500"
    assert len(vh.latest_scan("AAPL")) == 3


def test_explorer_table_counts_dte_from_today_and_flags_building(seeded):
    t = vh.explorer_table(vh.latest_scan("AAPL"), vh.daily_summary("AAPL"), today=date(2026, 9, 30))
    assert "2026-09-29" not in set(t["expiry"])                      # expired contract dropped
    jan = t[(t.strike == 360.0)].iloc[0]
    assert jan.dte == 107                                            # from today, not the scan date
    assert jan.oi_change == 900 and jan.oi_up_days == 2
    oct_ = t[(t.strike == 340.0)].iloc[0]
    assert oct_.oi_change == 0 and oct_.oi_up_days == 0
    assert t.iloc[0].strike == 340.0                                 # default sort: volume desc


def test_unknown_previous_oi_gives_unknown_change_not_zero():
    latest = pd.DataFrame([{"side": "CALL", "strike": 360.0, "expiry": "2027-01-15", "volume": 900,
                            "open_interest": 2500, "bid": 1, "ask": 1.1, "last": 1, "iv": 0.25}])
    daily = pd.DataFrame([
        {"side": "CALL", "strike": 360.0, "expiry": "2027-01-15", "session_date": "2026-09-29",
         "volume": 800, "open_interest": None, "last": 1, "iv": 0.25},
        {"side": "CALL", "strike": 360.0, "expiry": "2027-01-15", "session_date": "2026-09-30",
         "volume": 900, "open_interest": 2500, "last": 1, "iv": 0.25},
    ])
    row = vh.explorer_table(latest, daily, today=date(2026, 9, 30)).iloc[0]
    assert pd.isna(row.oi_change) and row.oi_up_days == 0


# ── Streamlit rendering ──────────────────────────────────────────────────────

_SCRIPT = """
import streamlit as st, volume_history as vh, volume_page
from zoneinfo import ZoneInfo
vh.DB_PATH = {db!r}
def greeks(S, K, iv, dte, r=0.05, is_call=True):
    return (0.42 if is_call else -0.42), 0.01, -0.10
{body}
"""


def test_page_renders_empty_state(tmp_path):
    from streamlit.testing.v1 import AppTest

    body = ("volume_page.render_volume_page('AAPL', tz=ZoneInfo('America/New_York'), spot=345.0,"
            " scan_ts=None, greeks_fn=greeks)")
    at = AppTest.from_string(_SCRIPT.format(db=str(tmp_path / "none.db"), body=body)).run()
    assert not at.exception
    assert any("No volume history" in i.value for i in at.info)


def test_page_renders_table_with_filters(seeded):
    from streamlit.testing.v1 import AppTest

    body = ("volume_page.render_volume_page('AAPL', tz=ZoneInfo('America/New_York'), spot=345.0,"
            " scan_ts=None, greeks_fn=greeks)")
    at = AppTest.from_string(_SCRIPT.format(db=seeded, body=body)).run()
    assert not at.exception
    assert len(at.dataframe) == 1
    assert at.number_input(key="vol_min") is not None and at.text_input(key="vol_strike") is not None
    at.number_input(key="vol_min").set_value(2000).run()           # search by volume
    assert not at.exception
    shown = at.dataframe[0].value
    assert list(shown["strike"]) == [340.0]


def test_contract_history_and_pretrade_handoff(seeded):
    from streamlit.testing.v1 import AppTest

    import pre_trade_check

    body = """
import pandas as pd
row = pd.Series({"side": "CALL", "strike": 360.0, "expiry": "2027-01-15", "dte": 107,
                 "volume": 900, "open_interest": 2500, "bid": 11.75, "ask": 12.0, "iv": 0.258})
volume_page._render_contract(row, ticker="AAPL", tz=ZoneInfo("America/New_York"), spot=345.0,
                             scan_ts="2026-09-30T15:45:00-04:00", greeks_fn=greeks)
"""
    at = AppTest.from_string(_SCRIPT.format(db=seeded, body=body)).run()
    assert not at.exception
    btn = at.button(key="vol_check_AAPL")
    assert not btn.disabled
    at = btn.click().run()
    staged = at.session_state[pre_trade_check.ARCHIVE_PREFILL_KEY]["prefill"]
    assert staged["symbol"] == "AAPL" and staged["direction"] == "CALL"
    assert staged["strike"] == 360.0 and staged["expiry"] == "2027-01-15"
    assert staged["bid"] == 11.75 and staged["ask"] == 12.0 and staged["underlying"] == 345.0


def test_pretrade_button_disabled_without_greeks(seeded):
    from streamlit.testing.v1 import AppTest

    body = """
import pandas as pd
row = pd.Series({"side": "CALL", "strike": 340.0, "expiry": "2026-10-02", "dte": 2,
                 "volume": 3000, "open_interest": 5000, "bid": 4.45, "ask": 4.65, "iv": 0.203})
volume_page._render_contract(row, ticker="AAPL", tz=ZoneInfo("America/New_York"), spot=None,
                             scan_ts=None, greeks_fn=greeks)
"""
    at = AppTest.from_string(_SCRIPT.format(db=seeded, body=body)).run()
    assert not at.exception
    assert at.button(key="vol_check_AAPL").disabled                # no spot → no greeks → refuse
