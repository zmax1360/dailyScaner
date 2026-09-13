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
        hold_hours=9.0,
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
    assert prefill["hold_hours"] == 1.0
    for key in CHART_KEYS:
        assert key not in prefill
    assert set(prefill) <= set(PREFILL_CONTRACT_FIELDS)
    assert CHART_FIELDS.intersection(prefill) <= {"hold_hours"}


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
    assert loaded["prefill"]["hold_hours"] == 1.0
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
    assert ss["ptc_hold"] == 1.0
    assert ss["ptc_pattern"] == ""
    assert "ptc_target" in ss
    orig = ss["ptc_prefill_original"]
    for key in CHART_KEYS:
        assert key not in orig
    assert orig["hold_hours"] == 1.0
    assert ss["ptc_scan_meta"]["scanner_rank"] == 4


def _archive_row(**kw):
    base = dict(
        Side="CALL",
        Strike="$180.0",
        Expiry="2026-08-28",
        DTE="2d",
        Price="$1.07",
        IV="28.0%",
        _strike=180.0,
        _dte=2,
        _delta=0.41,
        _theta=-0.0520,
        _theta_units="per day",
        _oi=3100,
        lastPrice=1.07,
    )
    base.update(kw)
    return base


class _St:
    query_params: dict = {}

    def __init__(self):
        self.session_state = {}
        self.query_params = {}
        self.markdowns: list[str] = []

    def markdown(self, html, **_kw):
        self.markdowns.append(html)


def test_archive_prefill_mapping():
    prefill = ptc.archive_prefill_from_row(
        _archive_row(), ticker="aapl", underlying=177.55,
    )
    assert prefill is not None
    assert prefill["symbol"] == "AAPL"
    assert prefill["direction"] == "CALL"
    assert prefill["strike"] == 180.0
    assert prefill["expiry"] == "2026-08-28"
    assert prefill["dte"] == 2
    assert prefill["delta"] == pytest.approx(0.41)
    assert prefill["theta"] == pytest.approx(-0.0520)
    assert prefill["theta_units"] == "per day"
    assert prefill["open_interest"] == 3100
    assert prefill["underlying"] == pytest.approx(177.55)
    assert prefill["bid"] is None
    assert prefill["ask"] is None
    assert prefill["hold_hours"] == 1.0
    for key in ("target_distance", "invalidation_distance", "entry_window"):
        assert key not in prefill


def test_archive_prefill_uses_numeric_bid_ask():
    ts = "2026-08-27T09:45:00-04:00"
    prefill = ptc.archive_prefill_from_row(
        _archive_row(_bid=1.05, _ask=1.12, Bid="$9.99", Ask="$9.99"),
        ticker="AAPL",
        underlying=177.55,
        scan_ts=ts,
    )
    assert prefill is not None
    assert prefill["bid"] == pytest.approx(1.05)
    assert prefill["ask"] == pytest.approx(1.12)
    assert prefill["scan_ts"] == ts
    assert ptc.archive_greeks_refusal(prefill["delta"], prefill["theta"]) is None


def test_archive_prefill_ignores_display_bid_ask_cells():
    prefill = ptc.archive_prefill_from_row(
        _archive_row(Bid="$1.05", Ask="$1.12", bid=1.05, ask=1.12),
        ticker="AAPL",
        underlying=177.55,
    )
    assert prefill is not None
    assert prefill["bid"] is None
    assert prefill["ask"] is None


def test_archive_prefill_blank_when_quote_pair_incomplete():
    for kw in (
        dict(_bid=1.05, _ask=None),
        dict(_bid=None, _ask=1.12),
        dict(_bid=0.0, _ask=1.12),
        dict(_bid=1.05, _ask=0.0),
        dict(_bid=float("nan"), _ask=1.12),
        dict(_bid=1.05, _ask=float("nan")),
    ):
        prefill = ptc.archive_prefill_from_row(
            _archive_row(**kw), ticker="AAPL", underlying=177.55,
        )
        assert prefill is not None, kw
        assert prefill["bid"] is None, kw
        assert prefill["ask"] is None, kw
        assert ptc.archive_greeks_refusal(0.41, -0.052) is None


def test_archive_prefill_reads_theta_units():
    hourly = ptc.archive_prefill_from_row(
        _archive_row(_theta_units="per hour", _theta=-0.008),
        ticker="AAPL", underlying=170.0,
    )
    assert hourly is not None
    assert hourly["theta_units"] == "per hour"
    assert hourly["theta"] == pytest.approx(-0.008)
    missing_units = ptc.archive_prefill_from_row(
        _archive_row(_theta_units=""),
        ticker="AAPL", underlying=170.0,
    )
    assert missing_units is None


def test_archive_null_greeks_refused():
    assert ptc.archive_greeks_refusal(None, -0.05)
    assert ptc.archive_greeks_refusal(0.41, None)
    assert ptc.archive_greeks_refusal(0.0, -0.05)
    assert ptc.archive_greeks_refusal(0.41, 0.0)
    assert ptc.archive_greeks_refusal(float("nan"), -0.05)
    assert ptc.archive_prefill_from_row(
        _archive_row(_delta=None, _theta=None, IV="0.0%"),
        ticker="AAPL", underlying=170.0,
    ) is None
    assert ptc.archive_prefill_from_row(
        _archive_row(_theta=0.0),
        ticker="AAPL", underlying=170.0,
    ) is None
    assert ptc.archive_greeks_refusal(0.41, -0.05) is None


def test_archive_prefill_session_cleared_after_consume():
    st = _St()
    prefill = ptc.archive_prefill_from_row(
        _archive_row(), ticker="AAPL", underlying=177.55,
    )
    pid = ptc.stage_archive_prefill(
        st, prefill, contract_id="CALL|180.0000|2026-08-28",
    )
    assert ptc.ARCHIVE_PREFILL_KEY in st.session_state
    assert st.session_state[ptc.ARCHIVE_PREFILL_KEY]["id"] == pid

    consumed = ptc.consume_archive_prefill(st)
    assert consumed is not None
    assert ptc.ARCHIVE_PREFILL_KEY not in st.session_state
    ss = st.session_state
    assert ss["ptc_symbol"] == "AAPL"
    assert ss["ptc_direction"] == "CALL"
    assert ss["ptc_strike"] == 180.0
    assert ss["ptc_dte"] == 2
    assert ss["ptc_delta"] == pytest.approx(0.41)
    assert ss["ptc_theta"] == pytest.approx(-0.0520)
    assert ss["ptc_theta_units"] == "per day"
    assert ss["ptc_oi"] == 3100.0
    assert ss["ptc_underlying"] == pytest.approx(177.55)
    assert ss["ptc_bid"] == 0.0
    assert ss["ptc_ask"] == 0.0
    assert ss["ptc_prefill_original"]["bid"] is None
    assert ss["ptc_prefill_original"]["ask"] is None
    assert ss["ptc_target"] == 0.0
    assert ss["ptc_invalidation"] == 0.0
    assert ss["ptc_hold"] == 1.0
    assert ss["ptc_pattern"] == ""
    assert "ptc_account" not in ss
    assert ss[ptc.ARCHIVE_SOURCE_FLAG] == "archive"
    assert ss[ptc.ARCHIVE_APPLIED_KEY] == pid

    again = ptc.consume_archive_prefill(st)
    assert again is None
    assert ptc.ARCHIVE_PREFILL_KEY not in st.session_state

    # Restaging the same id is ignored (stale); a new id applies.
    st.session_state[ptc.ARCHIVE_PREFILL_KEY] = {
        "id": pid, "prefill": dict(prefill),
    }
    assert ptc.consume_archive_prefill(st) is None
    assert ptc.ARCHIVE_PREFILL_KEY not in st.session_state

    blank = _St()
    assert ptc.consume_archive_prefill(blank) is None
    assert "ptc_symbol" not in blank.session_state


def test_archive_prefill_session_copies_quote_pair():
    st = _St()
    ts = "2026-08-27T09:45:00-04:00"
    prefill = ptc.archive_prefill_from_row(
        _archive_row(_bid=1.05, _ask=1.12),
        ticker="AAPL",
        underlying=177.55,
        scan_ts=ts,
    )
    ptc.stage_archive_prefill(st, prefill)
    ptc.consume_archive_prefill(st)
    ss = st.session_state
    assert ss["ptc_bid"] == pytest.approx(1.05)
    assert ss["ptc_ask"] == pytest.approx(1.12)
    assert ss["ptc_prefill_original"]["bid"] == pytest.approx(1.05)
    assert ss["ptc_prefill_original"]["ask"] == pytest.approx(1.12)
    assert ss["ptc_scan_meta"]["scan_ts"] == ts

    st.markdowns.clear()
    kwargs = ptc._marked_kwargs(st, "bid", "Bid")
    assert kwargs == {"label_visibility": "collapsed"}
    assert "from scanner (stale) · 2026-08-27 09:45" in st.markdowns[-1]
    st.markdowns.clear()
    ptc._marked_kwargs(st, "ask", "Ask")
    assert "from scanner (stale) · 2026-08-27 09:45" in st.markdowns[-1]
    st.markdowns.clear()
    ptc._marked_kwargs(st, "delta", "Delta")
    assert "from scanner (stale)" not in st.markdowns[-1]
    assert "from scanner</span>" in st.markdowns[-1]


def test_archive_quote_marker_and_scanner_tag():
    ts = "2026-08-27T09:45:00-04:00"
    assert ptc.archive_quote_marker(ts) == "from scanner (stale) · 2026-08-27 09:45"
    assert ptc.archive_quote_marker(None) == "from scanner (stale)"

    st = _St()
    st.session_state["ptc_prefill_original"] = {"bid": 1.05}
    st.session_state["ptc_bid"] = 1.05
    st.session_state[ptc.ARCHIVE_SOURCE_FLAG] = "scanner"
    st.session_state["ptc_scan_meta"] = {"scan_ts": ts}
    ptc._marked_kwargs(st, "bid", "Bid")
    assert "from scanner (stale)" not in st.markdowns[-1]
    assert "from scanner</span>" in st.markdowns[-1]


def test_archive_prefill_verdict_is_distance_skip_not_pass():
    st = _St()
    prefill = ptc.archive_prefill_from_row(
        _archive_row(), ticker="AAPL", underlying=177.55,
    )
    ptc.stage_archive_prefill(st, prefill)
    ptc.consume_archive_prefill(st)
    result = compute_pre_trade(PreTradeInputs(
        symbol=st.session_state["ptc_symbol"],
        direction=st.session_state["ptc_direction"],
        underlying=st.session_state["ptc_underlying"],
        target_distance=st.session_state["ptc_target"] or None,
        invalidation_distance=st.session_state["ptc_invalidation"] or None,
        hold_hours=st.session_state["ptc_hold"] or None,
        strike=st.session_state["ptc_strike"],
        dte=st.session_state["ptc_dte"],
        bid=st.session_state["ptc_bid"] or None,
        ask=st.session_state["ptc_ask"] or None,
        delta=st.session_state["ptc_delta"],
        theta=st.session_state["ptc_theta"],
        theta_units=st.session_state["ptc_theta_units"],
        open_interest=st.session_state["ptc_oi"],
        account_size=0,
    ))
    assert result["verdict"] == "SKIP"
    assert result["plan"] is None
    assert result["field_errors"].get("target_distance") == ptc.DISTANCE_ERROR
    assert result["field_errors"].get("invalidation_distance") == ptc.DISTANCE_ERROR


def test_archive_prefill_save_check_normal_schema(tmp_path, monkeypatch):
    monkeypatch.setattr(ptc, "CHECKS_PATH", str(tmp_path / "checks.json"))
    result = compute_pre_trade(PreTradeInputs(
        symbol="AAPL", direction="CALL", underlying=177.55,
        strike=180.0, dte=2, delta=0.41, theta=-0.052,
        theta_units="per day", open_interest=3100,
    ))
    row = save_check(result, contract_id="CALL|180.0000|2026-08-28")
    assert set(row) >= {
        "check_id", "ts_et", "inputs", "derived", "gates", "failed",
        "skip_reason", "verdict", "plan", "taken", "prefill_overrides",
        "pattern",
    }
    assert row["pattern"] is None
    assert row["verdict"] == "SKIP"
    assert row["plan"] is None
    assert row["contract_id"] == "CALL|180.0000|2026-08-28"
    assert row["scan_id"] is None
    loaded = ptc.load_checks()
    assert len(loaded) == 1
    assert loaded[0]["check_id"] == row["check_id"]


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
    assert blank["pattern"] is None


def test_0dte_prefill_hold_is_half_hour():
    prefill = contract_prefill_from_row(_row(dte=0), ticker="AAPL")
    assert prefill["hold_hours"] == 0.5
    arch = ptc.archive_prefill_from_row(
        _archive_row(_dte=0, DTE="0d"), ticker="AAPL", underlying=177.55,
    )
    assert arch is not None
    assert arch["hold_hours"] == 0.5
    st = _St()
    ptc.stage_archive_prefill(st, arch)
    ptc.consume_archive_prefill(st)
    assert st.session_state["ptc_hold"] == 0.5
    assert st.session_state["ptc_invalidation"] == 0.0


def test_pattern_candidates_do_not_fill_invalidation():
    or_data = {
        "5M": {"high": 320.10, "low": 318.40, "bias_dir": "bull"},
        "15M": {"high": 320.50, "low": 318.20, "bias_dir": "neutral"},
    }
    emas = {
        "daily": {"ema14": 317.98, "ema28": 316.10, "ema50": 314.00},
        "weekly": {"ema14": 310.00, "ema28": 305.00, "ema50": 300.00},
    }
    assert ptc.pattern_candidates("", or_data=or_data, vwap=319.01, emas=emas) == []
    assert ptc.pattern_candidates(ptc.PATTERN_OTHER, or_data=or_data) == []

    orb_long = ptc.pattern_candidates(
        ptc.PATTERN_ORB, direction="CALL", or_data=or_data,
    )
    assert [c["level"] for c in orb_long] == [318.40, 318.20]
    assert "5M OR low" in orb_long[0]["label"]
    assert "15M OR low" in orb_long[1]["label"]

    orb_short = ptc.pattern_candidates(
        ptc.PATTERN_ORB, direction="PUT", or_data=or_data,
    )
    assert [c["level"] for c in orb_short] == [320.10, 320.50]

    vwap_c = ptc.pattern_candidates(ptc.PATTERN_VWAP, vwap=319.01)
    assert len(vwap_c) == 1
    assert vwap_c[0]["label"] == "Session VWAP 319.01"

    ema_c = ptc.pattern_candidates(ptc.PATTERN_EMA, emas=emas)
    labels = [c["label"] for c in ema_c]
    assert "EMA 14 (daily close) 317.98" in labels
    assert "EMA 21" not in " ".join(labels)

    assert ptc.invalidation_from_level(319.01, 317.98) == pytest.approx(1.03)
    assert ptc.invalidation_from_level(0, 317.98) is None
    assert ptc.invalidation_from_level(None, 317.98) is None
    st = _St()
    ptc.apply_archive_prefill_to_session(st, {
        "prefill": ptc.archive_prefill_from_row(
            _archive_row(), ticker="AAPL", underlying=177.55,
        ),
    })
    assert st.session_state["ptc_invalidation"] == 0.0
    st.session_state["ptc_pattern"] = ptc.PATTERN_ORB
    assert st.session_state["ptc_invalidation"] == 0.0
    typed = 2.25
    st.session_state["ptc_invalidation"] = typed
    st.session_state["ptc_pattern"] = ptc.PATTERN_VWAP
    assert st.session_state["ptc_invalidation"] == typed
    dist = ptc.invalidation_from_level(319.01, 317.98)
    st.session_state["ptc_invalidation"] = dist
    assert st.session_state["ptc_invalidation"] == pytest.approx(1.03)


def test_save_check_writes_pattern_existing_null_ok(tmp_path, monkeypatch):
    monkeypatch.setattr(ptc, "CHECKS_PATH", str(tmp_path / "checks.json"))
    result = compute_pre_trade(PreTradeInputs(symbol="AAPL", direction="CALL"))
    row = save_check(result, pattern="ORB break")
    assert row["pattern"] == "ORB break"
    loaded = ptc.load_checks()
    assert loaded[0]["pattern"] == "ORB break"
    # Legacy row without the key:
    ptc._write_json(ptc.CHECKS_PATH, [{"check_id": "old", "verdict": "SKIP"}])
    again = ptc.load_checks()
    assert again[0].get("pattern") is None
    assert again[0]["check_id"] == "old"
