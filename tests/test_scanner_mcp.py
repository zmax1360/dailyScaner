"""scanner_mcp: read-only tools over saved scans and the broker export."""

from __future__ import annotations

import json
import sys
from datetime import datetime

import pytest

import game_plan
import gex
import scanner_mcp as sm

ET = sm.ET
NOW = datetime(2026, 10, 22, 14, 30, tzinfo=ET)


def _plan(as_of: datetime) -> dict:
    plan = game_plan.Plan(
        ticker="AAPL", spot=346.5, as_of=as_of, stale=False,
        side=game_plan.STAND_ASIDE, side_reason="trend and scanner disagree",
        regime=game_plan.POSITIVE, regime_note="", net_gex=1.2e8, gamma_expiry="2026-10-23",
        levels=(game_plan.Level("Resistance", 350.0, "resistance"),),
        candidates=(), expect=(), changes=(),
        trend_side=game_plan.CALLS, flow_side=game_plan.PUTS, flow_note="puts 4 of 6",
        hold=game_plan.HOLD_CONFLICT, hold_note="Conflict: no plan until they agree.")
    return game_plan.plan_to_json(plan)


def _gamma(as_of: datetime) -> dict:
    by = {340.0: -7.8e6, 342.5: 1.0e6, 345.0: 3.1e7, 347.5: 2.7e7, 350.0: 4.2e7}
    return gex.summary_to_json({
        "expiry": "2026-10-23", "spot": 346.5, "net": 1.18e8,
        "call_wall": 350.0, "call_wall_gex": 4.2e7, "put_wall": 340.0, "put_wall_gex": -7.8e6,
        "support": 345.0, "support_gex": 3.1e7, "resistance": 350.0, "resistance_gex": 4.2e7,
        "top": [(350.0, 4.2e7), (345.0, 3.1e7)], "first_negative_below_spot": 340.0,
        "by_strike": by, "as_of": as_of})


def _payload(ts: datetime, spot: float, put_magnet: float, nan_oi: bool = False) -> dict:
    return {
        "timestamp": ts.isoformat(), "spot": spot, "direction": "BULLISH",
        "volume": {"pc_ratio": 0.98, "total_call_vol": 1000, "total_put_vol": 980,
                   "top_calls": [{"strike": 350.0, "expiry": "2026-10-23", "dte": 1,
                                  "lastPrice": 0.8, "volume": 91860.0,
                                  "openInterest": float("nan") if nan_oi else 22637.0}],
                   "top_puts": [{"strike": 345.0, "expiry": "2026-10-23", "dte": 1,
                                 "lastPrice": 1.51, "volume": 53319.0, "openInterest": 3325.0}]},
        "signal_magnets": {"call": {"strike": 350.0}, "put": {"strike": put_magnet}},
        "gamma": _gamma(ts), "game_plan": _plan(ts),
    }


@pytest.fixture
def archive(tmp_path):
    d = tmp_path / "archive"
    d.mkdir()
    (d / "AAPL_20261022_142200.json").write_text(
        json.dumps(_payload(datetime(2026, 10, 22, 14, 22, tzinfo=ET), 346.73, 340.0)))
    (d / "AAPL_20261022_142805.json").write_text(
        json.dumps(_payload(datetime(2026, 10, 22, 14, 28, 5, tzinfo=ET), 346.57, 345.0,
                            nan_oi=True)).replace("NaN", "NaN"))
    (d / "dailyScanerv9.py").write_text("# not an archive")
    (d / "AAPLX_20261022_142805.json").write_text("{}")
    return str(d)


HEADER = ("effective_date,effective_time,settlement_date,account_id,account_type,activity_type,"
          "activity_sub_type,description,direction,symbol,underlying symbol,name,currency,"
          "quantity,unit_price,commission,net_cash_amount\n")


def _row(day, t, side, sym, qty, cash):
    return (f"{day},{t},,X,Non-registered margin,Trade,{side},desc,LONG,{sym},AAPL,Apple,USD,"
            f"{qty},{abs(cash) / abs(qty)},0,{cash}\n")


@pytest.fixture
def broker(tmp_path):
    d = tmp_path / "broker"
    d.mkdir()
    put = "AAPL  261023P00340000"
    rows = [
        # Oct 20: one clean trade, +20
        _row("2026-10-20", "10:53:16", "BUY", "AAPL  261021P00337500", 1, -83),
        _row("2026-10-20", "10:56:11", "SELL", "AAPL  261021P00337500", -1, 103),
        # Oct 22: -6, +26 (done at 10:17), then an entry after the limit, and an add to a loser
        _row("2026-10-22", "10:08:44", "BUY", put, 1, -75),
        _row("2026-10-22", "10:10:16", "SELL", put, -1, 69),
        _row("2026-10-22", "10:11:18", "BUY", put, 1, -69),
        _row("2026-10-22", "10:17:07", "SELL", put, -1, 95),
        _row("2026-10-22", "14:41:36", "BUY", "AAPL  261023P00342500", 1, -94),
        _row("2026-10-22", "14:47:05", "SELL", "AAPL  261023P00342500", -1, 87),
        _row("2026-10-22", "14:48:40", "BUY", "AAPL  261023P00342500", 1, -86),
        _row("2026-10-22", "14:49:00", "BUY", "AAPL  261023P00342500", 1, -80),
        _row("2026-10-22", "14:50:34", "SELL", "AAPL  261023P00342500", -2, 163),
    ]
    (d / "activities-export-2026-10-22.csv").write_text(HEADER + "".join(rows))
    return str(d)


def test_import_has_no_mcp_or_network_side_effects():
    assert "mcp" not in sys.modules or "mcp.server.fastmcp" not in sys.modules
    assert sm.list_tickers is not None


def test_list_tickers_ignores_other_files(archive):
    out = sm.list_tickers(archive_dir=archive)
    assert [t["ticker"] for t in out["tickers"]] == ["AAPL", "AAPLX"]
    assert out["tickers"][0]["newest_archive"] == "20261022_142805"


def test_scan_status_age_and_stale(archive):
    out = sm.scan_status("aapl", now=NOW, archive_dir=archive)
    assert out["available"] and out["file"] == "AAPL_20261022_142805.json"
    assert out["age_minutes"] == pytest.approx(1.9, abs=0.1)
    assert out["stale"] is False
    late = sm.scan_status("AAPL", now=datetime(2026, 10, 22, 15, 30, tzinfo=ET),
                          archive_dir=archive)
    assert late["stale"] is True


def test_missing_ticker_is_reported_not_defaulted(archive):
    out = sm.game_plan_report("NVDA", archive_dir=archive)
    assert out["available"] is False and "NVDA" in out["reason"]


def test_bad_ticker_rejected(archive):
    with pytest.raises(ValueError):
        sm.scan_status("../etc", archive_dir=archive)


def test_game_plan_is_the_saved_one(archive):
    out = sm.game_plan_report("AAPL", now=NOW, archive_dir=archive)
    assert out["side"] == game_plan.STAND_ASIDE
    assert out["side_label"] == "Stand aside"
    assert "Conflict" in out["text"] and "<b>" not in out["text"]


def test_gamma_near_spot_and_regime(archive):
    out = sm.gamma_report("AAPL", strikes_each_side=2, archive_dir=archive)
    assert out["regime"] == "positive"
    assert [r["strike"] for r in out["near_spot"]] == [342.5, 345.0, 347.5, 350.0]
    assert out["call_wall"] == 350.0 and out["put_wall"] == 340.0
    assert "GAMMA" in out["text"]


def test_magnets_changes_and_missing_oi_is_none(archive):
    out = sm.magnets_report("AAPL", archive_dir=archive)
    assert out["top_calls"][0]["open_interest"] is None       # NaN -> None, not 0 or 1
    assert out["top_calls"][0]["vol_oi"] is None
    assert out["top_puts"][0]["vol_oi"] == pytest.approx(16.04, abs=0.01)
    assert any("put magnet $340 -> $345" in c for c in out["changes_vs_previous_scan"])


def test_daily_guard_counts_rules(broker):
    out = sm.daily_guard("2026-10-22", broker_dir=broker, now=NOW)
    assert out["entries"] == 5
    assert out["realized_pnl"] == pytest.approx(10.0)
    assert out["status"] == "DONE"                             # 4 entries reached
    assert out["rules_followed"] is False
    text = " ".join(out["rules_broken"])
    assert "3 entries after the +$20/-$20 limit" in text   # all three, though P&L fell back under
    assert out["limit_hit_at"] == "10:17"
    assert "add(s) to a losing position" in text
    assert "5 entries (limit 4)" in text
    assert out["streak_days"] == 0


def test_daily_guard_clean_day_and_streak(broker):
    out = sm.daily_guard("2026-10-20", broker_dir=broker, now=NOW)
    assert out["rules_followed"] is True and out["realized_pnl"] == pytest.approx(20.0)
    assert out["status"] == "DONE" and out["streak_days"] == 1


def test_daily_guard_without_export(tmp_path):
    out = sm.daily_guard(broker_dir=str(tmp_path))
    assert out["available"] is False


def test_summary_has_every_section(archive, broker):
    out = sm.summary_report("AAPL", now=NOW, archive_dir=archive, broker_dir=broker)
    assert set(out) == {"status", "game_plan", "gamma", "magnets", "daily_guard"}


def test_build_server_registers_tools():
    pytest.importorskip("mcp.server.fastmcp")
    server = sm.build_server()
    assert server.name == "dailyscaner"
