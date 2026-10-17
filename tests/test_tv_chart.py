"""TradingView-style chart: the payload (ui/components/tv_chart.py) and the style switch."""

from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from ui.components import tv_chart as tv
from ui.gamma_data import gamma_levels

ET = ZoneInfo("America/New_York")


def _bars(index):
    return pd.DataFrame({
        "Open": [1.0, 2.0, float("nan")], "High": [2.0, 3.0, 3.0], "Low": [0.5, 1.5, 1.5],
        "Close": [1.5, 1.8, 2.0], "Volume": [100.0, float("nan"), 50.0],
        "EMA9": [float("nan"), 1.6, 1.7], "VWAP": [1.4, 1.5, 1.6],
    }, index=index)


def test_times_are_new_york_wall_clock():
    aware = pd.DatetimeIndex([datetime(2026, 10, 16, 9, 30, tzinfo=ET)] * 3)
    naive = pd.DatetimeIndex([datetime(2026, 10, 16, 9, 30)] * 3)
    want = int(datetime(2026, 10, 16, 9, 30, tzinfo=ZoneInfo("UTC")).timestamp())
    assert tv._wall_seconds(aware)[0] == want == tv._wall_seconds(naive)[0]


def test_payload_skips_incomplete_bars_and_gaps_never_zero():
    idx = pd.date_range("2026-10-16 09:30", periods=3, freq="15min", tz=ET)
    p = tv.payload(_bars(idx), levels=[{"name": "Put wall", "price": 330.0, "color": "#2DD4BF"},
                                       {"name": "bad", "price": None}])
    assert len(p["candles"]) == 2                          # the NaN-open bar is left out
    assert [v["value"] for v in p["volume"]] == [100.0]    # NaN volume is not drawn as 0
    ema = next(line for line in p["lines"] if line["name"] == "EMA 9")
    assert len(ema["data"]) == 2 and all(pt["value"] > 0 for pt in ema["data"])
    assert next(line for line in p["lines"] if line["name"] == "VWAP")["dashed"]
    assert p["levels"] == [{"title": "Put wall", "price": 330.0, "color": "#2DD4BF"}]
    json.dumps(p)                                          # plain JSON for the page


def test_empty_bars_and_page_escaping():
    assert tv.payload(pd.DataFrame())["candles"] == []
    page = tv.html({"candles": [], "volume": [], "lines": [],
                    "levels": [{"title": "</script><b>", "price": 1.0, "color": "#fff"}]})
    assert "</script><b>" not in page and tv.LIB_URL in page
    assert "lightweight-charts@4.2.0" in tv.LIB_URL           # pinned, never "latest"


def test_gamma_levels_include_the_flip_when_saved():
    keys = [lv["key"] for lv in gamma_levels({"support": 330.0, "gamma_flip_nearest": 343.38})]
    assert keys == ["support", "gamma_flip_nearest"]
