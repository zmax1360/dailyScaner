"""Game Plan and gamma in the automatic Telegram message (plan_report.py, telegram_bot,
scheduler)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import game_plan as gp
import plan_report
import volume_history as vh
from tests.test_gex import LATER, MONDAY, NEXT_FRI, TODAY, _leg

ET = ZoneInfo("America/New_York")
ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 3, 11, 0, tzinfo=ET)           # the Saturday after the snapshot
GOLDEN = json.loads((ROOT / "tests" / "golden" / "AAPL_20260728_095049.json").read_text())


def _row(side, pool, score, strike, expiry, dte, last, delta):
    return {"side": side, "pool": pool, "Value_Score": score, "strike": strike,
            "expiry": expiry, "dte": dte, "last": last, "delta": delta, "volume": 5000,
            "openInterest": 4000, "Status": ""}


@pytest.fixture(autouse=True)
def no_real_archive(tmp_path, monkeypatch):
    """Scanner-side history is read from an empty folder unless a test fills one."""
    d = tmp_path / "archive"
    d.mkdir()
    monkeypatch.setattr(plan_report, "ARCHIVE_DIR", str(d))
    return d


@pytest.fixture
def payload():
    """A scan archive with a bullish 15-minute stack, spot 333.5 and stored picks."""
    p = copy.deepcopy(GOLDEN)
    p["spot"] = 333.5
    p["session"] = {"day_high": 334.6, "day_low": 331.9, "open": 332.5, "prev_close": 332.0}
    p["timeframes"]["15M"].update(ema9=333.4, ema21=333.0, ema50=332.6)
    p["timestamp"] = "2026-10-03T10:58:00-04:00"
    p["best_value"] = {"rows": [
        _row("CALL", "1DTE+", 0.31, 335.0, NEXT_FRI, 6, 1.90, 0.41),
        _row("PUT", "1DTE+", 0.55, 330.0, NEXT_FRI, 6, 1.10, -0.30),
        _row("CALL", "0DTE", 0.22, 335.0, MONDAY, 0, 0.40, 0.33),
    ]}
    return p


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    db = str(tmp_path / "vh.db")
    monkeypatch.setattr(vh, "DB_PATH", db)
    calls = _leg([(335.0, 0.01, 5000, 9000, 0.30, 0.01, 0.02, TODAY),
                  (335.0, 1.2, 5000, 9000, 0.30, 1.15, 1.25, MONDAY),
                  (332.5, 2.4, 4000, 3000, 0.30, 2.35, 2.45, MONDAY),
                  (340.0, 1.9, 2000, 4000, 0.28, 1.85, 1.95, NEXT_FRI),
                  (335.0, 4.0, 1500, 1000, 0.27, 3.95, 4.05, LATER)])
    puts = _leg([(327.5, 0.4, 3000, 6000, 0.33, 0.38, 0.42, MONDAY),
                 (325.0, 1.1, 1500, 5000, 0.31, 1.05, 1.15, NEXT_FRI)])
    vh.record_scan(calls, puts, ticker="AAPL", scan_id="AAPL_20261002_160500",
                   ts=datetime(2026, 10, 2, 16, 5, tzinfo=ET), db_path=db)
    return db


# ── plan_report ─────────────────────────────────────────────────────────────

def test_picks_come_from_the_ranking_the_scan_saved(payload):
    picks = plan_report.picks_from_archive(payload)
    assert len(picks) == 3 and set(picks["pool"]) == {"1DTE+", "0DTE"}
    assert plan_report.picks_from_archive({}).empty
    assert plan_report.picks_from_archive({"best_value": None}).empty


def test_plan_from_archive_uses_the_archive_only(payload, snapshot):
    gamma = plan_report.gamma_summary_for("AAPL", 333.5, today=NOW.date())
    plan = plan_report.plan_from_archive(payload, "AAPL", gamma=gamma, now=NOW,
                                         settings={"stop_buffer_pct": 0.1,
                                                   "min_reward_to_risk": 1.5})
    assert plan.side == gp.CALLS and plan.regime == gp.POSITIVE and not plan.stale
    assert plan.candidate_for("1DTE+").pick["strike"] == 335.0        # the call, not the put
    assert plan.candidate_for("0DTE").pick["dte"] == 0
    assert "VWAP" in plan.missing                                     # not stored in archives
    assert plan.trade is not None and plan.trade.better_entry.name == "15-minute EMA 21"
    assert plan.price_note == "Price is the one recorded by the scan."


def test_saved_settings_are_read_and_validated(tmp_path):
    f = tmp_path / "ui_settings.json"
    defaults = {"stop_buffer_pct": gp.STOP_BUFFER_PCT, "min_reward_to_risk": gp.MIN_REWARD_TO_RISK}
    assert plan_report.saved_plan_settings(str(f)) == defaults                  # no file
    f.write_text("{not json")
    assert plan_report.saved_plan_settings(str(f)) == defaults
    f.write_text(json.dumps({"stop_buffer_pct": 0.25, "min_reward_to_risk": 2.0, "top_n": 9}))
    assert plan_report.saved_plan_settings(str(f)) == {"stop_buffer_pct": 0.25,
                                                       "min_reward_to_risk": 2.0}
    f.write_text(json.dumps({"stop_buffer_pct": 9, "min_reward_to_risk": "2"}))
    assert plan_report.saved_plan_settings(str(f)) == defaults                  # out of range
    f.write_text("[1, 2]")
    assert plan_report.saved_plan_settings(str(f)) == defaults


def test_report_lines(payload, snapshot):
    plan, gamma = plan_report.report_lines(payload, "AAPL", now=NOW)
    assert plan[0] == "🎯 <b>GAME PLAN · AAPL</b>" and plan[1] == "Side: <b>Calls only</b>"
    assert any(line.startswith("1DTE+: CALL $335") for line in plan)
    assert any(line.startswith("Calls plan: enter at or below") for line in plan)
    assert gamma[0] == "🧱 <b>GAMMA · Oct 5</b>"
    only_gamma = plan_report.report_lines(payload, "AAPL", plan=False, now=NOW)
    assert only_gamma[0] == [] and only_gamma[1] == gamma
    assert plan_report.report_lines(payload, "AAPL", plan=False, gamma=False) == ([], [])


def test_plan_report_needs_no_app_code():
    """The scheduler and the bot run without Streamlit; this module must too."""
    code = ("import sys, plan_report; "
            "bad = [m for m in sys.modules if m == 'streamlit' or m.startswith('ui.')]; "
            "sys.exit(1 if bad else 0)")
    assert subprocess.run([sys.executable, "-c", code], cwd=ROOT).returncode == 0


# ── the bot's report and the scheduler ──────────────────────────────────────

OFF = {k: False for k in ("session", "mtf", "magnets", "volume_expiry", "orb", "deltas",
                          "best_value", "catalyst", "market_news", "game_plan", "gamma")}


def _report(payload, **on):
    import telegram_bot

    return telegram_bot._fmt_report(payload, None, "AAPL", 5, {**OFF, **on}, [])


def test_automatic_message_carries_the_plan_first_and_gamma(payload, snapshot, monkeypatch):
    monkeypatch.setattr(plan_report, "datetime", type("D", (datetime,), {
        "now": classmethod(lambda cls, tz=None: NOW)}))
    msg = _report(payload, game_plan=True, gamma=True, magnets=True)
    assert "🎯 <b>GAME PLAN · AAPL</b>" in msg and "🧱 <b>GAMMA · Oct 5</b>" in msg
    assert msg.index("GAME PLAN") < msg.index("GAMMA ·")
    assert msg.index("Options Scanner") < msg.index("GAME PLAN")      # right under the header
    assert len(msg) < 4096


def test_plan_is_short_inside_a_full_report_and_complete_alone(payload, snapshot, monkeypatch):
    monkeypatch.setattr(plan_report, "datetime", type("D", (datetime,), {
        "now": classmethod(lambda cls, tz=None: NOW)}))
    full_report = _report(payload, game_plan=True, gamma=True, magnets=True)
    assert "Calls plan: enter at or below" in full_report
    assert "Levels:" not in full_report and "lose their stack" not in full_report
    alone = _report(payload, game_plan=True, deltas=True)
    assert "Levels:" in alone and "lose their stack" in alone
    plan = plan_report.plan_from_archive(payload, "AAPL", now=NOW)
    short, long = gp.plan_lines(plan, compact=True), gp.plan_lines(plan)
    assert long[:len(short)] == short and len(long) > len(short)
    assert len("\n".join(short)) < 600


def test_sections_are_absent_unless_asked_for(payload, snapshot):
    msg = _report(payload, magnets=True)
    assert "GAME PLAN" not in msg and "GAMMA" not in msg


def test_a_failure_never_blocks_the_rest_of_the_message(payload, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("snapshot unreadable")

    monkeypatch.setattr(plan_report, "report_lines", boom)
    msg = _report(payload, game_plan=True, gamma=True, magnets=True)
    assert "Game plan and gamma unavailable (RuntimeError)" in msg
    assert "Options Scanner" in msg and "snapshot unreadable" not in msg


def test_gamma_section_says_so_when_there_is_no_snapshot(payload, tmp_path, monkeypatch):
    monkeypatch.setattr(vh, "DB_PATH", str(tmp_path / "empty.db"))
    msg = _report(payload, gamma=True)
    assert "no chain snapshot with usable open interest yet" in msg


def test_scheduler_asks_for_both_sections_and_the_bot_offers_them():
    import telegram_bot

    src = (ROOT / "scheduler.py").read_text()
    block = src[src.index("def _notify_success"):src.index("expiry_drill=[]")]
    assert '"game_plan":     True' in block and '"gamma":         True' in block
    keys = [k for k, _ in telegram_bot._SECTIONS]
    assert keys[:2] == ["game_plan", "gamma"]


# ── scanner side from the day's archives ────────────────────────────────────
def _scan(folder, hhmmss, side, ticker="AAPL", day="20261003"):
    stamp = f"{day[:4]}-{day[4:6]}-{day[6:]}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:]}-04:00"
    rows = [] if side is None else [
        _row(side, "1DTE+", 0.9, 335.0, NEXT_FRI, 6, 1.5, 0.4),
        _row("PUT" if side == "CALL" else "CALL", "1DTE+", 0.2, 330.0, NEXT_FRI, 6, 1.0, 0.3),
        _row("PUT" if side == "CALL" else "CALL", "0DTE", 1.0, 335.0, MONDAY, 0, 0.4, 0.3),
    ]
    (folder / f"{ticker}_{day}_{hhmmss}.json").write_text(
        json.dumps({"timestamp": stamp, "best_value": {"rows": rows}}))


def test_top_pick_side_is_the_best_ranked_row_of_the_pool(payload):
    assert plan_report.top_pick_side(payload) == "PUT"            # 0.55 beats 0.31
    assert plan_report.top_pick_side(payload, "0DTE") == "CALL"
    assert plan_report.top_pick_side({}) is None
    payload["best_value"]["rows"][1]["Value_Score"] = float("nan")
    assert plan_report.top_pick_side(payload) == "CALL"


def test_scanner_flow_reads_the_five_scans_before_this_one(payload, no_real_archive):
    for t in ("101000", "102000", "103000", "104000", "105000"):
        _scan(no_real_archive, t, "CALL")
    _scan(no_real_archive, "100000", "PUT")                       # seventh back: ignored
    _scan(no_real_archive, "110500", "PUT")                       # after this scan: ignored
    _scan(no_real_archive, "105500", "PUT", ticker="NVDA")        # another ticker
    _scan(no_real_archive, "105700", "PUT", day="20261002")       # another day
    flow = plan_report.scanner_flow(payload, "aapl")
    assert (flow["calls"], flow["puts"], flow["scans"]) == (5, 1, 6)
    assert flow["side"] == gp.CALLS


def test_scanner_flow_skips_a_broken_file_and_this_scans_own_archive(payload, no_real_archive):
    for t in ("102000", "103000", "104000", "105000"):
        _scan(no_real_archive, t, "PUT")
    (no_real_archive / "AAPL_20261003_105500.json").write_text("{not json")
    (no_real_archive / "AAPL_20261003_105759.json").write_text(json.dumps(payload))
    flow = plan_report.scanner_flow(payload, "AAPL")
    assert (flow["puts"], flow["scans"]) == (5, 5) and flow["side"] == gp.UNKNOWN


def test_plan_from_archive_holds_on_conflict_and_outside_the_window(payload, snapshot,
                                                                   no_real_archive):
    for t in ("101000", "102000", "103000", "104000", "105000"):
        _scan(no_real_archive, t, "PUT")
    plan = plan_report.plan_from_archive(payload, "AAPL", now=NOW)   # bullish EMAs, put flow
    assert plan.hold == gp.HOLD_CONFLICT and plan.trade is None
    late = plan_report.plan_from_archive(payload, "AAPL", now=NOW.replace(hour=15, minute=10))
    assert late.hold == gp.HOLD_WINDOW
    lines, _ = plan_report.report_lines(payload, "AAPL", now=NOW)
    assert any(x.startswith("⛔ Conflict") for x in lines)
