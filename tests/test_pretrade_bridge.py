"""Check this bridge: scanner row → Pre-Trade prefill. No UI, no scoring."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import pre_trade_check as ptc
from best_value_ui import contract_key
from pre_trade_check import (
    CHART_FIELDS,
    PREFILL_CONTRACT_FIELDS,
    STALE_QUOTE_MINUTES,
    PreTradeInputs,
    apply_candidate_to_session,
    build_scan_snapshot,
    candidate_ref,
    compute_pre_trade,
    contract_prefill_from_row,
    format_prefill_banner,
    is_stale,
    load_scan_contract,
    make_scan_id,
    parse_candidate_ref,
    prefill_overrides,
    save_check,
    scan_age_minutes,
    staleness_warning,
    store_scan_snapshot,
)

ET = ZoneInfo("America/New_York")

CHART_KEYS = (
    "underlying",
    "target_distance",
    "invalidation_distance",
    "hold_hours",
    "entry_window",
)


def _row(**kw):
    base = dict(
        side="CALL",
        strike=180.0,
        expiry="2026-08-21",
        dte=2,
        last=1.07,
        bid=1.05,
        ask=1.10,
        delta=0.40,
        openInterest=4200,
        Value_Score=0.31,
        # Poison pills — must never leak into prefill
        underlying=177.5,
        target_distance=2.0,
        invalidation_distance=1.0,
        hold_hours=1.0,
        entry_window="09:45–11:30",
        atr=1.25,
        spot=177.5,
    )
    base.update(kw)
    return base


def test_parse_candidate_ref():
    assert parse_candidate_ref("20260819T143200:CALL|180.0000|2026-08-21") == (
        "20260819T143200",
        "CALL|180.0000|2026-08-21",
    )
    assert parse_candidate_ref("") is None
    assert parse_candidate_ref(None) is None
    assert parse_candidate_ref("no-colon") is None
    assert parse_candidate_ref(":missing-scan") is None
    assert parse_candidate_ref("scan:") is None


def test_scan_id_has_no_colon():
    sid = make_scan_id("2026-08-19 14:32:00 ET")
    assert sid == "20260819T143200"
    assert ":" not in sid
    ref = candidate_ref(sid, "CALL|180.0000|2026-08-21")
    parsed = parse_candidate_ref(ref)
    assert parsed is not None
    assert parsed[0] == sid
    assert parsed[1] == "CALL|180.0000|2026-08-21"


def test_prefill_is_contract_half_only():
    prefill = contract_prefill_from_row(_row(), ticker="nvda")
    assert prefill["symbol"] == "NVDA"
    assert prefill["direction"] == "CALL"
    assert prefill["strike"] == 180.0
    assert prefill["dte"] == 2
    assert prefill["bid"] == 1.05
    assert prefill["ask"] == 1.10
    assert prefill["delta"] == pytest.approx(0.40)
    assert prefill["open_interest"] == 4200
    assert prefill["theta"] is None
    assert prefill["theta_units"] == "per day"
    for key in CHART_KEYS:
        assert key not in prefill
    assert set(prefill) <= set(PREFILL_CONTRACT_FIELDS)
    assert CHART_FIELDS.isdisjoint(prefill)


def test_prefill_does_not_invent_theta():
    prefill = contract_prefill_from_row(_row(), ticker="AAPL")
    assert "theta" in prefill
    assert prefill["theta"] is None


def test_prefill_overrides_detects_bid_refresh():
    original = contract_prefill_from_row(_row(), ticker="NVDA")
    current = dict(original)
    current["bid"] = 1.08
    current["ask"] = 1.12
    out = prefill_overrides(original, current)
    assert set(out) == {"bid", "ask"}
    assert out["bid"] == {"from": 1.05, "to": 1.08}
    assert out["ask"] == {"from": 1.10, "to": 1.12}
    assert "strike" not in out
    assert "target_distance" not in out
    assert "underlying" not in out


def test_prefill_overrides_ignore_blank_theta():
    original = contract_prefill_from_row(_row(), ticker="NVDA")
    current = dict(original)
    current["theta"] = 0.0
    assert prefill_overrides(original, current) == {}


def test_banner_matches_spec():
    ts = datetime(2026, 8, 19, 14, 32, tzinfo=ET)
    assert format_prefill_banner(ts, 4, 0.31) == (
        "From scan 2026-08-19 14:32 · rank 04 · score 0.31"
    )


def test_staleness_threshold():
    scan = datetime(2026, 8, 19, 14, 32, tzinfo=ET)
    now = scan + timedelta(minutes=10)
    assert is_stale(scan, now) is False
    assert staleness_warning(scan, now) is None
    now = scan + timedelta(minutes=11)
    assert is_stale(scan, now) is True
    assert STALE_QUOTE_MINUTES == 10
    msg = staleness_warning(scan, now)
    assert msg == (
        "quotes are 11 minutes old — refresh bid/ask before trusting the ratio."
    )
    assert scan_age_minutes(scan, now) == pytest.approx(11.0)


def test_build_snapshot_rank_score_and_no_chart_fields():
    scored = pd.DataFrame([
        _row(side="PUT", strike=175.0, expiry="2026-08-21", Value_Score=0.09,
             bid=0.80, ask=0.90, delta=-0.22, openInterest=800),
        _row(side="CALL", strike=180.0, expiry="2026-08-21", Value_Score=0.31,
             bid=1.05, ask=1.10, delta=0.40, openInterest=4200),
        _row(side="CALL", strike=182.5, expiry="2026-08-21", Value_Score=0.22,
             bid=0.70, ask=0.78, delta=0.33, openInterest=1500),
    ])
    ranked = scored.sort_values("Value_Score", ascending=False)
    display_only = ranked.drop(columns=["bid", "ask", "delta"], errors="ignore")
    snap = build_scan_snapshot(
        "NVDA", scored, display_only, "2026-08-19 14:32:00 ET",
    )
    assert snap["scan_id"] == "20260819T143200"
    cid = contract_key("CALL", 180.0, "2026-08-21")
    entry = snap["contracts"][cid]
    assert entry["rank"] == 1
    assert entry["score"] == pytest.approx(0.31)
    prefill = entry["prefill"]
    assert prefill["bid"] == 1.05
    assert prefill["ask"] == 1.10
    assert prefill["delta"] == pytest.approx(0.40)
    assert prefill["theta"] is None
    for key in CHART_KEYS:
        assert key not in prefill
    # rank 04 style: fourth would be 4; third-ranked here is the PUT
    put_cid = contract_key("PUT", 175.0, "2026-08-21")
    assert snap["contracts"][put_cid]["rank"] == 3


def test_store_and_load_scan_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(ptc, "SCANS_PATH", str(tmp_path / "scans.json"))
    scored = pd.DataFrame([_row()])
    snap = build_scan_snapshot("NVDA", scored, scored, "2026-08-19 14:32:00 ET")
    store_scan_snapshot(snap)
    cid = contract_key("CALL", 180.0, "2026-08-21")
    loaded = load_scan_contract(snap["scan_id"], cid)
    assert loaded is not None
    assert loaded["rank"] == 1
    assert loaded["score"] == pytest.approx(0.31)
    assert loaded["prefill"]["symbol"] == "NVDA"
    for key in CHART_KEYS:
        assert key not in loaded["prefill"]
    assert load_scan_contract(snap["scan_id"], "MISSING") is None


def test_apply_candidate_leaves_chart_blank():
    class _SS(dict):
        pass

    class _St:
        session_state = _SS()

    payload = {
        "scan_id": "20260819T143200",
        "contract_id": "CALL|180.0000|2026-08-21",
        "scan_ts": "2026-08-19T14:32:00-04:00",
        "rank": 4,
        "score": 0.31,
        "prefill": contract_prefill_from_row(_row(), ticker="NVDA"),
    }
    payload["prefill"]["target_distance"] = 2.0
    payload["prefill"]["invalidation_distance"] = 1.0
    payload["prefill"]["underlying"] = 177.5
    apply_candidate_to_session(_St, payload)
    ss = _St.session_state
    assert ss["ptc_symbol"] == "NVDA"
    assert ss["ptc_direction"] == "CALL"
    assert ss["ptc_strike"] == 180.0
    assert ss["ptc_bid"] == 1.05
    assert ss["ptc_ask"] == 1.10
    assert ss["ptc_delta"] == pytest.approx(0.40)
    assert ss["ptc_dte"] == 2
    assert ss["ptc_oi"] == 4200.0
    assert ss["ptc_theta"] == 0.0
    assert ss["ptc_underlying"] == 0.0
    assert ss["ptc_target"] == 0.0
    assert ss["ptc_invalidation"] == 0.0
    assert ss["ptc_hold"] == 0.0
    assert "ptc_target" in ss
    orig = ss["ptc_prefill_original"]
    for key in CHART_KEYS:
        assert key not in orig
    assert ss["ptc_scan_meta"]["scanner_rank"] == 4


def test_save_check_stores_scan_link(tmp_path, monkeypatch):
    monkeypatch.setattr(ptc, "CHECKS_PATH", str(tmp_path / "checks.json"))
    result = compute_pre_trade(PreTradeInputs(symbol="NVDA", direction="CALL"))
    row = save_check(
        result,
        scan_id="20260819T143200",
        contract_id="CALL|180.0000|2026-08-21",
        scanner_rank=4,
        scanner_score=0.31,
        prefill_overrides={"bid": {"from": 1.05, "to": 1.08}},
    )
    assert row["scan_id"] == "20260819T143200"
    assert row["contract_id"] == "CALL|180.0000|2026-08-21"
    assert row["scanner_rank"] == 4
    assert row["scanner_score"] == pytest.approx(0.31)
    assert row["prefill_overrides"]["bid"]["to"] == 1.08
    blank = save_check(result)
    assert blank["scan_id"] is None
    assert blank["contract_id"] is None
    assert blank["scanner_rank"] is None
    assert blank["scanner_score"] is None
    assert blank["prefill_overrides"] == {}
