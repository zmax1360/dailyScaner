"""Scorecard: broker export parsing (trade_history.py) and the Scorecard page.

Every row here is invented. No real export, account number or trade is used.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pandas as pd
import pytest

import trade_history as th

ROOT = Path(__file__).resolve().parents[1]
HEADER = ("effective_date,effective_time,settlement_date,account_id,account_type,activity_type,"
          "activity_sub_type,description,direction,symbol,underlying symbol,name,currency,"
          "quantity,unit_price,commission,net_cash_amount")
ACCOUNT = "TESTACCT000"


def _trade(day, time, sub, symbol, qty, cash, currency="USD", fx=None):
    desc = f"{symbol}: {'Bought' if sub == 'BUY' else 'Sold'} {abs(qty)} contract"
    if fx:
        desc += f", FX Rate: {fx}"
    return (f'{day},{time},,{ACCOUNT},Margin,Trade,{sub},"{desc}",LONG,{symbol},TEST,Test Co,'
            f"{currency},{qty},{abs(cash) / abs(qty)},0,{cash}")


def _expiry(day, symbol, n):
    return (f'{day},,,{ACCOUNT},Margin,OptionExpiry,-,"{symbol}: Expired {n} contract",,'
            f"{symbol},TEST,Test Co,USD,,,,")


C340 = "TEST  261009C00340000"       # call, strike 340, expires 2026-10-09
P330 = "TEST  261007P00330000"       # put, strike 330, expires 2026-10-07

ROWS = [
    f"2026-10-06,09:00:00,,{ACCOUNT},Margin,MoneyMovement,E_TRFIN,Deposit,,,,,CAD,300,,,300",
    _trade("2026-10-07", "10:06:00", "BUY", C340, 2, -190.0),      # 2 @ $0.95
    _trade("2026-10-07", "10:09:00", "SELL", C340, -2, 200.0),     # +10, held 3 min, 2 DTE
    _trade("2026-10-07", "09:40:00", "BUY", P330, 3, -90.0),       # 3 @ $0.30, 0DTE
    _trade("2026-10-07", "10:40:00", "SELL", P330, -1, 20.0),      # -10 on one, held 60 min
    _expiry("2026-10-07", P330, 2),                                # -60 on the other two
    _trade("2026-10-07", "14:00:00", "BUY", C340, 1, -141.0, "CAD", 1.41),    # $100 in USD
    _trade("2026-10-07", "14:10:00", "SELL", C340, -1, 169.2, "CAD", 1.41),   # $120 -> +20
]


def _write(path: Path, rows=ROWS) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join([HEADER, *rows]) + "\n")
    return str(path)


@pytest.fixture
def export(tmp_path):
    return _write(tmp_path / "broker" / "activities-export-2026-10-07.csv")


def _trades(export):
    return th.closed_trades(th.load_activities(export))


# ── parsing ─────────────────────────────────────────────────────────────────

def test_occ_symbols_are_parsed():
    assert th.parse_occ(C340) == {"underlying": "TEST", "expiry": pd.Timestamp("2026-10-09"),
                                  "cp": "C", "strike": 340.0}
    assert th.parse_occ("AAPL  260831P00317500")["strike"] == 317.5
    assert th.parse_occ("AAPL") is None and th.parse_occ(None) is None


def test_the_account_number_is_never_loaded(export):
    a = th.load_activities(export)
    assert "account_id" not in a.columns and "account_type" not in a.columns
    trades, _ = th.closed_trades(a)
    assert ACCOUNT not in trades.to_csv()


def test_a_file_that_is_not_an_export_is_rejected(tmp_path):
    bad = tmp_path / "x.csv"
    bad.write_text("a,b\n1,2\n")
    with pytest.raises(ValueError, match="not an activity export"):
        th.load_activities(str(bad))


# ── matching buys to sells ──────────────────────────────────────────────────

def test_round_trips_are_matched_first_in_first_out(export):
    t, notes = _trades(export)
    assert notes == {"open_contracts": 0, "unmatched_sells": 0, "skipped_rows": 0}
    assert len(t) == 4 and int(t["qty"].sum()) == 6
    first_call = t[(t.cp == "C") & (t.qty == 2)].iloc[0]
    assert first_call["pnl"] == pytest.approx(10.0) and first_call["hold_min"] == pytest.approx(3.0)
    assert first_call["premium"] == pytest.approx(0.95) and first_call["pool"] == "1DTE+"
    assert first_call["dte"] == 2


def test_expiry_closes_the_remaining_contracts_at_zero(export):
    t, _ = _trades(export)
    puts = t[t.cp == "P"].sort_values("exit")
    sold, expired = puts.iloc[0], puts.iloc[1]
    assert (sold["qty"], sold["pnl"], bool(sold["expired"])) == (1, pytest.approx(-10.0), False)
    assert (expired["qty"], expired["pnl"], bool(expired["expired"])) == (2, pytest.approx(-60.0), True)
    assert set(puts["pool"]) == {"0DTE"}


def test_cad_rows_are_converted_with_the_rate_on_the_row(export):
    t, _ = _trades(export)
    fx_trade = t[t.entry == pd.Timestamp("2026-10-07 14:00:00")].iloc[0]
    assert fx_trade["cost"] == pytest.approx(100.0) and fx_trade["pnl"] == pytest.approx(20.0)


def test_open_lots_unmatched_sells_and_unvalued_rows_are_reported(tmp_path):
    rows = [
        _trade("2026-10-07", "10:00:00", "BUY", C340, 3, -300.0),
        _trade("2026-10-07", "10:05:00", "SELL", C340, -1, 110.0),          # 2 stay open
        _trade("2026-10-07", "11:00:00", "SELL", P330, -2, 50.0),           # no buy in the file
        _trade("2026-10-07", "12:00:00", "BUY", P330, 1, -141.0, "CAD"),    # CAD, no FX rate
    ]
    t, notes = th.closed_trades(th.load_activities(_write(tmp_path / "x.csv", rows)))
    assert len(t) == 1 and t.iloc[0]["pnl"] == pytest.approx(10.0)
    assert notes == {"open_contracts": 2, "unmatched_sells": 2, "skipped_rows": 1}


def test_no_trades_gives_empty_results():
    t, notes = th.closed_trades(pd.DataFrame())
    assert t.empty and th.summary(t) == {} and th.breakdown(t, "pool").empty
    assert th.daily(t).empty and th.worst(t).empty


# ── results ─────────────────────────────────────────────────────────────────

def test_summary_numbers(export):
    s = th.summary(_trades(export)[0])
    assert s["trades"] == 4 and s["contracts"] == 6 and s["days"] == 1
    assert s["pnl"] == pytest.approx(-40.0)                  # +10 -10 -60 +20
    assert s["win_rate"] == pytest.approx(0.5)
    assert s["avg_win"] == pytest.approx(15.0) and s["avg_loss"] == pytest.approx(-35.0)
    assert s["profit_factor"] == pytest.approx(30.0 / 70.0)
    assert (s["green_days"], s["red_days"]) == (0, 1)


def test_breakdowns(export):
    t = _trades(export)[0]
    pool = th.breakdown(t, "pool").set_index("bucket")
    assert pool.loc["0DTE", "pnl"] == pytest.approx(-70.0) and pool.loc["0DTE", "trades"] == 2
    assert pool.loc["1DTE+", "pnl"] == pytest.approx(30.0) and pool.loc["1DTE+", "win_rate"] == 1.0
    side = th.breakdown(t, "side").set_index("bucket")
    assert side.loc["Calls", "pnl"] == pytest.approx(30.0)
    hold = th.breakdown(t, "hold").set_index("bucket")
    assert hold.loc["Under 5 min", "trades"] == 1 and hold.loc["5 to 15 min", "trades"] == 1
    assert hold.loc["1 to 4 hours", "trades"] == 1           # exactly 60 minutes
    prem = th.breakdown(t, "premium").set_index("bucket")
    assert prem.loc["$0.25 to $0.60", "pnl"] == pytest.approx(-70.0)     # the $0.30 puts
    assert prem.loc["$0.60 to $1.20", "trades"] == 2
    time = th.breakdown(t, "time").set_index("bucket")
    assert time.loc["Before 10:00", "trades"] == 2 and time.loc["10:00 to 11:00", "trades"] == 1
    with pytest.raises(ValueError):
        th.breakdown(t, "nope")


def test_payoff_shows_what_is_taken_and_what_it_requires(export):
    p = th.payoff(_trades(export)[0])            # wins +10, +20; losses -10, -60
    assert p["trades"] == 4 and p["win_rate"] == pytest.approx(0.5)
    assert p["avg_win"] == pytest.approx(15.0) and p["avg_loss"] == pytest.approx(-35.0)
    assert p["payoff_ratio"] == pytest.approx(15.0 / 35.0)
    assert p["breakeven_win_rate"] == pytest.approx(35.0 / 50.0)      # needs 70%, has 50%
    assert p["win_needed"] == pytest.approx(35.0)                     # at a 50% win rate
    assert p["per_trade"] == pytest.approx(-10.0)
    # median change in the option's price: wins +10/190 and +20/100, losses -10/30 and -60/60
    assert p["median_win_pct"] == pytest.approx((10 / 190 + 0.20) / 2)
    assert p["median_loss_pct"] == pytest.approx((-1 / 3 - 1.0) / 2)
    # per contract: wins +5 and +20, losses -10 and -30
    assert p["reward_to_risk"] == pytest.approx(12.5 / 20.0)
    assert p["reward_to_risk_needed"] == pytest.approx(1.0)           # at a 50% win rate


def test_one_r_is_the_average_loss_per_contract_for_each_expiry_type(export):
    t = _trades(export)[0]
    units = th.risk_units(t)
    assert units["0DTE"] == pytest.approx(20.0)                       # -10 and -30 per contract
    assert units["1DTE+"] == pytest.approx(20.0)                      # no 1DTE+ loss: all trades
    assert th.risk_units(pd.DataFrame()) == {}
    assert th.risk_units(t[t.pnl > 0]) == {}                          # no losses at all


def test_recent_trades_show_prices_per_share_and_the_result_in_r(export):
    t = _trades(export)[0]
    r = th.recent(t, 10)
    # newest exit first: the puts that expired at the close, then the 14:10 sale
    assert list(r["contract"])[:2] == ["TEST 330P Oct 7", "TEST 340C Oct 9"]
    last = r.iloc[1]                                                  # bought $1.00, sold $1.20
    assert last["bought"] == pytest.approx(1.00) and last["sold"] == pytest.approx(1.20)
    assert last["change"] == pytest.approx(0.20) and last["change_pct"] == pytest.approx(0.20)
    assert last["r_multiple"] == pytest.approx(20.0 / 20.0)           # +$20 a contract, 1R = $20
    expired = r[r["sold"] == 0].iloc[0]
    assert expired["r_multiple"] == pytest.approx(-30.0 / 20.0) and expired["qty"] == 2
    assert len(th.recent(t, 2)) == 2 and th.recent(pd.DataFrame()).empty
    assert th.contract_label(t[t.cp == "P"].iloc[0]) == "TEST 330P Oct 7"


def test_payoff_needs_a_winner_and_a_loser(export):
    t = _trades(export)[0]
    assert th.payoff(t[t.pnl > 0]) == {} and th.payoff(t[t.pnl <= 0]) == {}
    assert th.payoff(pd.DataFrame()) == {}


# A day with scratches: +20, -10, and two trades closed for exactly what they cost.
SCRATCH_ROWS = [
    _trade("2026-10-08", "10:00:00", "BUY", C340, 2, -100.0),
    _trade("2026-10-08", "10:05:00", "SELL", C340, -2, 120.0),      # +20  (+10 a contract)
    _trade("2026-10-08", "10:10:00", "BUY", C340, 1, -90.0),
    _trade("2026-10-08", "10:12:00", "SELL", C340, -1, 80.0),       # -10
    _trade("2026-10-08", "10:20:00", "BUY", C340, 2, -190.0),
    _trade("2026-10-08", "10:22:00", "SELL", C340, -2, 190.0),      # scratch
    _trade("2026-10-08", "10:30:00", "BUY", C340, 1, -85.0),
    _trade("2026-10-08", "10:31:00", "SELL", C340, -1, 85.0),       # scratch
]


@pytest.fixture
def scratchy(tmp_path):
    return th.closed_trades(th.load_activities(_write(tmp_path / "s.csv", SCRATCH_ROWS)))[0]


def test_a_break_even_trade_is_a_scratch_not_a_loss(scratchy):
    """Regression: scratches were counted as losses, which shrank the average loss and so
    the size of 1R, and made every result in R look bigger than it was."""
    s = th.summary(scratchy)
    assert (s["trades"], s["wins"], s["losses"], s["scratches"]) == (4, 1, 1, 2)
    assert s["avg_loss"] == pytest.approx(-10.0)                    # not (-10 + 0 + 0) / 3
    assert s["win_rate"] == pytest.approx(0.25)                     # of all four trades
    assert th.risk_units(scratchy)["1DTE+"] == pytest.approx(10.0)
    r = th.recent(scratchy).set_index("pnl")["r_multiple"]
    assert r[20.0] == pytest.approx(1.0) and r[-10.0] == pytest.approx(-1.0)
    assert (r[0.0] == 0.0).all()


def test_payoff_accounts_for_scratches_in_the_break_even_figures(scratchy):
    p = th.payoff(scratchy)                       # 1 win of 20, 1 loss of 10, 2 scratches
    assert (p["win_rate"], p["loss_rate"], p["scratch_rate"]) == (0.25, 0.25, 0.5)
    assert p["avg_win"] == pytest.approx(20.0) and p["avg_loss"] == pytest.approx(-10.0)
    assert p["reward_to_risk"] == pytest.approx(1.0)                # +10 against -10 a contract
    assert p["reward_to_risk_needed"] == pytest.approx(1.0)         # one loss per win
    # break even needs win_rate x 20 = loss_rate x 10 with half the trades scratched
    assert p["breakeven_win_rate"] == pytest.approx(0.5 * 10 / 30)
    assert p["win_needed"] == pytest.approx(10.0)
    assert p["per_trade"] == pytest.approx(2.5)


def test_breakdown_average_loss_ignores_scratches(scratchy):
    pool = th.breakdown(scratchy, "pool").set_index("bucket")
    assert pool.loc["1DTE+", "trades"] == 4 and pool.loc["1DTE+", "avg_loss"] == pytest.approx(-10.0)


def test_daily_and_worst(export):
    t = _trades(export)[0]
    d = th.daily(t)
    assert len(d) == 1 and d.iloc[0]["pnl"] == pytest.approx(-40.0) and d.iloc[0]["trades"] == 4
    assert th.worst(t, 1).iloc[0]["pnl"] == pytest.approx(-60.0)


# ── page ────────────────────────────────────────────────────────────────────

_SCRIPT = """
from ui.pages import scorecard
scorecard.render({"ticker": "TEST"})
"""


def _run(monkeypatch, folder):
    from streamlit.testing.v1 import AppTest

    from ui.pages import scorecard

    monkeypatch.setattr(scorecard, "BROKER_DIR", str(folder))     # never the real data/broker
    at = AppTest.from_string(_SCRIPT, default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def test_page_asks_for_a_file_when_there_is_none(tmp_path, monkeypatch):
    at = _run(monkeypatch, tmp_path / "empty")
    assert any("No trade file yet" in i.value for i in at.info)
    assert len(at.metric) == 0


class _Tables(__import__("html.parser", fromlist=["HTMLParser"]).HTMLParser):
    """Collects every HTML table on the page as a list of rows of cell text."""

    def __init__(self):
        super().__init__()
        self.tables, self._row, self._cell, self._style = [], None, None, False

    def handle_starttag(self, tag, attrs):
        if tag == "style":
            self._style = True
        elif tag == "table":
            self.tables.append([])
        elif tag == "tr":
            self._row = []
        elif tag in ("th", "td"):
            self._cell = ""

    def handle_data(self, data):
        if self._cell is not None and not self._style:
            self._cell += data

    def handle_endtag(self, tag):
        if tag == "style":
            self._style = False
        elif tag in ("th", "td") and self._cell is not None:
            self._row.append(self._cell)
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.tables[-1].append(self._row)
            self._row = None


def _tables(at):
    parser = _Tables()
    for m in at.markdown:
        if "<table" in m.value:
            parser.feed(m.value)
    return parser.tables


def test_page_shows_results_and_breakdowns(export, monkeypatch):
    at = _run(monkeypatch, Path(export).parent)
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Realized result"] == "-$40.00"
    assert metrics["Win rate"] == "50%"
    assert metrics["Average win / loss"] == "$15.00 / -$35.00"
    assert metrics["Closed trades"] == "4"
    titles = " ".join(m.value for m in at.markdown)
    for title in ("Result by day", "Targets and stops", "Latest trades",
                  "Same-day or later expiry",
                  "Holding time", "Premium paid per contract", "Time of entry",
                  "Calls or puts", "Largest losses"):
        assert title in titles
    tables = _tables(at)
    assert len(tables) == 8 and len(at.get("plotly_chart")) == 1
    assert len(at.dataframe) == 0                         # no unevenly aligned grid widgets
    pool = next(t for t in tables if t[0][1] == "Trades")
    assert pool[0] == ["", "Trades", "Result", "Win rate", "Average win", "Average loss"]
    assert pool[1][:3] == ["0DTE", "2", "-$70.00"] and pool[2][:3] == ["1DTE+", "2", "+$30.00"]
    assert not any(ACCOUNT in m.value for m in at.markdown)


def test_page_targets_table(export, monkeypatch):
    at = _run(monkeypatch, Path(export).parent)
    targets = next(t for t in _tables(at) if "Typical win" in t[0])
    assert targets[0] == ["", "Typical win", "Typical loss", "Win rate",
                          "Win rate to break even", "Reward to risk",
                          "Reward to risk to break even", "Per trade"]
    all_row = targets[1]
    assert all_row[0] == "All trades" and all_row[3] == "50%" and all_row[4] == "70%"
    assert all_row[5] == "0.62" and all_row[6] == "1.00" and all_row[7] == "-$10.00"
    assert [r[0] for r in targets[1:]] == ["All trades"]   # each pool here lacks a win or a loss


def test_page_latest_trades_table(export, monkeypatch):
    at = _run(monkeypatch, Path(export).parent)
    recent = next(t for t in _tables(at) if "Bought at" in t[0])
    assert recent[0] == ["Contract", "Entered", "Contracts", "Bought at", "Sold at", "Change",
                         "Result", "In R", "Held (min)"]
    assert recent[1] == ["TEST 330P Oct 7", "Oct 07 09:40", "2", "$0.30", "$0.00",
                         "-$0.30 (-100%)", "-$60.00", "-1.5R", "380"]            # expired
    assert recent[2] == ["TEST 340C Oct 9", "Oct 07 14:00", "1", "$1.00", "$1.20",
                         "+$0.20 (+20%)", "+$20.00", "+1.0R", "10"]
    cap = next(c.value for c in at.caption if c.value.startswith("1R is"))
    assert r"\$20.00 for 0DTE" in cap and cap.count("$") == cap.count(r"\$")


def test_tables_are_centred_one_line_and_safe_for_markdown():
    from ui.widgets import centered_table_html

    html = centered_table_html(pd.DataFrame({"": ["0DTE"], "Result": ["-$572.01"]}), "x")
    assert "\n" not in html and "$" not in html and "&#36;572.01" in html
    css = html.replace(" ", "")
    assert "#T_xth{text-align:center;" in css and "#T_xtd{text-align:center;" in css
    assert "#T_xtd:first-child{text-align:left;" in css
    assert "row_heading" not in html                      # the index is hidden


def test_newest_file_wins_and_uploads_are_validated(tmp_path):
    from ui.pages import scorecard

    folder = tmp_path / "broker"
    old = _write(folder / "old.csv")
    new = _write(folder / "new.csv", ROWS[:3])
    os.utime(old, (1, 1))
    assert scorecard.latest_export(str(folder)) == new
    assert scorecard.latest_export(str(tmp_path / "missing")) is None

    data = ("\n".join([HEADER, *ROWS]) + "\n").encode()
    saved = scorecard.save_upload("../../my export (1).csv", data, str(folder))
    assert Path(saved).parent == folder and Path(saved).name == "my_export__1_.csv"
    with pytest.raises(ValueError, match="not a broker activity export"):
        scorecard.save_upload("junk.csv", b"a,b\n1,2\n", str(folder))
    assert not [p for p in folder.iterdir() if p.name.endswith(".part")]     # nothing left behind
    assert not (folder / "junk.csv").exists()


def test_money_format():
    from ui.pages import scorecard

    assert scorecard.money(12.5) == "+$12.50" and scorecard.money(-506.01) == "-$506.01"
    assert scorecard.money(0) == "$0.00" and scorecard.money(None) == "—"
    assert scorecard.money(-10.83, signed=False) == "-$10.83"


# ── privacy ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["data/broker/activities-export-2026-10-07.csv",
                                  "data/ui_settings.json"])
def test_private_files_are_ignored_by_git(path):
    """The broker export and the settings file must never be committed."""
    out = subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT)
    assert out.returncode == 0, f"{path} is not ignored by git"


def test_scorecard_is_a_menu_page():
    from ui import shell

    assert {e.id: e.title for e in shell.entries()}["scorecard"] == "Scorecard"


def test_page_reports_break_even_trades(tmp_path, monkeypatch):
    _write(tmp_path / "broker" / "s.csv", SCRATCH_ROWS)
    at = _run(monkeypatch, tmp_path / "broker")
    assert any("1 wins, 1 losses, 2 break-even" in c.value for c in at.caption)
    recent = next(t for t in _tables(at) if "Bought at" in t[0])
    assert [row[7] for row in recent[1:]] == ["+0.0R", "+0.0R", "-1.0R", "+1.0R"]
