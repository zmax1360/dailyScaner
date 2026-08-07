"""Persist scoring inputs + IV-vs-realized instrumentation (no scoring change)."""

from __future__ import annotations

import ast
import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import pytz

from attribution import _db, _ensure_schema, log_run
from best_value import calculate_best_value
from config import SCORING
from features.realized_vol import iv_premium, realized_vol

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


def _log_scored(tmp_path, scored=None, **kwargs):
    db = str(tmp_path / "persist.db")
    chain = _sample_chain()
    if scored is None:
        scored = calculate_best_value(
            chain,
            spot_price=250.0,
            now_et=NOW,
            daily_bias="HEAVY BULLISH",
            news_bias="BULLISH",
            market_state="BULLISH TAILWIND",
            optimal_strategy="(+2) BULLISH",
        )
    run_id = log_run(
        ticker="TEST",
        scored_df=scored,
        cfg=SCORING,
        spot=250.0,
        db_path=db,
        ts_et=NOW,
        **kwargs,
    )
    return db, run_id, scored


# ── Part 1 ───────────────────────────────────────────────────────────────────

def test_multipliers_reproduce_value_score(tmp_path):
    """Load-bearing: base_score * product(multipliers) == score (rel=1e-9)."""
    db, run_id, _ = _log_scored(tmp_path)
    with _db(db) as c:
        rows = c.execute(
            """
            SELECT score, base_score, multipliers
            FROM flags WHERE run_id=? AND is_control=0
            """,
            (run_id,),
        ).fetchall()
    assert rows
    for r in rows:
        mults = json.loads(r["multipliers"])
        prod = math.prod(float(v) for v in mults.values())
        reconstructed = float(r["base_score"]) * prod
        # Engine rounds Value_Score to 4dp after multipliers; product must
        # match that stored score (incomplete dict is what this catches).
        assert round(reconstructed, 4) == pytest.approx(
            float(r["score"]), rel=1e-9, abs=1e-9,
        )


def test_delta_persisted_and_matches_leverage_leg(tmp_path):
    db, run_id, scored = _log_scored(tmp_path)
    with _db(db) as c:
        rows = c.execute(
            """
            SELECT side, strike, delta, leverage_raw, spot
            FROM flags WHERE run_id=? AND is_control=0 AND delta IS NOT NULL
            """,
            (run_id,),
        ).fetchall()
    assert rows
    scored_ok = scored[scored["Value_Score"].notna()]
    for r in rows:
        match = scored_ok[
            (scored_ok["side"].astype(str).str.upper() == r["side"])
            & (scored_ok["strike"] == r["strike"])
        ]
        assert not match.empty
        eng = match.iloc[0]
        assert float(r["delta"]) == pytest.approx(float(eng["delta"]), rel=1e-9)
        # leverage_raw == abs(delta) * spot / last (engine-v1.1)
        last = float(eng["last"])
        expected_lev = abs(float(eng["delta"])) * float(r["spot"]) / last
        assert float(r["leverage_raw"]) == pytest.approx(expected_lev, rel=1e-6)


def test_null_delta_row_stores_null_not_zero(tmp_path):
    """Null delta must persist as SQL NULL, never coerced to 0."""
    chain = _sample_chain()
    # Force one row's IV to NaN so bs_delta returns None
    chain.loc[0, "iv"] = float("nan")
    scored = calculate_best_value(
        chain, spot_price=250.0, now_et=NOW, optimal_strategy="(+1) BULL",
    )
    # Manually ensure a scored-looking row with null delta reaches log_run
    # by injecting a row the filter already scored... instead assert engine
    # excludes null-delta from Value_Score, and if we pass one through
    # log_run stores NULL.
    null_row = scored.iloc[0].copy()
    null_row["delta"] = float("nan")
    null_row["_lev"] = float("nan")
    null_row["_nlev"] = float("nan")
    null_row["_base_score"] = 0.5
    null_row["_nflow"] = 0.5
    null_row["_flow"] = 1.0
    null_row["Value_Score"] = 0.5
    null_row["_multipliers"] = {"_base": 1.0}
    df = pd.DataFrame([null_row])
    db = str(tmp_path / "null_delta.db")
    run_id = log_run(
        ticker="TEST", scored_df=df, cfg=SCORING, spot=250.0,
        db_path=db, ts_et=NOW,
    )
    with _db(db) as c:
        row = c.execute(
            "SELECT delta, leverage_raw FROM flags WHERE run_id=? AND is_control=0",
            (run_id,),
        ).fetchone()
    assert row is not None
    assert row["delta"] is None
    assert row["leverage_raw"] is None


def test_legs_stored_pre_and_post_normalisation(tmp_path):
    db, run_id, scored = _log_scored(tmp_path)
    with _db(db) as c:
        rows = c.execute(
            """
            SELECT leverage_raw, flow_raw, leverage_norm, flow_norm,
                   nlev, nflow, base_score
            FROM flags WHERE run_id=? AND is_control=0
            """,
            (run_id,),
        ).fetchall()
    assert rows
    for r in rows:
        assert r["leverage_raw"] is not None
        assert r["flow_raw"] is not None
        assert r["leverage_norm"] is not None
        assert r["flow_norm"] is not None
        assert r["leverage_norm"] == pytest.approx(r["nlev"], abs=1e-12)
        assert r["flow_norm"] == pytest.approx(r["nflow"], abs=1e-12)
        # Norm legs live in [0, 1]
        assert 0.0 <= float(r["leverage_norm"]) <= 1.0
        assert 0.0 <= float(r["flow_norm"]) <= 1.0
        # Raw and norm are different scales (unless degenerate singleton)
        w_lev = float(SCORING["w_lev"])
        w_flow = float(SCORING["w_flow"])
        expected_base = float(r["leverage_norm"]) * w_lev + float(r["flow_norm"]) * w_flow
        assert float(r["base_score"]) == pytest.approx(expected_base, rel=1e-9)


def test_optimal_strategy_and_outlook_persisted_on_runs(tmp_path):
    db, run_id, _ = _log_scored(
        tmp_path,
    )
    # Re-score with explicit strategy
    chain = _sample_chain()
    scored = calculate_best_value(
        chain, spot_price=250.0, now_et=NOW,
        optimal_strategy="(-2) BEARISH",
    )
    db2 = str(tmp_path / "strat.db")
    rid = log_run(
        ticker="TEST", scored_df=scored, cfg=SCORING, spot=250.0,
        db_path=db2, ts_et=NOW,
    )
    with _db(db2) as c:
        row = c.execute(
            "SELECT optimal_strategy, strategy_outlook FROM runs WHERE run_id=?",
            (rid,),
        ).fetchone()
    assert row["optimal_strategy"] == "(-2) BEARISH"
    assert row["strategy_outlook"] == -2

    # STRADDLE / UNKNOWN → NULL outlook
    scored_u = calculate_best_value(
        chain, spot_price=250.0, now_et=NOW,
        optimal_strategy="—",
    )
    # Force unknown label if engine rewrote it
    if scored_u["Value_Score"].notna().any():
        scored_u = scored_u.copy()
        scored_u.loc[scored_u["Value_Score"].notna(), "Optimal_Strategy"] = "STRADDLE"
        # Use a label strategy_outlook returns None for
        from strategy_engine import STRAT_STRADDLE
        scored_u.loc[scored_u["Value_Score"].notna(), "Optimal_Strategy"] = STRAT_STRADDLE
        rid3 = log_run(
            ticker="TEST", scored_df=scored_u, cfg=SCORING, spot=250.0,
            db_path=db2, ts_et=NOW,
        )
        with _db(db2) as c:
            row3 = c.execute(
                "SELECT strategy_outlook FROM runs WHERE run_id=?",
                (rid3,),
            ).fetchone()
        assert row3["strategy_outlook"] is None


def test_migration_preserves_existing_rows(tmp_path):
    db = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            ts_et TEXT NOT NULL,
            ticker TEXT NOT NULL,
            n_scored INTEGER NOT NULL,
            config_hash TEXT NOT NULL,
            engine_sha TEXT,
            daily_bias TEXT,
            market_state TEXT,
            news_bias TEXT,
            spot REAL,
            vwap_state TEXT,
            notes TEXT
        );
        CREATE TABLE flags (
            flag_id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            ts_et TEXT NOT NULL,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            strike REAL NOT NULL,
            expiry TEXT NOT NULL,
            score REAL,
            rank INTEGER,
            multipliers TEXT NOT NULL DEFAULT '{}',
            mid REAL,
            bid REAL,
            ask REAL,
            spot REAL,
            is_control INTEGER NOT NULL DEFAULT 0,
            mark_t1h REAL,
            mark_t1d REAL,
            mark_expiry REAL,
            notes TEXT,
            CHECK (is_control IN (0, 1))
        );
        INSERT INTO runs (run_id, ts_et, ticker, n_scored, config_hash)
        VALUES ('legacy-run', '2026-07-01T10:00:00-04:00', 'AAPL', 1, 'deadbeef');
        INSERT INTO flags (
            run_id, ts_et, ticker, side, strike, expiry, score, rank, multipliers, is_control
        ) VALUES (
            'legacy-run', '2026-07-01T10:00:00-04:00', 'AAPL', 'CALL', 200.0,
            '2026-07-18', 0.42, 1, '{}', 0
        );
        """
    )
    conn.commit()
    before = conn.execute("SELECT COUNT(*) FROM flags").fetchone()[0]
    conn.close()

    with _db(db) as c:
        _ensure_schema(c)
        after = c.execute("SELECT COUNT(*) FROM flags").fetchone()[0]
        row = c.execute(
            "SELECT score, delta, leverage_raw, iv_premium FROM flags WHERE run_id='legacy-run'"
        ).fetchone()
        cols = {r[1] for r in c.execute("PRAGMA table_info(flags)")}
        run_cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}

    assert before == 1
    assert after == 1
    assert row["score"] == 0.42
    assert row["delta"] is None
    assert row["leverage_raw"] is None
    assert row["iv_premium"] is None
    for col in (
        "delta", "leverage_raw", "flow_raw", "leverage_norm", "flow_norm",
        "extrinsic", "realized_vol_20d", "iv_premium", "base_score", "multipliers",
    ):
        assert col in cols
    assert "optimal_strategy" in run_cols
    assert "strategy_outlook" in run_cols


def test_extrinsic_computed_correctly_for_calls_and_puts():
    spot = 250.0
    chain = pd.DataFrame([
        {
            "side": "CALL", "strike": 240.0, "expiry": "2026-08-21", "dte": 32,
            "last": 12.0, "bid": 11.0, "ask": 13.0,
            "volume": 800, "openInterest": 1000, "iv": 0.35,
        },
        {
            "side": "PUT", "strike": 260.0, "expiry": "2026-08-21", "dte": 32,
            "last": 12.0, "bid": 11.0, "ask": 13.0,
            "volume": 800, "openInterest": 1000, "iv": 0.35,
        },
    ])
    scored = calculate_best_value(
        chain, spot_price=spot, now_et=NOW, optimal_strategy="(+1) BULL",
    )
    call = scored[scored["side"] == "CALL"].iloc[0]
    put = scored[scored["side"] == "PUT"].iloc[0]
    # mid = (11+13)/2 = 12
    assert float(call["extrinsic"]) == pytest.approx(12.0 - max(spot - 240.0, 0), abs=1e-9)
    assert float(put["extrinsic"]) == pytest.approx(12.0 - max(260.0 - spot, 0), abs=1e-9)


# ── Part 2 ───────────────────────────────────────────────────────────────────

def test_realized_vol_annualised_with_sqrt_252():
    # Varying daily moves so stdev is non-trivial (constant growth → ~0 stdev)
    rets_pct = [
        0.012, -0.008, 0.015, -0.011, 0.009, -0.004, 0.018, -0.013,
        0.006, -0.009, 0.014, -0.007, 0.011, -0.015, 0.008, -0.005,
        0.016, -0.010, 0.007, -0.012,
    ]
    closes = [100.0]
    for r in rets_pct:
        closes.append(closes[-1] * (1.0 + r))
    assert len(closes) == 21
    rv = realized_vol(closes, window=20)
    assert rv is not None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, 21)]
    from statistics import stdev
    expected = stdev(rets) * math.sqrt(252)
    assert rv == pytest.approx(expected, rel=1e-12)
    # Must annualise with sqrt(252), not leave as daily sigma
    assert rv == pytest.approx(stdev(rets) * math.sqrt(252), abs=1e-15)
    assert abs(rv - stdev(rets)) / rv > 0.5  # annualised is clearly larger


def test_realized_vol_none_when_insufficient_closes():
    assert realized_vol([100.0] * 20, window=20) is None  # need 21
    assert realized_vol([100.0] * 21, window=20) is not None
    assert realized_vol([100.0, 0.0] + [100.0] * 20, window=20) is None
    assert realized_vol([-1.0] + [100.0] * 20, window=20) is None


def test_iv_premium_none_when_rv_near_zero():
    assert iv_premium(0.3, 0.01) is None
    assert iv_premium(0.3, 0.009) is None
    assert iv_premium(0.3, None) is None
    assert iv_premium(None, 0.2) is None
    assert iv_premium(0.4, 0.2) == pytest.approx(2.0)


def test_iv_premium_not_used_in_scoring():
    """AST-assert iv_premium / realized_vol never appear in scoring modules."""
    root = Path(__file__).resolve().parents[1]
    for rel in ("best_value.py", "config.py"):
        src = (root / rel).read_text(encoding="utf-8")
        tree = ast.parse(src)
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "iv_premium" in node.value or "realized_vol" in node.value:
                    pytest.fail(f"{rel} string literal mentions instrumentation field")
        assert "iv_premium" not in names, f"{rel} references iv_premium"
        assert "realized_vol" not in names, f"{rel} references realized_vol"
        assert "realized_vol_20d" not in names, f"{rel} references realized_vol_20d"


def _varying_closes(n: int = 30) -> list[float]:
    rets = [
        0.012, -0.008, 0.015, -0.011, 0.009, -0.004, 0.018, -0.013,
        0.006, -0.009, 0.014, -0.007, 0.011, -0.015, 0.008, -0.005,
        0.016, -0.010, 0.007, -0.012, 0.013, -0.006, 0.010, -0.014,
        0.005, -0.008, 0.017, -0.009, 0.004,
    ]
    closes = [100.0]
    for r in rets[: n - 1]:
        closes.append(closes[-1] * (1.0 + r))
    return closes


def test_iv_premium_persisted_from_daily_closes(tmp_path):
    closes = _varying_closes(30)
    db, run_id, _ = _log_scored(tmp_path, daily_closes=closes)
    with _db(db) as c:
        row = c.execute(
            """
            SELECT realized_vol_20d, iv_premium, iv
            FROM flags WHERE run_id=? AND is_control=0 LIMIT 1
            """,
            (run_id,),
        ).fetchone()
    assert row["realized_vol_20d"] is not None
    assert float(row["realized_vol_20d"]) > 0.01
    assert row["iv_premium"] == pytest.approx(
        float(row["iv"]) / float(row["realized_vol_20d"]), rel=1e-9,
    )
