"""Attribution layer: multipliers, controls, WAL schema, mark immutability."""

from __future__ import annotations

import inspect
import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
import pytz

from attribution import (
    _db,
    _ensure_schema,
    alert_attribution_failure,
    build_control_rows,
    config_hash,
    due_for_marking,
    log_run,
    score_from_flag_parts,
    write_mark,
)
from best_value import calculate_best_value
from config import SCORING

ET = pytz.timezone("US/Eastern")
NOW = ET.localize(datetime(2026, 7, 20, 11, 0, 0))


def _sample_chain(spot: float = 250.0, expiry: str = "2026-08-21") -> pd.DataFrame:
    rows = []
    for side in ("CALL", "PUT"):
        for k in (240.0, 245.0, 250.0, 255.0, 260.0):
            rows.append({
                "side": side,
                "strike": k,
                "expiry": expiry,
                "dte": 32,
                "last": abs(spot - k) * 0.02 + 1.5,
                "bid": 1.0,
                "ask": 2.0,
                "volume": 800,
                "openInterest": 1000,
                "iv": 0.35,
            })
    return pd.DataFrame(rows)


def test_wal_schema(tmp_path):
    db = str(tmp_path / "smoke.db")
    with _db(db) as c:
        names = [
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','view')"
            )
        ]
        journal = c.execute("PRAGMA journal_mode").fetchone()[0]
    assert "runs" in names
    assert "flags" in names
    assert "v_outcomes" in names
    assert str(journal).lower() == "wal"


def test_config_hash_stable_and_sensitive():
    h1 = config_hash(SCORING)
    h2 = config_hash(dict(SCORING))
    assert h1 == h2
    tweaked = dict(SCORING)
    tweaked["mult_pov_urgency"] = float(tweaked["mult_pov_urgency"]) + 0.01
    assert config_hash(tweaked) != h1


def test_multipliers_are_recorded():
    df = _sample_chain()
    out = calculate_best_value(
        df,
        spot_price=250.0,
        now_et=NOW,
        daily_bias="HEAVY BULLISH",
        news_bias="BULLISH",
    )
    scored = out[out["Value_Score"].notna()]
    assert not scored.empty
    m = scored["_multipliers"].iloc[0]
    assert m, "multiplier breakdown is empty"
    assert isinstance(m, dict)
    w_lev = float(SCORING["w_lev"])
    w_flow = float(SCORING["w_flow"])
    base = scored["_nlev"].iloc[0] * w_lev + scored["_nflow"].iloc[0] * w_flow
    prod = base
    for v in m.values():
        prod *= v
    assert round(prod, 4) == pytest.approx(scored["Value_Score"].iloc[0], abs=1e-9)


def test_control_is_deterministic():
    chain = _sample_chain()
    a = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    b = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    assert a.equals(b)
    assert len(a) == 2
    assert set(a["side"]) == {"CALL", "PUT"}
    assert set(a["strike"]) == {250.0}


def test_control_is_independent_of_engine_output():
    """Control selection ignores scores — same rule whether scored is empty or not."""
    chain = _sample_chain()
    scored = calculate_best_value(chain, spot_price=250.0, now_et=NOW)
    ctrl = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    # API takes chain + spot + expiry only (no scored_df / Value_Score)
    assert "Value_Score" not in ctrl.columns
    assert list(ctrl["strike"].unique()) == [250.0]
    # Changing scores cannot change control without changing chain/spot/expiry
    scored2 = scored.copy()
    if scored2["Value_Score"].notna().any():
        scored2.loc[scored2["Value_Score"].notna(), "Value_Score"] = 0.0
    ctrl2 = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    assert ctrl.equals(ctrl2)


def test_log_run_reconciles_counts(tmp_path):
    db = str(tmp_path / "attr.db")
    chain = _sample_chain()
    scored = calculate_best_value(
        chain, spot_price=250.0, now_et=NOW, daily_bias="HEAVY BULLISH",
    )
    ctrl = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    run_id = log_run(
        ticker="TEST",
        scored_df=scored,
        cfg=SCORING,
        spot=250.0,
        daily_bias="HEAVY BULLISH",
        control_rows=ctrl,
        db_path=db,
        ts_et=NOW,
    )
    with _db(db) as c:
        n_scored = c.execute(
            "SELECT n_scored FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        n_flags = c.execute(
            "SELECT COUNT(*) FROM flags WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        n_ctrl = c.execute(
            "SELECT SUM(is_control) FROM flags WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        empty = c.execute(
            """
            SELECT COUNT(*) FROM flags
            WHERE run_id=? AND (multipliers='{}' OR multipliers IS NULL)
            """,
            (run_id,),
        ).fetchone()[0]
        missing_base = c.execute(
            """
            SELECT COUNT(*) FROM flags
            WHERE run_id=? AND is_control=0
              AND (nlev IS NULL OR nflow IS NULL OR base_score IS NULL)
            """,
            (run_id,),
        ).fetchone()[0]
    assert n_flags == n_scored + n_ctrl
    assert n_ctrl >= 1
    assert empty == 0
    assert missing_base == 0


def test_db_row_multipliers_reproduce_score(tmp_path):
    """A2: a stored flags row alone must reproduce score (VERIFY load-bearing check)."""
    db = str(tmp_path / "a2.db")
    chain = _sample_chain()
    scored = calculate_best_value(
        chain,
        spot_price=250.0,
        now_et=NOW,
        daily_bias="HEAVY BULLISH",
        news_bias="BULLISH",
        market_state="BULLISH TAILWIND",
    )
    ctrl = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    run_id = log_run(
        ticker="TEST",
        scored_df=scored,
        cfg=SCORING,
        spot=250.0,
        control_rows=ctrl,
        db_path=db,
        ts_et=NOW,
    )
    with _db(db) as c:
        rows = c.execute(
            """
            SELECT score, nlev, nflow, base_score, multipliers
            FROM flags WHERE run_id=? AND is_control=0
            """,
            (run_id,),
        ).fetchall()
    assert rows
    for r in rows:
        rebuilt = score_from_flag_parts(
            r["nlev"], r["nflow"], r["multipliers"], base_score=r["base_score"],
        )
        assert rebuilt == pytest.approx(r["score"], abs=1e-9)
        # also via nlev/nflow + config weights
        rebuilt2 = score_from_flag_parts(r["nlev"], r["nflow"], r["multipliers"])
        assert rebuilt2 == pytest.approx(r["score"], abs=1e-9)


def test_alert_attribution_failure_never_raises(monkeypatch):
    monkeypatch.setattr("attribution._load_env_file", lambda: None)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    assert alert_attribution_failure("unit-test") is False


def test_marks_are_immutable(tmp_path):
    db = str(tmp_path / "marks.db")
    chain = _sample_chain()
    scored = calculate_best_value(chain, spot_price=250.0, now_et=NOW)
    ctrl = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    # Backdate so due_for_marking finds the row
    past = ET.localize(datetime(2026, 7, 20, 9, 0, 0))
    log_run(
        ticker="TEST",
        scored_df=scored,
        cfg=SCORING,
        spot=250.0,
        control_rows=ctrl,
        db_path=db,
        ts_et=past,
    )
    due = due_for_marking("t1h", db_path=db, as_of=NOW)
    assert due
    fid = int(due[0]["flag_id"])
    assert write_mark(fid, "t1h", 3.25, db_path=db) is True
    assert write_mark(fid, "t1h", 9.99, db_path=db) is False  # no overwrite
    with _db(db) as c:
        v = c.execute(
            "SELECT mark_t1h FROM flags WHERE flag_id=?", (fid,)
        ).fetchone()[0]
    assert v == 3.25
    # Zero / None must not poison
    assert write_mark(fid, "t1d", 0.0, db_path=db) is False
    assert write_mark(fid, "t1d", None, db_path=db) is False


def test_mark_records_timestamp(tmp_path, monkeypatch):
    db = str(tmp_path / "mark_ts.db")
    chain = _sample_chain()
    scored = calculate_best_value(chain, spot_price=250.0, now_et=NOW)
    ctrl = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    past = ET.localize(datetime(2026, 7, 20, 9, 0, 0))
    log_run(
        ticker="TEST", scored_df=scored, cfg=SCORING, spot=250.0,
        control_rows=ctrl, db_path=db, ts_et=past,
    )
    mark_time = ET.localize(datetime(2026, 7, 20, 12, 0, 0))
    monkeypatch.setattr("attribution.now_et", lambda: mark_time)
    with _db(db) as c:
        fid = c.execute(
            "SELECT flag_id FROM flags WHERE is_control=0 LIMIT 1"
        ).fetchone()[0]
    assert write_mark(fid, "t1h", 2.5, db_path=db) is True
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_t1h, marked_t1h_at FROM flags WHERE flag_id=?",
            (fid,),
        ).fetchone()
    assert row["mark_t1h"] == 2.5
    assert row["marked_t1h_at"] is not None
    parsed = datetime.fromisoformat(row["marked_t1h_at"])
    assert parsed.tzinfo is not None
    assert parsed.astimezone(ZoneInfo("America/New_York")) == mark_time


def test_mark_value_and_timestamp_are_atomic():
    src = inspect.getsource(write_mark)
    # Single UPDATE must set value and *_at together; no second statement path.
    assert "UPDATE flags" in src
    assert src.count("UPDATE flags") == 1
    assert "SET {col} = ?, {at_col} = ?" in src
    assert "AND {col} IS NULL" in src
    # Rejected writes leave both NULL
    # (covered operationally below via refuse path)


def test_mark_refuse_leaves_both_null(tmp_path):
    db = str(tmp_path / "atomic.db")
    chain = _sample_chain()
    scored = calculate_best_value(chain, spot_price=250.0, now_et=NOW)
    ctrl = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    log_run(
        ticker="TEST", scored_df=scored, cfg=SCORING, spot=250.0,
        control_rows=ctrl, db_path=db, ts_et=NOW,
    )
    with _db(db) as c:
        fid = c.execute(
            "SELECT flag_id FROM flags WHERE is_control=0 LIMIT 1"
        ).fetchone()[0]
    assert write_mark(fid, "t1h", 0.0, db_path=db) is False
    with _db(db) as c:
        row = c.execute(
            "SELECT mark_t1h, marked_t1h_at FROM flags WHERE flag_id=?",
            (fid,),
        ).fetchone()
    assert row["mark_t1h"] is None
    assert row["marked_t1h_at"] is None


def test_migration_preserves_existing_rows(tmp_path):
    db = str(tmp_path / "migrate.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            ts_et TEXT NOT NULL,
            ticker TEXT NOT NULL,
            n_scored INTEGER NOT NULL,
            config_hash TEXT NOT NULL
        );
        CREATE TABLE flags (
            flag_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(run_id),
            ts_et TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            strike REAL NOT NULL,
            expiry TEXT NOT NULL,
            score REAL,
            rank INTEGER,
            multipliers TEXT NOT NULL DEFAULT '{}',
            mid REAL,
            is_control INTEGER NOT NULL DEFAULT 0,
            mark_t1h REAL,
            mark_t1d REAL,
            mark_expiry REAL
        );
        INSERT INTO runs VALUES ('r1','2026-07-20T10:00:00-04:00','TEST',1,'abc');
        INSERT INTO flags (
            run_id, ts_et, ticker, side, strike, expiry,
            score, rank, multipliers, mid, is_control
        ) VALUES (
            'r1','2026-07-20T10:00:00-04:00','TEST','CALL',250.0,'2026-08-21',
            0.5, 1, '{"_base":1.0}', 1.25, 0
        );
        """
    )
    conn.commit()
    before = conn.execute(
        "SELECT flag_id, score, mid, ticker FROM flags"
    ).fetchone()
    cols_before = {r[1] for r in conn.execute("PRAGMA table_info(flags)")}
    assert "marked_t1h_at" not in cols_before
    _ensure_schema(conn)
    conn.commit()
    cols_after = {r[1] for r in conn.execute("PRAGMA table_info(flags)")}
    for col in ("marked_t1h_at", "marked_t1d_at", "marked_exp_at"):
        assert col in cols_after
    after = conn.execute(
        "SELECT flag_id, score, mid, ticker, marked_t1h_at, marked_t1d_at, marked_exp_at "
        "FROM flags"
    ).fetchone()
    assert after[0] == before[0]
    assert after[1] == before[1]
    assert after[2] == before[2]
    assert after[3] == before[3]
    assert after[4] is None and after[5] is None and after[6] is None
    n = conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0]
    assert n == 1
    conn.close()


def test_hours_elapsed_computed(tmp_path, monkeypatch):
    db = str(tmp_path / "hours.db")
    chain = _sample_chain()
    scored = calculate_best_value(chain, spot_price=250.0, now_et=NOW)
    ctrl = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    t0 = ET.localize(datetime(2026, 7, 20, 10, 0, 0))
    t1 = t0 + timedelta(hours=3)
    log_run(
        ticker="TEST", scored_df=scored, cfg=SCORING, spot=250.0,
        control_rows=ctrl, db_path=db, ts_et=t0,
    )
    monkeypatch.setattr("attribution.now_et", lambda: t1)
    with _db(db) as c:
        fid = c.execute(
            "SELECT flag_id FROM flags WHERE is_control=0 LIMIT 1"
        ).fetchone()[0]
    assert write_mark(fid, "t1h", 4.0, db_path=db) is True
    with _db(db) as c:
        hours = c.execute(
            "SELECT hours_t1h FROM v_outcomes WHERE flag_id=?", (fid,)
        ).fetchone()[0]
        unmarked = c.execute(
            "SELECT hours_t1d FROM v_outcomes WHERE flag_id=?", (fid,)
        ).fetchone()[0]
    assert hours == pytest.approx(3.0, abs=0.05)
    assert unmarked is None


def test_flag_state_columns_populated(tmp_path):
    db = str(tmp_path / "state.db")
    chain = _sample_chain()
    scored = calculate_best_value(chain, spot_price=250.0, now_et=NOW)
    ctrl = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    log_run(
        ticker="TEST", scored_df=scored, cfg=SCORING, spot=250.0,
        control_rows=ctrl, db_path=db, ts_et=NOW,
    )
    with _db(db) as c:
        row = c.execute(
            """
            SELECT dte, volume, open_interest, iv
            FROM flags WHERE is_control=0 LIMIT 1
            """
        ).fetchone()
    assert row["dte"] == 32
    assert row["volume"] == 800
    assert row["open_interest"] == 1000
    assert row["iv"] == pytest.approx(0.35)


def test_missing_source_column_writes_null(tmp_path):
    db = str(tmp_path / "missing_iv.db")
    chain = _sample_chain().drop(columns=["iv"])
    scored = calculate_best_value(chain, spot_price=250.0, now_et=NOW)
    # Ensure iv really absent on scored frame too
    if "iv" in scored.columns:
        scored = scored.drop(columns=["iv"])
    log_run(
        ticker="TEST", scored_df=scored, cfg=SCORING, spot=250.0,
        control_rows=None, db_path=db, ts_et=NOW,
    )
    with _db(db) as c:
        row = c.execute(
            "SELECT iv FROM flags WHERE is_control=0 LIMIT 1"
        ).fetchone()
    assert row["iv"] is None
    assert row["iv"] != 0.0


def test_control_rows_also_carry_state(tmp_path):
    db = str(tmp_path / "ctrl_state.db")
    chain = _sample_chain()
    scored = calculate_best_value(chain, spot_price=250.0, now_et=NOW)
    ctrl = build_control_rows(chain, spot=250.0, expiry="2026-08-21")
    log_run(
        ticker="TEST", scored_df=scored, cfg=SCORING, spot=250.0,
        control_rows=ctrl, db_path=db, ts_et=NOW,
    )
    with _db(db) as c:
        rows = c.execute(
            """
            SELECT dte, volume, open_interest, iv
            FROM flags WHERE is_control=1
            """
        ).fetchall()
    assert len(rows) >= 1
    for row in rows:
        assert row["dte"] == 32
        assert row["volume"] == 800
        assert row["open_interest"] == 1000
        assert row["iv"] == pytest.approx(0.35)
