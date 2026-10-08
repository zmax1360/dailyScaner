"""Report page: the period engine (period_report.py) and ui/pages/report.py.

All data here is invented. The dates are fixed; the page tests pin the ending date."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import period_report as pr
import volume_history as vh
from tests.test_gex import _leg
from tests.test_scorecard import C340, HEADER, _trade

ET = ZoneInfo("America/New_York")
D1, D2, D3 = date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 8)
EXP = "2026-10-09"


def _bars():
    idx = pd.to_datetime([D1, D2, D3])
    return pd.DataFrame({"Open": [333.0, 337.0, 336.8], "High": [336.0, 338.6, 341.6],
                         "Low": [332.0, 332.8, 335.9], "Close": [335.5, 336.7, 340.4],
                         "Volume": [1, 1, 1]}, index=idx)


# ── 1. stock ────────────────────────────────────────────────────────────────

def test_one_day_gives_that_days_open_high_low_close():
    s = pr.stock_period(_bars(), D3, D3)
    assert (s["open"], s["high"], s["low"], s["close"]) == (336.8, 341.6, 335.9, 340.4)
    assert s["change"] == pytest.approx(3.6) and s["change_pct"] == pytest.approx(3.6 / 336.8)
    assert len(s["days"]) == 1 and s["first_day"] == s["last_day"] == D3


def test_a_period_opens_on_its_first_day_and_closes_on_its_last():
    s = pr.stock_period(_bars(), D1, D3)
    assert s["open"] == 333.0 and s["close"] == 340.4                 # first open, last close
    assert s["high"] == 341.6 and s["high_day"] == D3
    assert s["low"] == 332.0 and s["low_day"] == D1
    assert list(s["days"]["day"]) == [D1, D2, D3]
    assert list(s["days"]["change"]) == pytest.approx([2.5, -0.3, 3.6])


def test_no_bars_in_the_period_gives_nothing():
    assert pr.stock_period(_bars(), date(2026, 9, 1), date(2026, 9, 2)) == {}
    assert pr.stock_period(None, D1, D3) == {} and pr.stock_period(pd.DataFrame(), D1, D3) == {}
    assert pr.stock_period(_bars().drop(columns=["High"]), D1, D3) == {}


def test_trading_days_follow_the_bars_or_fall_back_to_weekdays():
    assert pr.trading_days(D1, D3, pr.stock_period(_bars(), D1, D3)) == [D1, D2, D3]
    assert pr.trading_days(date(2026, 10, 9), date(2026, 10, 12)) == [date(2026, 10, 9),
                                                                      date(2026, 10, 12)]


# ── 2. top picks ────────────────────────────────────────────────────────────

def _row(pool, side, strike, score, last, expiry=EXP):
    return {"pool": pool, "side": side, "strike": strike, "Value_Score": score, "last": last,
            "expiry": expiry}


def _archive(folder: Path, day: date, hhmmss: str, rows, spot=340.0) -> str:
    folder.mkdir(parents=True, exist_ok=True)
    name = f"AAPL_{day:%Y%m%d}_{hhmmss}"
    (folder / f"{name}.json").write_text(json.dumps({"spot": spot, "best_value": {"rows": rows}}))
    return name


@pytest.fixture
def archives(tmp_path):
    folder = tmp_path / "archive"
    _archive(folder, D3, "093300", [_row("1DTE+", "CALL", 337.5, 0.99, 2.00)])   # before 9:45
    _archive(folder, D3, "094800", [_row("1DTE+", "CALL", 340.0, 0.40, 0.90),
                                    _row("1DTE+", "PUT", 335.0, 0.70, 0.60),     # the top one
                                    _row("1DTE+", "CALL", 345.0, None, 0.20)])   # not ranked
    _archive(folder, D3, "100300", [_row("1DTE+", "CALL", 342.5, 0.95, 0.50),    # later: ignored
                                    _row("0DTE", "CALL", 340.0, 0.30, 0.45, "2026-10-08")])
    return str(folder)


def test_first_pick_is_the_top_ranked_contract_of_the_first_scan_after_945(archives):
    picks = pr.first_picks("AAPL", D3, archives)
    assert [p["pool"] for p in picks] == ["1DTE+", "0DTE"]
    swing, day = picks
    assert (swing["side"], swing["strike"], swing["score"], swing["open"]) == ("PUT", 335.0, 0.70, 0.60)
    assert swing["picked_at"] == datetime(2026, 10, 8, 9, 48, tzinfo=ET)
    assert day["picked_at"] == datetime(2026, 10, 8, 10, 3, tzinfo=ET)   # first scan ranking 0DTE
    assert pr.first_picks("AAPL", D1, archives) == []                    # no archives that day


def test_unreadable_archives_are_skipped(tmp_path):
    folder = tmp_path / "archive"
    folder.mkdir()
    (folder / "AAPL_20261008_095000.json").write_text("{broken")
    (folder / "AAPL_20261008_bad.json").write_text("{}")
    assert pr.first_picks("AAPL", D3, str(folder)) == []


@pytest.fixture
def chain(tmp_path):
    """The $335 put through D3: quoted 0.60 at the pick, up to 0.90, down to 0.40, 0.55 last."""
    db = str(tmp_path / "vh.db")
    quotes = [("093000", 0.70, 0.74), ("094800", 0.58, 0.62), ("101500", 0.88, 0.92),
              ("130000", 0.38, 0.42), ("160000", 0.53, 0.57)]
    for hhmmss, bid, ask in quotes:
        ts = datetime.strptime(f"20261008{hhmmss}", "%Y%m%d%H%M%S").replace(tzinfo=ET)
        puts = _leg([(335.0, (bid + ask) / 2, 1000, 5000, 0.30, bid, ask, EXP)])
        vh.record_scan(None, puts, ticker="AAPL", scan_id=f"AAPL_20261008_{hhmmss}", ts=ts,
                       db_path=db, force=True)
    return db


def test_pick_path_is_open_high_low_close_after_the_pick(archives, chain):
    pick = pr.first_picks("AAPL", D3, archives)[0]
    path = pr.pick_path("AAPL", pick, chain)
    assert path["open"] == 0.60
    assert path["high"] == pytest.approx(0.90) and path["low"] == pytest.approx(0.40)
    assert path["close"] == pytest.approx(0.55) and path["snapshots"] == 4   # 9:30 is before it
    assert path["change_pct"] == pytest.approx((0.55 - 0.60) / 0.60)


def test_pick_with_no_snapshots_keeps_its_open_and_leaves_the_rest_empty(archives, tmp_path):
    pick = pr.first_picks("AAPL", D3, archives)[0]
    path = pr.pick_path("AAPL", pick, str(tmp_path / "none.db"))
    assert path["open"] == 0.60 and path["high"] is None and path["close"] is None
    assert path["change_pct"] is None and path["snapshots"] == 0


def test_snapshot_price_prefers_the_quote_midpoint():
    assert pr._price(pd.Series({"bid": 0.50, "ask": 0.60, "last": 0.90})) == pytest.approx(0.55)
    assert pr._price(pd.Series({"bid": 0.0, "ask": 0.60, "last": 0.42})) == 0.42
    assert pr._price(pd.Series({"bid": None, "ask": None, "last": 0})) is None


def test_picks_report_covers_each_day(archives, chain):
    rows = pr.picks_report("AAPL", [D1, D3], archives, chain)
    assert [(r["day"], r["pool"]) for r in rows] == [(D3, "1DTE+"), (D3, "0DTE")]
    assert rows[0]["close"] == pytest.approx(0.55) and rows[1]["close"] is None


# ── 3. gamma ────────────────────────────────────────────────────────────────

@pytest.fixture
def gamma_data(tmp_path):
    """Two snapshots on D3: call-heavy in the morning, put-heavy in the afternoon."""
    folder, db = tmp_path / "archive", str(tmp_path / "vh.db")
    for hhmmss, call_oi, put_oi in (("100000", 9000, 1000), ("150000", 1000, 9000),
                                    ("153000", 1000, 9000)):
        sid = f"AAPL_20261008_{hhmmss}"
        if hhmmss != "153000":                               # the last one has no archive
            _archive(folder, D3, hhmmss, [], spot=340.0)
        ts = datetime.strptime(f"20261008{hhmmss}", "%Y%m%d%H%M%S").replace(tzinfo=ET)
        calls = _leg([(342.5, 0.9, 5000, call_oi, 0.25, 0.88, 0.92, EXP)])
        puts = _leg([(337.5, 0.6, 5000, put_oi, 0.25, 0.58, 0.62, EXP)])
        vh.record_scan(calls, puts, ticker="AAPL", scan_id=sid, ts=ts, db_path=db, force=True)
    return str(folder), db


def test_gamma_day_reports_the_high_and_the_low_with_their_times(gamma_data):
    folder, db = gamma_data
    g = pr.gamma_day("AAPL", D3, folder, db)
    assert g["expiry"] == EXP and g["readings"] == 2 and g["skipped"] == 1
    assert g["net_high"] > 0 > g["net_low"]
    assert g["net_high_at"] == datetime(2026, 10, 8, 10, 0, tzinfo=ET)
    assert g["net_low_at"] == datetime(2026, 10, 8, 15, 0, tzinfo=ET)
    assert g["net_last"] == g["net_low"]                     # the 15:00 reading is the last
    assert g["call_wall"] == 342.5 and g["put_wall"] == 337.5
    assert g["call_wall_gex"] > 0 > g["put_wall_gex"]


def test_gamma_day_is_empty_without_snapshots_or_archives(gamma_data, tmp_path):
    folder, db = gamma_data
    assert pr.gamma_day("AAPL", D1, folder, db) == {}                      # no snapshots that day
    assert pr.gamma_day("AAPL", D3, str(tmp_path / "nowhere"), db) == {}   # no archives: no spot
    assert pr.gamma_day("AAPL", D3, folder, str(tmp_path / "none.db")) == {}
    assert [g["day"] for g in pr.gamma_report("AAPL", [D1, D3], folder, db)] == [D3]


# ── 4. trades ───────────────────────────────────────────────────────────────

def _trades():
    import trade_history as th

    rows = [
        _trade("2026-10-07", "10:00:00", "BUY", C340, 1, -100.0),
        _trade("2026-10-07", "10:05:00", "SELL", C340, -1, 130.0),      # +30
        _trade("2026-10-08", "10:00:00", "BUY", C340, 2, -200.0),
        _trade("2026-10-08", "10:05:00", "SELL", C340, -2, 240.0),      # +40
        _trade("2026-10-08", "11:00:00", "BUY", C340, 1, -90.0),
        _trade("2026-10-08", "11:02:00", "SELL", C340, -1, 80.0),       # -10
        _trade("2026-10-08", "12:00:00", "BUY", C340, 1, -85.0),
        _trade("2026-10-08", "12:01:00", "SELL", C340, -1, 85.0),       # break-even
    ]
    return rows


@pytest.fixture
def trades(tmp_path):
    import trade_history as th

    f = tmp_path / "t.csv"
    f.write_text("\n".join([HEADER, *_trades()]) + "\n")
    return th.closed_trades(th.load_activities(str(f)))[0]


def test_trades_are_counted_by_entry_day_and_ticker(trades):
    assert len(pr.trades_in(trades, D3, D3)) == 3 and len(pr.trades_in(trades, D2, D3)) == 4
    assert len(pr.trades_in(trades, D3, D3, "TEST")) == 3
    assert pr.trades_in(trades, D3, D3, "AAPL").empty
    assert pr.trades_in(None, D3, D3).empty


def test_scorecard_for_a_period(trades):
    card = pr.scorecard_period(pr.trades_in(trades, D3, D3))
    assert (card["trades"], card["wins"], card["losses"], card["scratches"]) == (3, 1, 1, 1)
    assert card["gained"] == pytest.approx(40.0) and card["lost"] == pytest.approx(-10.0)
    assert card["net"] == pytest.approx(30.0)
    assert card["reward_to_risk"] == pytest.approx(20.0 / 10.0)        # +20 a contract vs -10
    assert pr.scorecard_period(pr.trades_in(trades, D1, D1)) == {}


def test_scorecard_without_a_loss_has_no_ratio(trades):
    card = pr.scorecard_period(pr.trades_in(trades, D2, D2))           # one winning trade
    assert card["trades"] == 1 and card["gained"] == pytest.approx(30.0) and card["lost"] == 0
    assert card["reward_to_risk"] is None


# ── page ────────────────────────────────────────────────────────────────────

def test_period_presets():
    from ui.pages import report

    days = [D1, D2, D3]
    sat = date(2026, 10, 10)
    assert report.resolve_period("1 day", sat, days) == (D3, D3)        # weekend: last session
    assert report.resolve_period("1 day", D2, days) == (D2, D2)
    assert report.resolve_period("5 days", sat, days) == (D1, D3)       # fewer than 5 available
    assert report.resolve_period("1 month", D3, days) == (D3 - timedelta(days=30), D3)
    assert report.resolve_period("Custom", D3, days, D1) == (D1, D3)
    assert report.resolve_period("Custom", D1, days, D3) == (D1, D1)    # never start after end
    assert report.resolve_period("1 day", sat, []) == (sat, sat)        # no bars at all


def test_money_format():
    from ui.pages import report

    assert report.usd(340.4) == "$340.40" and report.usd(-10) == "-$10.00"
    assert report.usd(3.6, signed=True) == "+$3.60" and report.usd(None) == "—"


_SCRIPT = """
from ui.pages import report
report.render({"ticker": "AAPL"})
"""


@pytest.fixture
def page(tmp_path, monkeypatch, archives, chain):
    """The page on invented data, pinned to end on D3 and never touching real files."""
    from streamlit.testing.v1 import AppTest

    from ui import broker_files
    from ui.pages import report

    monkeypatch.setattr(report, "_daily_bars", lambda ticker: _bars())
    monkeypatch.setattr(pr, "ARCHIVE_DIR", archives)
    monkeypatch.setattr(vh, "DB_PATH", chain)
    broker = tmp_path / "broker"
    broker.mkdir()
    rows = [r.replace("TEST  ", "AAPL  ").replace(",TEST,", ",AAPL,") for r in _trades()]
    (broker / "x.csv").write_text("\n".join([HEADER, *rows]) + "\n")
    monkeypatch.setattr(broker_files, "BROKER_DIR", str(broker))
    report._picks.clear()
    report._gamma.clear()

    def run(period="1 day"):
        at = AppTest.from_string(_SCRIPT, default_timeout=60)
        at.session_state["report_end"] = D3
        at.session_state["report_period"] = period
        at.run()
        assert not at.exception, at.exception
        return at

    return run


def _tables(at):
    from tests.test_scorecard import _tables as parse

    return parse(at)


def test_page_one_day(page):
    at = page()
    m = {x.label: x.value for x in at.metric}
    assert (m["Open"], m["High"], m["Low"], m["Close"]) == ("$336.80", "$341.60", "$335.90",
                                                           "$340.40")
    assert m["Change"] == "+$3.60 (+1.07%)"
    assert m["Trades"] == "3" and m["Winning trades"] == "1 (33%)"
    assert m["Gained"] == "$40.00" and m["Lost"] == "-$10.00" and m["Reward to risk"] == "2.00"
    titles = " ".join(x.value for x in at.markdown)
    for t in ("1 · The stock", "2 · The scanner's top pick", "3 · Gamma", "4 · Your trades"):
        assert t in titles
    picks = next(t for t in _tables(at) if "Picked at" in t[0])
    assert picks[0] == ["Day", "Pool", "Contract", "Picked at", "Open", "High", "Low", "Close",
                        "Change"]
    assert picks[1] == ["Thu Oct 08", "1DTE+", "PUT $335 10-09", "09:48", "$0.60", "$0.90",
                        "$0.40", "$0.55", "-8%"]
    assert any("Thu Oct 08, 2026" in c.value for c in at.caption)
    assert any(r"Net result +\$30.00" in c.value for c in at.caption)


def test_page_several_days_adds_a_row_per_day(page):
    at = page("5 days")
    m = {x.label: x.value for x in at.metric}
    assert m["Open"] == "$333.00" and m["Close"] == "$340.40" and m["Trades"] == "4"
    stock = next(t for t in _tables(at) if t[0][:2] == ["Day", "Open"])
    assert [r[0] for r in stock[1:]] == ["Tue Oct 06", "Wed Oct 07", "Thu Oct 08"]
    assert stock[3] == ["Thu Oct 08", "$336.80", "$341.60", "$335.90", "$340.40", "+$3.60"]


def test_page_says_so_when_a_section_has_no_data(page, monkeypatch, tmp_path):
    from ui import broker_files

    monkeypatch.setattr(pr, "ARCHIVE_DIR", str(tmp_path / "no-archives"))
    monkeypatch.setattr(broker_files, "BROKER_DIR", str(tmp_path / "no-broker"))
    at = page()
    notes = " ".join(i.value for i in at.info)
    assert "No ranked pick was stored" in notes and "No chain snapshots" in notes
    assert "No trade file yet" in notes
    assert {x.label for x in at.metric} == {"Open", "High", "Low", "Close", "Change"}


def test_report_is_a_menu_page_and_app_stays_small():
    from ui import shell

    assert {e.id: e.title for e in shell.entries()}["report"] == "Report"
