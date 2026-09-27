"""Picks ledger: expiry-keyed paths, ask-entry/bid-exit, live-window cut."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from picks_ledger import (
    BACKFILL_START,
    RankIntegrityError,
    build_session,
    parse_args,
    render_html,
    render_txt,
    session_date_et,
)

ET = ZoneInfo("America/New_York")
SESSION = "2026-09-11"


SCHEMA = """
CREATE TABLE flags (
    flag_id INTEGER PRIMARY KEY,
    run_id TEXT,
    ts_et TEXT,
    ticker TEXT,
    side TEXT,
    strike REAL,
    expiry TEXT,
    score REAL,
    rank INTEGER,
    bid REAL,
    ask REAL,
    spot REAL,
    is_control INTEGER DEFAULT 0,
    pool TEXT,
    dte INTEGER,
    delta REAL,
    iv REAL,
    volume INTEGER,
    open_interest INTEGER,
    mark_close REAL,
    mark_expiry REAL,
    close_method TEXT,
    method_t15m TEXT,
    method_t30m TEXT,
    paired_flag_id INTEGER
);
"""


def _ts(h, m, s=0) -> str:
    return datetime(2026, 9, 11, h, m, s, tzinfo=ET).isoformat(timespec="seconds")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.executescript(SCHEMA)
    return c


def _ins(conn, *, ts, side, strike, expiry, rank, bid, ask, spot=333.0,
         score=0.2, dte=7, delta=0.3, fid=None, run_id="r1"):
    conn.execute(
        """
        INSERT INTO flags (
            flag_id, run_id, ts_et, ticker, side, strike, expiry, score, rank,
            bid, ask, spot, is_control, pool, dte, delta, iv, volume,
            open_interest, mark_close, mark_expiry
        ) VALUES (?, ?, ?, 'AAPL', ?, ?, ?, ?, ?, ?, ?, ?, 0, '1DTE+',
                  ?, ?, 0.4, 1000, 2000, ?, NULL)
        """,
        (fid, run_id, ts, side, strike, expiry, score, rank, bid, ask, spot,
         dte, delta, bid),
    )


def test_four_340c_expiries_are_four_picks():
    conn = _conn()
    expiries = ("2026-09-14", "2026-09-16", "2026-09-18", "2026-10-16")
    for i, exp in enumerate(expiries, start=1):
        _ins(conn, ts=_ts(10, 0), side="CALL", strike=340, expiry=exp,
             rank=i, bid=0.6, ask=0.7, fid=i)
        _ins(conn, ts=_ts(10, 5), side="CALL", strike=340, expiry=exp,
             rank=i, bid=0.8, ask=0.9, fid=10 + i)
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    keys = {(p.side, p.strike, p.expiry) for p in rep.picks}
    assert len(keys) == 4
    assert keys == {("CALL", 340.0, e) for e in expiries}


def test_pnl_is_ask_entry_bid_exit_and_ignores_after_hours():
    conn = _conn()
    _ins(conn, ts=_ts(10, 0), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=1, bid=1.90, ask=2.00, spot=334.0, fid=1)
    _ins(conn, ts=_ts(10, 5), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=2, bid=1.70, ask=1.80, spot=334.0, fid=2)
    _ins(conn, ts=_ts(10, 10), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=3, bid=2.60, ask=2.70, spot=334.0, fid=3)
    _ins(conn, ts=_ts(16, 20), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=1, bid=9.00, ask=9.10, spot=334.0, fid=4)
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    assert len(rep.picks) == 1
    p = rep.picks[0]
    assert p.entry_ask == 2.0
    assert p.mfe == 2.6
    assert p.mfe_pct == pytest.approx((2.6 - 2.0) / 2.0)
    assert p.mae == 1.7
    assert p.mae_pct == pytest.approx((1.7 - 1.90) / 1.90)
    assert p.spread_cost == pytest.approx((2.0 - 1.90) / 2.0)
    assert p.mae_pct != pytest.approx(-p.spread_cost)
    assert p.t_mfe == pytest.approx(10.0)
    assert p.t_decay is None
    assert all(pt["bid"] != 9.0 for pt in p.path)
    assert p.mark_close == 2.60
    assert p.pnl_close == pytest.approx((2.60 - 2.00) / 2.00)
    assert p.moneyness == pytest.approx((337.5 - 334.0) / 334.0)
    assert p.spread_pct == pytest.approx((2.0 - 1.90) / 2.0)


def test_t_decay_and_streaks_in_scans():
    conn = _conn()
    bids = [0.90, 1.10, 1.20, 0.95]
    for i, bid in enumerate(bids):
        ask = 1.00 if i == 0 else bid + 0.05
        _ins(conn, ts=_ts(11, i * 5), side="PUT", strike=330, expiry="2026-09-18",
             rank=1, bid=bid, ask=ask, fid=i + 1)
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    p = rep.picks[0]
    assert p.scans_profitable == 2
    assert p.max_win_streak_scans == 2
    assert p.t_first_profitable == pytest.approx(5.0)
    assert p.t_decay == pytest.approx(15.0)
    txt = render_txt(rep)
    assert "scans" in txt.lower()
    html = render_html(rep)
    assert "entry_ask" in html
    assert "cdn." not in html.lower()


def test_low_coverage_excluded_from_mfe_aggregates():
    conn = _conn()
    scans = [0, 5, 10, 15, 20]
    for i, m in enumerate(scans):
        _ins(conn, ts=_ts(10, m), side="CALL", strike=335, expiry="2026-09-18",
             rank=1, bid=2.0 + i * 0.5, ask=2.1, fid=100 + i)
        if i == 0:
            _ins(conn, ts=_ts(10, m), side="PUT", strike=325, expiry="2026-09-18",
                 rank=2, bid=1.0, ask=1.1, fid=200)
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    by_side = {p.side: p for p in rep.picks}
    assert by_side["PUT"].low_coverage
    assert not by_side["CALL"].low_coverage
    assert rep.n_reached_30 == 1
    assert rep.n_usable == 1
    txt = render_txt(rep)
    html = render_html(rep)
    assert "INSUFFICIENT DATA" in txt
    assert "INSUFFICIENT DATA" in html
    assert "1 / 2 usable" in txt


def test_stale_window_note_removed_from_eod_report():
    import eod_report
    assert not hasattr(eod_report, "WINDOW_END_NOTE")
    src = open(eod_report.__file__, encoding="utf-8").read()
    assert "do not act on these until the window closes" not in src


def test_backfill_default_start():
    assert BACKFILL_START == "2026-08-10"


def test_close_is_last_live_bid_not_after_hours():
    conn = _conn()
    _ins(conn, ts=_ts(10, 0), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=1, bid=1.90, ask=2.00, spot=334.0, fid=1)
    _ins(conn, ts=_ts(10, 5), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=2, bid=1.70, ask=1.80, spot=334.0, fid=2)
    _ins(conn, ts=_ts(10, 10), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=3, bid=2.60, ask=2.70, spot=334.0, fid=3)
    _ins(conn, ts=_ts(16, 20), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=1, bid=9.00, ask=9.10, spot=334.0, fid=4)
    conn.commit()
    p = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:").picks[0]
    assert p.mark_close == 2.60
    assert p.pnl_close == pytest.approx((2.60 - 2.00) / 2.00)


def test_pick_set_is_first_scan_live_top_ten():
    """Live ranks 1–10 at the first scan; later high scores do not replace them."""
    conn = _conn()
    for i in range(12):
        _ins(
            conn, ts=_ts(10, 0), side="CALL", strike=300 + i, expiry="2026-09-18",
            rank=i + 1, bid=1.0, ask=1.1, score=0.50 - i * 0.01, fid=i + 1,
        )
    _ins(
        conn, ts=_ts(11, 0), side="CALL", strike=400, expiry="2026-09-18",
        rank=1, bid=1.0, ask=1.1, score=0.99, fid=99,
    )
    for i in range(9):
        _ins(
            conn, ts=_ts(11, 0), side="CALL", strike=300 + i, expiry="2026-09-18",
            rank=i + 2, bid=1.0, ask=1.1, score=0.40, fid=100 + i,
        )
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    strikes = {p.strike for p in rep.picks}
    assert len(rep.picks) == 10
    assert 400.0 not in strikes
    assert 310.0 not in strikes
    assert 311.0 not in strikes
    assert strikes == {300.0 + i for i in range(10)}


def test_missing_scan_breaks_win_streak():
    conn = _conn()
    # Grid: 10:00, 10:05, 10:10. B is missing at 10:05 — streak must reset.
    for i, m in enumerate((0, 5, 10)):
        _ins(conn, ts=_ts(10, m), side="CALL", strike=335, expiry="2026-09-18",
             rank=1, bid=2.2, ask=2.0, score=0.4, fid=10 + i)
    _ins(conn, ts=_ts(10, 0), side="PUT", strike=325, expiry="2026-09-18",
         rank=2, bid=1.2, ask=1.0, score=0.3, fid=1)
    _ins(conn, ts=_ts(10, 10), side="PUT", strike=325, expiry="2026-09-18",
         rank=2, bid=1.2, ask=1.1, score=0.3, fid=2)
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    put = next(p for p in rep.picks if p.side == "PUT")
    assert put.max_win_streak_scans == 1
    assert put.scans_profitable == 2


def test_reconstructed_label_and_spot_chart_and_na_decay():
    conn = _conn()
    _ins(conn, ts=_ts(10, 0), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=1, bid=1.90, ask=2.00, spot=334.0, fid=1)
    _ins(conn, ts=_ts(10, 5), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=1, bid=2.20, ask=2.30, spot=334.2, fid=2)
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    txt = render_txt(rep)
    html = render_html(rep)
    assert "RECONSTRUCTED" in txt
    assert "RECONSTRUCTED" in html
    assert "id=\"spot\"" in html
    assert "N/A" in txt
    assert "never profitable" in txt
    assert "usable" in txt
    assert rep.picks[0].t_decay is None
    assert any(pt.get("spot") == 334.0 for pt in rep.picks[0].path)


def test_duplicate_ranks_in_one_scan_fail_loudly():
    conn = _conn()
    _ins(conn, ts=_ts(10, 0), side="CALL", strike=335, expiry="2026-09-18",
         rank=1, bid=1.0, ask=1.1, fid=1)
    _ins(conn, ts=_ts(10, 0), side="CALL", strike=340, expiry="2026-09-18",
         rank=1, bid=1.0, ask=1.1, fid=2)
    conn.commit()
    with pytest.raises(RankIntegrityError, match="duplicate ranks"):
        build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")


def test_double_logged_run_at_same_ts_does_not_fail():
    """Two run_ids at one timestamp (scanner double-fire) are copies, not mixed ranks."""
    conn = _conn()
    for rid, fid0 in (("r1", 1), ("r2", 11)):
        _ins(conn, ts=_ts(10, 0), side="CALL", strike=335, expiry="2026-09-18",
             rank=1, bid=1.90, ask=2.00, fid=fid0, run_id=rid)
        _ins(conn, ts=_ts(10, 0), side="CALL", strike=340, expiry="2026-09-18",
             rank=2, bid=1.90, ask=2.00, fid=fid0 + 1, run_id=rid)
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    assert {p.strike for p in rep.picks} == {335.0, 340.0}


def test_session_date_et_at_2330_is_that_trading_day_not_utc_next():
    now = datetime(2026, 9, 14, 23, 30, tzinfo=ET)
    assert session_date_et(now) == "2026-09-14"
    assert now.astimezone(ZoneInfo("UTC")).date().isoformat() == "2026-09-15"
    utc = datetime(2026, 9, 15, 3, 30, tzinfo=ZoneInfo("UTC"))
    assert session_date_et(utc) == "2026-09-14"
    conn = _conn()
    _ins(conn, ts=_ts(10, 0), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=1, bid=1.90, ask=2.00, fid=1)
    conn.commit()
    gen = datetime(2026, 9, 11, 23, 30, tzinfo=ET)
    rep = build_session(
        conn, ticker="AAPL", session=SESSION, db_path=":memory:", generated=gen,
    )
    txt = render_txt(rep)
    assert txt.startswith("PICKS LEDGER — 2026-09-11")
    assert "2026-09-12" not in txt.split("generated", 1)[0]


def test_cli_session_alias():
    a = parse_args(["--session", "2026-09-14", "--ticker", "AAPL"])
    assert a.session == "2026-09-14"
    b = parse_args(["--date", "2026-09-11"])
    assert b.session == "2026-09-11"


def test_wide_spread_dropped_from_pick_set_not_refilled():
    conn = _conn()
    for i in range(11):
        # rank 1 = 40% spread; ranks 2–11 tight. Do not promote rank 11.
        if i == 0:
            bid, ask = 1.80, 3.00  # 40%
        else:
            bid, ask = 1.90, 2.00
        _ins(
            conn, ts=_ts(10, 0), side="CALL", strike=300 + i, expiry="2026-09-18",
            rank=i + 1, bid=bid, ask=ask, fid=i + 1,
        )
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    ranks = {p.first_seen_rank for p in rep.picks}
    strikes = {p.strike for p in rep.picks}
    assert 1 not in ranks
    assert 11 not in ranks
    assert 300.0 not in strikes
    assert 310.0 not in strikes
    assert all(
        p.spread_pct is None or p.spread_pct <= 0.25 + 1e-12 for p in rep.picks
    )


def test_mae_from_entry_bid_not_identically_minus_spread_at_t0():
    conn = _conn()
    _ins(conn, ts=_ts(10, 0), side="CALL", strike=337.5, expiry="2026-09-18",
         rank=1, bid=1.90, ask=2.00, fid=1)
    conn.commit()
    p = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:").picks[0]
    assert p.spread_cost == pytest.approx(0.05)
    assert p.mae_pct == pytest.approx(0.0)
    assert p.mae_pct != pytest.approx(-p.spread_cost)


def test_header_n_never_profitable_and_usable_over_picks():
    conn = _conn()
    for i, m in enumerate((0, 5, 10, 15, 20)):
        _ins(conn, ts=_ts(10, m), side="CALL", strike=335, expiry="2026-09-18",
             rank=1, bid=0.90, ask=1.00, fid=i + 1)
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    assert rep.n_usable == 1
    assert len(rep.picks) == 1
    assert rep.n_never_profitable == 1
    assert "1 / 1 usable" in rep.summary
    assert "1 never profitable" in rep.summary
    txt = render_txt(rep)
    html = render_html(rep)
    assert "1 never profitable" in txt
    assert "1 / 1 usable" in html


def test_overlay_json_includes_benchmark_series():
    conn = _conn()
    for i in range(11):
        _ins(
            conn, ts=_ts(10, 0), side="CALL", strike=300 + i, expiry="2026-09-18",
            rank=i + 1, bid=1.90, ask=2.00, dte=7, fid=i + 1,
        )
    conn.commit()
    rep = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:")
    assert len(rep.picks) == 10
    assert rep.benchmark is not None
    assert "bench" in rep.benchmark["label"]
    html = render_html(rep)
    assert "LEDGER.benchmark" in html or '"benchmark"' in html
    assert "same-DTE" in html


def test_open_suspect_before_0935():
    conn = _conn()
    _ins(conn, ts=_ts(9, 32), side="CALL", strike=335, expiry="2026-09-18",
         rank=1, bid=1.90, ask=2.00, fid=1)
    conn.commit()
    p = build_session(conn, ticker="AAPL", session=SESSION, db_path=":memory:").picks[0]
    assert p.open_suspect
