"""Notify path must not publish results the scan refused to produce."""

from __future__ import annotations

import io
import logging
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from dailyScaner import ScanRefused, abort_scan
from notify_delivery import (
    ARCHIVE_MAX_AGE_MIN,
    REFUSAL_STREAK_ALERT_AT,
    archive_is_fresh,
    flow_dispersion_iqr,
    parse_abort_reason,
    provenance_line,
    ranking_has_signal,
    serialize_best_value_rows,
)
import scheduler as sched
from telegram_bot import _fmt_report

ET = ZoneInfo("America/New_York")


def test_abort_exits_code_3_with_reason_quality_gate():
    with pytest.raises(ScanRefused) as ei:
        abort_scan("quality_gate")
    assert ei.value.reason == "quality_gate"


def test_abort_exits_code_3_with_reason_volume_rollover(capsys):
    with pytest.raises(ScanRefused):
        abort_scan("volume_rollover")
    err = capsys.readouterr().err
    assert "ABORT_REASON=volume_rollover" in err


def test_abort_exits_code_3_with_reason_majority_stale(capsys):
    with pytest.raises(ScanRefused):
        abort_scan("majority_stale")
    assert "ABORT_REASON=majority_stale" in capsys.readouterr().err


def test_unhandled_exception_still_exits_1():
    """Genuine errors remain exit 1 — ScanRefused is the only exit-3 path."""
    assert issubclass(ScanRefused, Exception)
    assert ScanRefused("quality_gate").reason == "quality_gate"


def test_parse_abort_reason():
    assert parse_abort_reason("ABORT_REASON=quality_gate\n") == "quality_gate"
    assert parse_abort_reason("noise\nABORT_REASON=majority_stale") == "majority_stale"
    assert parse_abort_reason("nope") is None


def test_scheduler_skips_notify_on_exit_3(caplog):
    sched._reset_refusal_streak("AAPL")
    with patch.object(sched, "_notify_success") as ok, \
         patch.object(sched, "_notify_refusal_streak") as streak:
        with caplog.at_level(logging.INFO):
            sched.handle_scan_result(
                "AAPL", 3, "quality_gate", 5.0,
                notify=True, env={},
            )
        ok.assert_not_called()
        streak.assert_not_called()  # streak==1
        assert any("REFUSED (quality_gate)" in r.message for r in caplog.records)


def test_scheduler_sends_one_message_after_3_consecutive_refusals():
    sched._reset_refusal_streak("AAPL")
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
    with patch.object(sched, "_notify_success") as ok, \
         patch.object(sched, "_notify_refusal_streak") as streak:
        for _ in range(REFUSAL_STREAK_ALERT_AT - 1):
            sched.handle_scan_result(
                "AAPL", 3, "quality_gate", 1.0, notify=True, env=env,
            )
        streak.assert_not_called()
        sched.handle_scan_result(
            "AAPL", 3, "quality_gate", 1.0, notify=True, env=env,
        )
        streak.assert_called_once()
        ok.assert_not_called()


def test_scheduler_stays_silent_on_4th_consecutive_refusal():
    sched._reset_refusal_streak("AAPL")
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
    with patch.object(sched, "_notify_refusal_streak") as streak:
        for _ in range(REFUSAL_STREAK_ALERT_AT + 1):
            sched.handle_scan_result(
                "AAPL", 3, "quality_gate", 1.0, notify=True, env=env,
            )
        assert streak.call_count == 1


def test_streak_resets_after_a_successful_scan():
    sched._reset_refusal_streak("AAPL")
    env = {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_CHAT_ID": "1"}
    with patch.object(sched, "_notify_success") as ok, \
         patch.object(sched, "_notify_refusal_streak") as streak:
        for _ in range(REFUSAL_STREAK_ALERT_AT):
            sched.handle_scan_result(
                "AAPL", 3, "quality_gate", 1.0, notify=True, env=env,
            )
        assert streak.call_count == 1
        sched.handle_scan_result(
            "AAPL", 0, None, 2.0, notify=True, env=env,
        )
        ok.assert_called_once()
        assert sched._refusal_streak["AAPL"] == 0
        assert sched._refusal_alert_sent["AAPL"] is False
        # New streak can alert again
        for _ in range(REFUSAL_STREAK_ALERT_AT):
            sched.handle_scan_result(
                "AAPL", 3, "volume_rollover", 1.0, notify=True, env=env,
            )
        assert streak.call_count == 2


def _fresh_payload_with_bv(*, signal: bool = True) -> dict:
    now = datetime.now(ET)
    rows = []
    for i in range(8):
        rows.append({
            "side": "PUT",
            "strike": 300.0 - i,
            "expiry": "2026-08-15",
            "last": 1.0 + i * 0.1,
            "volume": 5000,
            "openInterest": 1000,
            "dVol": 100.0 * (i + 1) if signal else None,
            "Value_Score": 0.5 - i * 0.01,
            "Status": "⭐ BEST VALUE" if i == 0 else "",
            "_nflow": 0.1 * i if signal else 0.5,
            "_nlev": 0.5,
        })
    if signal:
        # force spread in _nflow for IQR
        for i, r in enumerate(rows):
            r["_nflow"] = i / (len(rows) - 1)
    return {
        "timestamp": now.isoformat(),
        "spot": 305.0,
        "direction": "BEARISH",
        "volume": {"pc_ratio": 1.0, "top_calls": [], "top_puts": []},
        "best_value": {
            "rows": rows,
            "n_scored": len(rows),
            "flow_dispersion": 0.5 if signal else 0.01,
            "engine_sha": "dc2906741dbb2b15",
        },
    }


def test_bot_does_not_recompute_best_value():
    payload = _fresh_payload_with_bv(signal=True)
    # If the bot tried to re-score it would import / call build_best_value_df
    with patch("telegram_bot.build_best_value_df", create=True) as bv:
        text = _fmt_report(
            payload, None, "AAPL", 5,
            include={"best_value": True},
            expiry_drill=[],
        )
        bv.assert_not_called()
    assert "BEST VALUE OPTION" in text
    assert "engine dc290674" in text


def test_bot_omits_best_value_when_archive_stale():
    payload = _fresh_payload_with_bv(signal=True)
    old = datetime.now(ET) - timedelta(minutes=ARCHIVE_MAX_AGE_MIN + 5)
    payload["timestamp"] = old.isoformat()
    text = _fmt_report(
        payload, None, "AAPL", 5,
        include={"best_value": True},
        expiry_drill=[],
    )
    assert "BEST VALUE OPTION" not in text
    assert "omitted" in text.lower()


def test_no_signal_ranking_is_not_published():
    payload = _fresh_payload_with_bv(signal=False)
    # flatten flow + null dVol
    for r in payload["best_value"]["rows"]:
        r["dVol"] = None
        r["_nflow"] = 0.5
    payload["best_value"]["flow_dispersion"] = 0.0
    ok, why = ranking_has_signal(payload["best_value"])
    assert ok is False
    text = _fmt_report(
        payload, None, "AAPL", 5,
        include={"best_value": True},
        expiry_drill=[],
    )
    assert "BEST VALUE OPTION" not in text
    assert "no signal" in text.lower() or "omitted" in text.lower()


def test_provenance_line_present_on_every_best_value_message():
    payload = _fresh_payload_with_bv(signal=True)
    text = _fmt_report(
        payload, None, "AAPL", 5,
        include={"best_value": True},
        expiry_drill=[],
    )
    assert "BEST VALUE OPTION" in text
    assert "contracts scored" in text
    assert "flow dispersion" in text
    assert "engine dc290674" in text
    # Missing provenance → omit section
    payload["best_value"]["engine_sha"] = None
    assert provenance_line(payload["best_value"], payload) is None
    text2 = _fmt_report(
        payload, None, "AAPL", 5,
        include={"best_value": True},
        expiry_drill=[],
    )
    assert "BEST VALUE OPTION" not in text2


def test_serialize_and_freshness_helpers():
    df = pd.DataFrame([
        {
            "side": "CALL", "strike": 310.0, "expiry": "2026-08-15",
            "last": 1.5, "volume": 2000, "openInterest": 500,
            "dVol": 100.0, "Value_Score": 0.4, "Status": "⭐ BEST VALUE",
            "_nflow": 0.1, "_nlev": 0.5,
        },
        {
            "side": "PUT", "strike": 300.0, "expiry": "2026-08-15",
            "last": 2.0, "volume": 3000, "openInterest": 800,
            "dVol": 200.0, "Value_Score": 0.3, "Status": "",
            "_nflow": 0.9, "_nlev": 0.4,
        },
    ] + [
        {
            "side": "PUT", "strike": 295.0 - i, "expiry": "2026-08-15",
            "last": 1.0, "volume": 1000, "openInterest": 500,
            "dVol": 50.0, "Value_Score": 0.2 - i * 0.01, "Status": "",
            "_nflow": 0.2 + i * 0.1, "_nlev": 0.3,
        }
        for i in range(6)
    ])
    snap = serialize_best_value_rows(df)
    assert snap["n_scored"] >= 5
    assert snap["engine_sha"]
    assert archive_is_fresh({
        "timestamp": datetime.now(ET).isoformat(),
        "chain_volume_rollover": False,
    })
    assert not archive_is_fresh({
        "timestamp": datetime.now(ET).isoformat(),
        "chain_volume_rollover": True,
    })


def test_run_scan_returns_exit_code_tuple():
    """run_scan contract: (code, reason, elapsed)."""
    fake = MagicMock()
    fake.returncode = 3
    fake.stderr = "ABORT_REASON=quality_gate\n"
    fake.stdout = ""
    with patch("scheduler.subprocess.run", return_value=fake):
        code, reason, elapsed = sched.run_scan("AAPL")
    assert code == 3
    assert reason == "quality_gate"
    assert elapsed >= 0
