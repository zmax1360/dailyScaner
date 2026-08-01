"""Paper-strategy entry rules — fixed in advance, clustered, no lookahead."""

from __future__ import annotations

import io
import sqlite3
from contextlib import redirect_stdout

import pytest

from eod_report import (
    CONFIRM_N,
    ENTRY_TIME_ET,
    ReportFilter,
    _load_paper_scans,
    _pick_entry_clock,
    _pick_entry_confirm,
    _pick_entry_first_seen,
    paper_trades_for_rule,
    section_paper_strategy,
)


SCHEMA = """
CREATE TABLE flags (
    flag_id INTEGER PRIMARY KEY,
    run_id TEXT,
    ts_et TEXT,
    ticker TEXT,
    side TEXT,
    strike REAL,
    expiry TEXT,
    dte INTEGER,
    rank INTEGER,
    is_control INTEGER,
    mid REAL,
    mark_close REAL
);
"""


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _ins(
    conn,
    *,
    ts: str,
    run_id: str,
    strike: float,
    mid: float,
    mark_close: float | None,
    rank: int | None = 1,
    is_control: int = 0,
    dte: int = 0,
    side: str = "CALL",
    expiry: str = "2026-07-31",
    ticker: str = "AAPL",
):
    conn.execute(
        """
        INSERT INTO flags (
            run_id, ts_et, ticker, side, strike, expiry, dte,
            rank, is_control, mid, mark_close
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            run_id, ts, ticker, side, strike, expiry, dte,
            rank, is_control, mid, mark_close,
        ),
    )


def test_constants_locked_defaults():
    assert ENTRY_TIME_ET == "10:00"
    assert CONFIRM_N == 5


def test_first_seen_and_clock_and_confirm():
    # A then B then B then B then B then B — CONFIRM enters on 5th B
    scans = [
        {"clock": "09:50:00", "contract": "A"},
        {"clock": "09:55:00", "contract": "B"},
        {"clock": "10:00:00", "contract": "B"},
        {"clock": "10:05:00", "contract": "B"},
        {"clock": "10:10:00", "contract": "B"},
        {"clock": "10:15:00", "contract": "B"},
    ]
    assert _pick_entry_first_seen(scans) == 0
    assert scans[_pick_entry_clock(scans)]["contract"] == "B"
    assert scans[_pick_entry_clock(scans)]["clock"] == "10:00:00"
    # streak: A=1; B=1..5 → enter on 5th consecutive B at idx 5
    assert _pick_entry_confirm(scans) == 5
    assert scans[_pick_entry_confirm(scans)]["contract"] == "B"


def test_confirm_enters_on_fifth_consecutive():
    scans = [{"clock": f"10:{i:02d}:00", "contract": "X"} for i in range(5)]
    assert _pick_entry_confirm(scans) == 4
    scans2 = [{"clock": f"10:{i:02d}:00", "contract": "X"} for i in range(4)]
    assert _pick_entry_confirm(scans2) is None


def test_clustering_one_trade_per_contract_day():
    conn = _conn()
    # Same contract rank-1 many times; mark_close set
    for i, mm in enumerate(range(50, 56)):
        _ins(
            conn,
            ts=f"2026-07-31T10:{mm:02d}:00-04:00",
            run_id=f"r{i}",
            strike=302.5,
            mid=1.0 + i * 0.1,
            mark_close=6.85,
            dte=0,
        )
    scans = _load_paper_scans(conn, "1=1", [], control=False)
    trades = paper_trades_for_rule(scans, "FIRST_SEEN", is_control=False)
    assert len(trades) == 1
    assert trades[0].entry_mid == pytest.approx(1.0)
    assert trades[0].pnl == pytest.approx((6.85 - 1.0) * 100)
    assert trades[0].persist_scans == 6


def test_clock_1000_skips_pre_ten():
    conn = _conn()
    _ins(
        conn, ts="2026-07-31T09:50:00-04:00", run_id="r0",
        strike=300.0, mid=2.0, mark_close=3.0, dte=0,
    )
    _ins(
        conn, ts="2026-07-31T10:05:00-04:00", run_id="r1",
        strike=305.0, mid=1.5, mark_close=4.0, dte=0,
    )
    scans = _load_paper_scans(conn, "1=1", [], control=False)
    trades = paper_trades_for_rule(scans, "CLOCK_1000", is_control=False)
    assert len(trades) == 1
    assert "305" in trades[0].contract
    assert trades[0].entry_mid == pytest.approx(1.5)


def test_no_lookahead_cheapest_entry():
    """Falling mids against a fixed exit must not select the cheapest mid."""
    conn = _conn()
    mids = [1.64, 0.94, 0.82, 0.66, 0.64]
    for i, mid in enumerate(mids):
        _ins(
            conn,
            ts=f"2026-07-31T11:{i:02d}:00-04:00",
            run_id=f"r{i}",
            strike=302.5,
            mid=mid,
            mark_close=6.85,
            dte=0,
        )
    scans = _load_paper_scans(conn, "1=1", [], control=False)
    # FIRST_SEEN uses first mid 1.64 — not the cheapest 0.64
    t = paper_trades_for_rule(scans, "FIRST_SEEN", is_control=False)[0]
    assert t.entry_mid == pytest.approx(1.64)
    # CONFIRM_5 enters on 5th scan at 0.64 — that is time-based, not exit-based
    t5 = paper_trades_for_rule(scans, "CONFIRM_5", is_control=False)[0]
    assert t5.entry_mid == pytest.approx(0.64)
    assert t5.persist_scans == 1  # only 5 scans total, entry at last


def test_section_prints_anti_curve_fit_banner():
    conn = _conn()
    buf = io.StringIO()
    with redirect_stdout(buf):
        section_paper_strategy(conn, ReportFilter(on_date="2026-07-31"))
    text = buf.getvalue()
    assert (
        "three entry rules, fixed in advance — "
        "do not add, remove, or tune after seeing results"
    ) in text
    assert "FIRST_SEEN" in text and "CLOCK_1000" in text and "CONFIRM_5" in text
    assert "CONTROL" in text
