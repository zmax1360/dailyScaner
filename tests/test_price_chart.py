"""All-in-one price chart: indicator series (chart_indicators.py) and the chart component."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

import chart_indicators as ci
import ema_stack
from ui.components import price_chart as pc

TZ = "America/New_York"


def _bars(sessions=("2026-09-30", "2026-10-01", "2026-10-02"), freq="5min", seed=7):
    """Deterministic 5-minute OHLCV for full regular sessions, timezone-aware."""
    rng = np.random.default_rng(seed)
    frames = []
    price = 330.0
    for day in sessions:
        idx = pd.date_range(f"{day} 09:30", f"{day} 15:55", freq=freq, tz=TZ)
        steps = rng.normal(0, 0.25, len(idx))
        close = price + np.cumsum(steps)
        open_ = np.concatenate([[price], close[:-1]])
        high = np.maximum(open_, close) + rng.uniform(0.02, 0.3, len(idx))
        low = np.minimum(open_, close) - rng.uniform(0.02, 0.3, len(idx))
        vol = rng.integers(50_000, 400_000, len(idx)).astype(float)
        frames.append(pd.DataFrame({"Open": open_, "High": high, "Low": low, "Close": close,
                                    "Volume": vol}, index=idx))
        price = float(close[-1])
    df = pd.concat(frames)
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    day = df.index.date
    df["VWAP"] = (tp * df["Volume"]).groupby(day).cumsum() / df["Volume"].groupby(day).cumsum()
    return df


# ── indicators ──────────────────────────────────────────────────────────────

def test_ema_series_matches_the_trend_banner_definition():
    close = _bars()["Close"]
    want = ema_stack.compute_emas(close)
    for p in ci.EMA_PERIODS:
        assert ci.ema_series(close, p).iloc[-1] == pytest.approx(want[f"ema{p}"], abs=1e-4)


def test_ema_series_is_nan_until_enough_bars_exist():
    close = pd.Series(range(1, 61), dtype=float)
    e = ci.ema_series(close, 50)
    assert e.iloc[:49].isna().all() and e.iloc[49:].notna().all()
    assert ci.ema_series(close.iloc[:10], 50).isna().all()          # never a default


def test_fast_stochastic_known_values():
    df = pd.DataFrame({"High": [10, 11, 12, 13, 14], "Low": [9, 10, 11, 12, 13],
                       "Close": [9.5, 10.5, 11.5, 12.5, 13.0]})
    s = ci.fast_stochastic(df, k=3, d=2)
    assert s["StochK"].iloc[:2].isna().all()
    # bar 2: lowest low 9, highest high 12, close 11.5 -> 100 * 2.5 / 3
    assert s["StochK"].iloc[2] == pytest.approx(100 * 2.5 / 3)
    # bar 4: lowest low 11, highest high 14, close 13 -> 100 * 2 / 3
    assert s["StochK"].iloc[4] == pytest.approx(100 * 2 / 3)
    assert s["StochD"].iloc[3] == pytest.approx(s["StochK"].iloc[2:4].mean())
    assert math.isnan(s["StochD"].iloc[2])


def test_fast_stochastic_stays_between_0_and_100_and_handles_a_flat_range():
    s = ci.fast_stochastic(_bars())
    k = s["StochK"].dropna()
    assert len(k) and k.between(0, 100).all()
    flat = pd.DataFrame({"High": [5.0] * 20, "Low": [5.0] * 20, "Close": [5.0] * 20})
    assert ci.fast_stochastic(flat)["StochK"].isna().all()           # zero range -> NaN


def test_atr_known_values_with_wilder_smoothing():
    df = pd.DataFrame({"High": [10.0, 11.0, 12.5], "Low": [9.0, 9.5, 11.0],
                       "Close": [9.5, 10.5, 12.0]})
    # true ranges: 1.0, max(1.5, 1.5, 0.0)=1.5, max(1.5, 2.0, 0.5)=2.0
    a = ci.atr(df, period=2)
    assert math.isnan(a.iloc[0])
    assert a.iloc[1] == pytest.approx(0.5 * 1.0 + 0.5 * 1.5)
    assert a.iloc[2] == pytest.approx(0.5 * 1.25 + 0.5 * 2.0)


def test_add_indicators_attaches_all_columns_and_rejects_missing_ohlc():
    out = ci.add_indicators(_bars())
    assert {"EMA9", "EMA21", "EMA50", "StochK", "StochD", "ATR"} <= set(out.columns)
    assert out["ATR"].dropna().gt(0).all()
    assert ci.add_indicators(pd.DataFrame()).empty
    assert ci.add_indicators(_bars().drop(columns=["High"])).empty


def test_last_session_keeps_only_the_latest_day_and_needs_a_timezone():
    df = _bars()
    last = ci.last_session(df)
    assert set(last.index.date) == {pd.Timestamp("2026-10-02").date()}
    assert len(last) == 78
    with pytest.raises(ValueError):
        ci.last_session(df.tz_localize(None))
    assert ci.last_session(pd.DataFrame()).empty


# ── preparing and drawing the chart ─────────────────────────────────────────

def test_fifteen_minute_timeframe_exists_and_is_the_default():
    from volume_analysis import _CHART_TF_SPEC, CHART_TIMEFRAMES

    assert "15M" in CHART_TIMEFRAMES and pc.DEFAULT_TIMEFRAME == "15M"
    assert _CHART_TF_SPEC["15M"]["resample"] == "15min"


def test_prepare_warms_up_on_history_then_shows_the_last_session():
    bars = _bars()
    shown = pc.prepare(bars, "5M")
    assert set(shown.index.date) == {pd.Timestamp("2026-10-02").date()}
    assert shown["EMA50"].notna().all()                  # warmed up by earlier sessions
    assert shown["EMA50"].iloc[0] == pytest.approx(ci.ema_series(bars["Close"], 50)[shown.index[0]])
    assert "Participation" in shown.columns
    assert len(pc.prepare(bars, "1H")) == len(bars)      # multi-day timeframes keep it all
    assert pc.prepare(pd.DataFrame(), "5M").empty


def _names(fig):
    return [t.name for t in fig.data]


def test_figure_has_price_emas_vwap_volume_and_stochastic_on_one_axis():
    fig = pc.build_figure(pc.prepare(_bars(), "5M"), ticker="aapl", timeframe="5M")
    names = _names(fig)
    for want in ("Price", "EMA 9", "EMA 21", "EMA 50", "VWAP", "Volume", "Stoch %K", "Stoch %D"):
        assert want in names, want
    assert fig.layout.title.text == "AAPL 5M"
    # price, volume and stochastic panels all follow the bottom panel's time axis
    assert fig.layout.xaxis.matches == "x3" and fig.layout.xaxis2.matches == "x3"
    assert fig.layout.xaxis3.matches is None


def test_indicator_toggles_add_and_remove_panels():
    df = pc.prepare(_bars(), "5M")
    base = pc.build_figure(df, show_stoch=False, show_atr=False, show_participation=False)
    assert "Stoch %K" not in _names(base) and "ATR 14" not in _names(base)
    assert "Participation ×" not in _names(base)
    full = pc.build_figure(df, show_stoch=True, show_atr=True, show_participation=True)
    assert {"Stoch %K", "ATR 14", "Participation ×"} <= set(_names(full))
    assert full.layout.height > base.layout.height


def test_an_ema_without_enough_bars_is_left_off_the_chart():
    one_day = _bars(sessions=("2026-10-02",), freq="15min")           # 26 bars
    fig = pc.build_figure(pc.prepare(one_day, "15M"), timeframe="15M")
    assert "EMA 9" in _names(fig) and "EMA 21" in _names(fig)
    assert "EMA 50" not in _names(fig)


def test_over_participation_bars_are_marked():
    bars = _bars()
    bars.iloc[-5, bars.columns.get_loc("Volume")] *= 40
    fig = pc.build_figure(pc.prepare(bars, "5M"))
    assert any(n.startswith("≥") for n in _names(fig))


def test_session_gaps_are_hidden_only_on_multi_day_timeframes():
    df = pc.prepare(_bars(), "1H")
    assert pc.build_figure(df, timeframe="1H").layout.xaxis.rangebreaks
    assert not pc.build_figure(pc.prepare(_bars(), "5M"), timeframe="5M").layout.xaxis.rangebreaks


def test_empty_data_gives_an_empty_figure_not_an_error():
    assert len(pc.build_figure(pd.DataFrame()).data) == 0


# ── the component in the app ────────────────────────────────────────────────

_SCRIPT = """
import pandas as pd
from tests.test_price_chart import _bars
from ui.components import price_chart as pc
from ui.context import ScanContext
pc._cached_bars = lambda ticker, timeframe: {bars}
pc.render(ScanContext(ticker="AAPL", curr={{"spot": 333.0}}))
"""


def _run(bars="_bars()"):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_SCRIPT.format(bars=bars), default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def test_component_renders_with_a_last_bar_read_out():
    at = _run()
    cap = next(c.value for c in at.caption if c.value.startswith("Last bar:"))
    for want in ("EMA 9", "EMA 21", "EMA 50", "VWAP", "Stoch", "ATR"):
        assert want in cap
    assert {c.key for c in at.checkbox} == {"chart_stoch", "chart_atr", "chart_participation"}


def test_component_reports_unavailable_data():
    at = _run(bars="pd.DataFrame()")
    assert any("chart unavailable" in c.value for c in at.caption)


def test_component_is_a_menu_entry():
    from ui import shell
    from ui.components import REGISTRY

    assert REGISTRY["price_chart"] is pc
    assert {e.id: e.title for e in shell.entries()}["price_chart"] == pc.TITLE


def test_options_flow_uses_the_combined_chart_instead_of_two():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "ui" / "pages" / "flow.py").read_text()
    assert "price_chart.render(" in src
    assert "render_vwap_chart(" not in src and "render_pov_leakage_chart(" not in src
