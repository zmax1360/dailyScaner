"""The archive's Best Value snapshot: top rows of each pool, with the fields readers
need and the multipliers behind each score (F-S4-01b, piece 2a)."""

import math

import pandas as pd

from notify_delivery import serialize_best_value_rows


def _df(n0, n1):
    rows = []
    for i in range(n0):
        rows.append({"pool": "0DTE", "side": "CALL", "strike": 700 + i, "expiry": "2026-10-16",
                     "dte": 0, "last": 1.0, "Value_Score": 0.9 - i / 1000})
    for i in range(n1):
        rows.append({"pool": "1DTE+", "side": "PUT", "strike": 600 + i, "expiry": "2026-10-23",
                     "dte": 7, "last": 2.0, "Value_Score": 0.5 - i / 1000})
    return pd.DataFrame(rows)


def test_a_crowded_0dte_pool_cannot_push_out_1dte_rows():
    snap = serialize_best_value_rows(_df(45, 12), top_n=30)
    pools = [r["pool"] for r in snap["rows"]]
    assert pools.count("0DTE") == 30 and pools.count("1DTE+") == 12
    assert snap["n_scored"] == 57


def test_rows_in_each_pool_are_best_first():
    snap = serialize_best_value_rows(_df(3, 3))
    for pool in ("0DTE", "1DTE+"):
        scores = [r["Value_Score"] for r in snap["rows"] if r["pool"] == pool]
        assert scores == sorted(scores, reverse=True)


def test_rows_carry_the_fields_readers_need_and_the_multipliers():
    df = pd.DataFrame([{
        "pool": "1DTE+", "side": "CALL", "strike": 350.0, "expiry": "2026-10-19", "dte": 4,
        "last": 0.57, "volume": 1200, "openInterest": 300, "dVol": 400, "Value_Score": 0.34,
        "Status": "⭐ BEST VALUE", "delta": 0.21, "iv": 0.24, "bid": 0.55, "ask": 0.59,
        "extrinsic": 0.57, "Strategy_Tag": "TREND", "Optimal_Strategy": "LONG CALL",
        "_base_score": 0.30, "_multipliers": {"news_with": 1.2, "macro_against": float("nan")},
    }])
    row = serialize_best_value_rows(df)["rows"][0]
    assert (row["delta"], row["iv"], row["bid"], row["ask"], row["extrinsic"]) == \
        (0.21, 0.24, 0.55, 0.59, 0.57)
    assert row["Strategy_Tag"] == "TREND" and row["Optimal_Strategy"] == "LONG CALL"
    assert row["_base_score"] == 0.30
    assert row["multipliers"] == {"news_with": 1.2}            # NaN factor dropped


def test_missing_new_fields_are_none_not_invented():
    row = serialize_best_value_rows(_df(0, 1))["rows"][0]
    assert row["delta"] is None and row["iv"] is None and row["bid"] is None
    assert row["multipliers"] == {} and row["Strategy_Tag"] == ""
