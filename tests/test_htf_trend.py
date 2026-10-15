"""The 1-hour and 4-hour trend line in the Game Plan (context only)."""

import numpy as np
import pandas as pd

import game_plan as gp
import htf_trend as ht


def _bars(closes, wiggle=0.6):
    """Hourly bars from a close path, with a zig-zag so swings form."""
    closes = np.asarray(closes, dtype=float)
    zz = np.where(np.arange(len(closes)) % 6 < 3, wiggle, -wiggle)
    c = closes + zz
    idx = pd.date_range("2026-09-01 09:30", periods=len(c), freq="h")
    return pd.DataFrame({"Open": c, "High": c + 0.4, "Low": c - 0.4, "Close": c,
                         "Volume": 1e6}, index=idx)


def test_rising_bars_are_up_and_falling_bars_are_down():
    up = ht.describe(_bars(np.linspace(300, 345, 120)))
    assert up["trend"] == ht.UP
    assert up["ema21"] > up["ema50"] and up["swing_low"] > up["prev_swing_low"]
    down = ht.describe(_bars(np.linspace(345, 300, 120)))
    assert down["trend"] == ht.DOWN
    assert down["swing_high"] < down["prev_swing_high"]


def test_flat_bars_are_a_range():
    assert ht.describe(_bars(np.full(120, 340.0)))["trend"] == ht.RANGE


def test_ema_and_swings_disagreeing_is_a_range():
    # EMA 21 still above EMA 50 after a long rise, but the last swing low is lower.
    path = list(np.linspace(300, 345, 100)) + list(np.linspace(345, 341, 12))
    d = ht.describe(_bars(path))
    assert d["ema21"] > d["ema50"] and d["swing_low"] < d["prev_swing_low"]
    assert d["trend"] == ht.RANGE


def test_too_few_bars_is_unknown_never_a_guess():
    d = ht.describe(_bars(np.linspace(300, 345, 30)))
    assert d["trend"] == ht.UNKNOWN and d["ema21"] is None
    assert ht.describe(None)["trend"] == ht.UNKNOWN


def test_swing_points_need_bars_on_both_sides():
    df = _bars([10, 9, 8, 9, 10, 9, 8, 7], wiggle=0)
    lows, highs = ht.swing_points(df)
    assert lows == [7.6]          # index 2; index 7 is newer but has no bars after it
    assert highs == [10.4]        # index 4


def test_from_archive_reads_saved_scans_and_old_scans_are_unknown():
    up = ht.describe(_bars(np.linspace(300, 345, 120)))
    payload = {"timeframes": {"1H": up, "4H": {"price": 340.0}}}
    assert ht.from_archive(payload) == {"1H": ht.UP, "4H": ht.UNKNOWN}
    assert ht.from_archive({}) == {"1H": ht.UNKNOWN, "4H": ht.UNKNOWN}


def test_plan_line_with_against_and_ranging():
    both_up = {"1H": ht.UP, "4H": ht.UP}
    assert ht.plan_line("CALLS", both_up) == (
        "Higher timeframes: 1H up · 4H up. The calls plan runs with them.")
    assert "runs against the higher trend" in ht.plan_line("PUTS", both_up)
    assert "pullback trade, take target 1" in ht.plan_line("PUTS", both_up)
    assert ht.plan_line("CALLS", {"1H": ht.RANGE, "4H": ht.UP}).endswith(
        "expect the range edges to hold.")
    assert ht.plan_line("STAND_ASIDE", both_up) == "Higher timeframes: 1H up · 4H up."
    assert ht.plan_line("CALLS", {}) == "Higher timeframes: 1H unknown · 4H unknown."


def test_game_plan_carries_the_line_and_it_changes_nothing_else():
    from tests.test_game_plan import _plan

    plain = _plan()
    with_htf = _plan(htf={"1H": ht.DOWN, "4H": ht.UP})
    assert plain.htf_note == ""
    assert with_htf.htf_note.startswith("Higher timeframes: 1H down · 4H up.")
    assert "runs against the higher trend" in with_htf.htf_note
    assert with_htf.side == plain.side and with_htf.trade == plain.trade
    assert with_htf.htf_note in gp.plan_lines(with_htf)


def test_scanner_saves_the_trend_for_1h_and_4h_only():
    import dailyScaner

    frames = {"15M": _bars(np.linspace(300, 345, 120)),
              "1H": _bars(np.linspace(300, 345, 120)),
              "4H": _bars(np.linspace(345, 300, 120))}
    out = dailyScaner.analyze_tf(frames)
    assert out["1H"]["trend"] == ht.UP and out["4H"]["trend"] == ht.DOWN
    assert "trend" not in out["15M"]
