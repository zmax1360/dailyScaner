"""TradingView-style price chart (TradingView Lightweight Charts, Apache-2.0) drawn from
this app's own bars, with the scan's levels as price lines.

``payload`` is pure (bars + levels -> plain lists, testable without a browser);
``html`` wraps it in a page that loads the library from unpkg at a pinned version.
Display only. Times are shown as New York wall-clock time.
"""

from __future__ import annotations

import json
import math
from typing import Any

import pandas as pd

LIB_URL = "https://unpkg.com/lightweight-charts@4.2.0/dist/lightweight-charts.standalone.production.js"
UP, DOWN = "#26a69a", "#ef5350"
EMA_COLORS = {"EMA9": "#7CFC00", "EMA21": "#2196F3", "EMA50": "#F44336"}
VWAP_COLOR = "#00E5FF"
ET = "America/New_York"


def _wall_seconds(index: pd.Index) -> list[int]:
    """Bar times as New York wall-clock time encoded as UTC seconds, which is how the
    library shows a local time. A naive index is already exchange time (see
    ``chart_indicators.last_session``)."""
    idx = pd.DatetimeIndex(index)
    if idx.tz is not None:
        idx = idx.tz_convert(ET).tz_localize(None)
    return [int(t.value // 1_000_000_000) for t in idx]


def _ok(v: Any) -> bool:
    try:
        return v is not None and math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


def payload(df: pd.DataFrame, *, levels: list[dict] | None = None,
            show_volume: bool = True) -> dict:
    """Series for the chart. Rows without a full OHLC are left out; an indicator is drawn
    only where it has a value (missing is a gap, never 0)."""
    if df is None or df.empty:
        return {"candles": [], "volume": [], "lines": [], "levels": []}
    times = _wall_seconds(df.index)
    candles, volume = [], []
    for t, (_, r) in zip(times, df.iterrows()):
        if not all(_ok(r.get(c)) for c in ("Open", "High", "Low", "Close")):
            continue
        candles.append({"time": t, "open": float(r["Open"]), "high": float(r["High"]),
                        "low": float(r["Low"]), "close": float(r["Close"])})
        if show_volume and _ok(r.get("Volume")):
            volume.append({"time": t, "value": float(r["Volume"]),
                           "color": (UP if r["Close"] >= r["Open"] else DOWN) + "80"})
    lines = []
    for name, color, dashed in [*[(n, c, False) for n, c in EMA_COLORS.items()],
                                ("VWAP", VWAP_COLOR, True)]:
        if name not in df.columns:
            continue
        pts = [{"time": t, "value": float(v)} for t, v in zip(times, df[name]) if _ok(v)]
        if pts:
            lines.append({"name": name.replace("EMA", "EMA "), "color": color,
                          "dashed": dashed, "data": pts})
    lv = [{"title": str(x.get("name", "")), "price": float(x["price"]),
           "color": str(x.get("color", "#9e9e9e"))}
          for x in (levels or []) if _ok(x.get("price"))]
    return {"candles": candles, "volume": volume, "lines": lines, "levels": lv}


def html(data: dict, *, height: int = 560) -> str:
    """A self-contained page drawing ``data`` (from ``payload``)."""
    blob = json.dumps(data).replace("</", "<\\/")
    return f"""<!doctype html><html><head><meta charset="utf-8">
<style>html,body{{margin:0;background:transparent}}#c{{width:100%;height:{height}px}}
#err{{color:#b0bec5;font:13px sans-serif;padding:8px}}</style></head><body>
<div id="c"></div><div id="err"></div>
<script src="{LIB_URL}"></script>
<script>
(function() {{
  const d = {blob};
  if (!window.LightweightCharts) {{
    document.getElementById('err').textContent =
      'The TradingView chart library could not be loaded (no internet?). Switch the chart style back to Plotly.';
    return;
  }}
  const el = document.getElementById('c');
  const chart = LightweightCharts.createChart(el, {{
    height: {height}, autoSize: true,
    localization: {{ locale: 'en-US' }},
    layout: {{ background: {{ type: 'solid', color: 'transparent' }}, textColor: '#b0bec5' }},
    grid: {{ vertLines: {{ color: 'rgba(255,255,255,0.05)' }}, horzLines: {{ color: 'rgba(255,255,255,0.05)' }} }},
    rightPriceScale: {{ borderColor: 'rgba(255,255,255,0.15)', scaleMargins: {{ top: 0.06, bottom: 0.22 }} }},
    timeScale: {{ borderColor: 'rgba(255,255,255,0.15)', timeVisible: true, secondsVisible: false }},
    crosshair: {{ mode: LightweightCharts.CrosshairMode.Normal }},
  }});
  const candles = chart.addCandlestickSeries({{
    upColor: '{UP}', downColor: '{DOWN}', borderVisible: false,
    wickUpColor: '{UP}', wickDownColor: '{DOWN}',
  }});
  candles.setData(d.candles);
  if (d.volume.length) {{
    const vol = chart.addHistogramSeries({{ priceFormat: {{ type: 'volume' }}, priceScaleId: '' }});
    vol.priceScale().applyOptions({{ scaleMargins: {{ top: 0.82, bottom: 0 }} }});
    vol.setData(d.volume);
  }}
  for (const l of d.lines) {{
    const s = chart.addLineSeries({{ color: l.color, lineWidth: 1.5,
      lineStyle: l.dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
      priceLineVisible: false, lastValueVisible: false, title: l.name }});
    s.setData(l.data);
  }}
  for (const lv of d.levels) {{
    candles.createPriceLine({{ price: lv.price, color: lv.color, lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed, axisLabelVisible: true, title: lv.title }});
  }}
  chart.timeScale().fitContent();
}})();
</script></body></html>"""
