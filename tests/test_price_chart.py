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


def test_last_session_keeps_only_the_latest_day():
    df = _bars()
    last = ci.last_session(df)
    assert set(last.index.date) == {pd.Timestamp("2026-10-02").date()}
    assert len(last) == 78
    assert ci.last_session(pd.DataFrame()).empty


def test_last_session_accepts_a_naive_index_as_exchange_wall_clock():
    """Regression: the Yahoo source strips the timezone, and the chart crashed on it."""
    naive = _bars().tz_localize(None)
    last = ci.last_session(naive)
    assert len(last) == 78
    assert set(last.index.date) == {pd.Timestamp("2026-10-02").date()}
    assert last.index[0] == pd.Timestamp("2026-10-02 09:30")


def test_last_session_converts_an_aware_index_before_taking_the_date():
    utc = _bars().tz_convert("UTC")                      # same instants, UTC wall clock
    assert len(ci.last_session(utc)) == 78


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


def test_legend_lists_only_the_price_overlays():
    fig = pc.build_figure(pc.prepare(_bars(), "5M"), show_atr=True)
    listed = [t.name for t in fig.data if t.showlegend is not False]
    assert listed == ["EMA 9", "EMA 21", "EMA 50", "VWAP"]


def test_participation_axis_has_no_tick_labels_to_collide_with_volume():
    fig = pc.build_figure(pc.prepare(_bars(), "5M"))
    hidden = [ax for ax in fig.select_yaxes() if ax.showticklabels is False]
    assert len(hidden) == 1


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
pc.render(ScanContext(ticker="AAPL", curr={"spot": 333.0}))
"""


def _run(monkeypatch, bars, summary=None):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(pc, "_cached_bars", lambda ticker, timeframe: bars)
    # never the real snapshot database
    monkeypatch.setattr(pc, "gamma_summary_for", lambda ticker, spot: summary)
    at = AppTest.from_string(_SCRIPT, default_timeout=30).run()
    assert not at.exception, at.exception
    return at


def test_component_renders_with_a_last_bar_read_out(monkeypatch):
    at = _run(monkeypatch, _bars())
    cap = next(c.value for c in at.caption if c.value.startswith("Last bar:"))
    for want in ("EMA 9", "EMA 21", "EMA 50", "VWAP", "Stoch", "ATR"):
        assert want in cap
    assert {c.key for c in at.checkbox} == {"chart_stoch", "chart_atr", "chart_participation",
                                            "chart_gamma"}
    assert not any(c.value.startswith("Gamma levels:") for c in at.caption)   # none available


def test_component_renders_with_timezone_naive_bars(monkeypatch):
    """Regression: real bars from the Yahoo source have no timezone."""
    at = _run(monkeypatch, _bars().tz_localize(None))
    assert any(c.value.startswith("Last bar:") for c in at.caption)


def test_prepare_handles_timezone_naive_bars_on_every_timeframe():
    from volume_analysis import CHART_TIMEFRAMES

    naive = _bars().tz_localize(None)
    for tf in CHART_TIMEFRAMES:
        shown = pc.prepare(naive, tf)
        assert not shown.empty, tf
        assert len(pc.build_figure(shown, timeframe=tf).data) > 0, tf


def test_component_reports_unavailable_data(monkeypatch):
    at = _run(monkeypatch, pd.DataFrame())
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


# ── end to end through the real fetch function, with a fake data source ─────

class _NaiveSource:
    """Stands in for the Yahoo source: OHLCV bars with the timezone stripped."""
    name = "fake"

    def fetch_history(self, ticker, *, interval, period):
        if interval == "5m":
            sessions = ("2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02")
            return _bars(sessions=sessions).drop(columns=["VWAP"]).tz_localize(None)
        if interval == "1h":
            days = pd.bdate_range("2026-09-01", "2026-10-02")
            idx = pd.DatetimeIndex([d + pd.Timedelta(hours=h, minutes=30)
                                    for d in days for h in range(9, 16)])
        else:
            idx = pd.bdate_range("2026-04-01", "2026-10-02")
        rng = np.random.default_rng(3)
        close = 330 + np.cumsum(rng.normal(0, 0.6, len(idx)))
        return pd.DataFrame({"Open": close - 0.2, "High": close + 0.8, "Low": close - 0.9,
                             "Close": close, "Volume": rng.integers(1e5, 9e5, len(idx))},
                            index=idx)


@pytest.mark.parametrize("timeframe", ["5M", "10M", "15M", "45M", "1H", "4H", "1D"])
def test_every_timeframe_draws_from_source_shaped_bars(timeframe):
    from volume_analysis import fetch_intraday_vwap_df

    bars = fetch_intraday_vwap_df("AAPL", last_session_only=False, timeframe=timeframe,
                                  source=_NaiveSource())
    assert not bars.empty and bars.index.tz is None
    shown = pc.prepare(bars, timeframe)
    assert not shown.empty
    names = _names(pc.build_figure(shown, ticker="AAPL", timeframe=timeframe, show_atr=True))
    assert {"Price", "EMA 9", "VWAP", "Volume", "Stoch %K", "ATR 14"} <= set(names)
    if timeframe in pc.SESSION_TIMEFRAMES:
        assert len(set(shown.index.date)) == 1


def test_fifteen_minute_view_has_a_full_session_with_all_three_emas():
    from volume_analysis import fetch_intraday_vwap_df

    bars = fetch_intraday_vwap_df("AAPL", last_session_only=False, timeframe="15M",
                                  source=_NaiveSource())
    shown = pc.prepare(bars, "15M")
    assert len(shown) == 26                                   # 09:30 .. 15:45
    assert shown[["EMA9", "EMA21", "EMA50"]].notna().all().all()


# ── gamma levels on the chart ───────────────────────────────────────────────

_SUMMARY = {"support": 330.0, "resistance": 335.0, "put_wall": 320.0, "call_wall": 330.0}


def test_gamma_levels_are_support_resistance_and_put_wall():
    from ui.gamma_data import gamma_levels

    levels = gamma_levels(_SUMMARY)
    assert [(lv["key"], lv["price"]) for lv in levels] == [
        ("resistance", 335.0), ("support", 330.0), ("put_wall", 320.0)]
    assert gamma_levels(None) == [] and gamma_levels({}) == []
    assert [lv["key"] for lv in gamma_levels({**_SUMMARY, "support": None})] == [
        "resistance", "put_wall"]


def _levels_for(df, *, near_below=0.2, near_above=0.2, far_below=3.0):
    """Levels placed relative to the candle range: two close by, one far below."""
    lo, hi = float(df["Low"].min()), float(df["High"].max())
    span = hi - lo
    return lo, hi, [
        {"key": "resistance", "name": "Call resistance", "price": round(hi + near_above * span, 2),
         "color": "#C084FC"},
        {"key": "support", "name": "Gamma support", "price": round(lo - near_below * span, 2),
         "color": "#FACC15"},
        {"key": "put_wall", "name": "Put wall", "price": round(lo - far_below * span, 2),
         "color": "#2DD4BF"},
    ]


def _price_lines(fig):
    return [s for s in fig.layout.shapes if s.type == "line" and s.yref == "y"]


def test_a_far_level_does_not_squash_the_candles():
    """Regression: a put wall far below the candles stretched the price axis to reach it."""
    df = pc.prepare(_bars(), "5M")
    lo, hi, levels = _levels_for(df)
    fig = pc.build_figure(df, levels=levels)
    y0, y1 = fig.layout.yaxis.range
    far = levels[2]["price"]
    assert y0 > far                                         # the axis does not reach it
    assert (y1 - y0) < 2.0 * (hi - lo)                      # candles keep most of the panel
    assert far not in [s.y0 for s in _price_lines(fig)]     # and no line is drawn for it
    label = next(a for a in fig.layout.annotations if a.text.startswith("Put wall"))
    assert label.text.endswith("↓") and label.y == pytest.approx(y0)


def test_near_levels_get_lines_and_stay_inside_the_axis():
    df = pc.prepare(_bars(), "5M")
    lo, hi, levels = _levels_for(df)
    fig = pc.build_figure(df, levels=levels)
    y0, y1 = fig.layout.yaxis.range
    drawn = sorted(s.y0 for s in _price_lines(fig))
    assert drawn == sorted([levels[0]["price"], levels[1]["price"]])
    assert all(y0 < y < y1 for y in drawn)
    labels = [a.text for a in fig.layout.annotations]
    assert any(t.startswith("Call resistance") for t in labels)
    assert any(t.startswith("Gamma support") for t in labels)


def test_far_levels_above_point_up_and_stack_without_overlapping():
    df = pc.prepare(_bars(), "5M")
    lo, hi, _ = _levels_for(df)
    span = hi - lo
    levels = [{"key": "a", "name": "A", "price": hi + 3 * span, "color": "#fff"},
              {"key": "b", "name": "B", "price": hi + 5 * span, "color": "#fff"}]
    fig = pc.build_figure(df, levels=levels)
    tags = [a for a in fig.layout.annotations if a.text.endswith("↑")]
    assert len(tags) == 2 and len({a.yshift for a in tags}) == 2
    assert not _price_lines(fig)


def test_without_levels_the_price_axis_scales_itself():
    df = pc.prepare(_bars(), "5M")
    fig = pc.build_figure(df)
    assert fig.layout.yaxis.range is None and not _price_lines(fig)
    assert len(pc.build_figure(df, levels=_levels_for(df)[2]).data) == len(fig.data)


def test_component_lists_gamma_levels_and_the_toggle_removes_them(monkeypatch):
    summary = {**_SUMMARY, "expiry": "2026-10-05"}
    at = _run(monkeypatch, _bars(), summary=summary)
    cap = next(c.value for c in at.caption if c.value.startswith("Gamma levels:"))
    assert r"Call resistance \$335" in cap and r"Put wall \$320" in cap
    at.checkbox(key="chart_gamma").set_value(False).run()
    assert not at.exception
    assert not any(c.value.startswith("Gamma levels:") for c in at.caption)
