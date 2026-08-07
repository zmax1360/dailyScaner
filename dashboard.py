#!/usr/bin/env python3
"""
dashboard.py — read-only visual review of AAPL scanner archives.

Design rule: this file computes NOTHING. It renders what dailyScaner.py
already decided and saved. If a value isn't in the archive JSON, it is
not shown. The scanner stays the single source of truth.

Usage:
    python dashboard.py                  # today
    python dashboard.py 2026-07-16       # a specific day
    python dashboard.py 2026-07-16 --open  # also open in browser

Reads:   archive/{TICKER}_{YYYYMMDD}_*.json
Writes:  dashboard_{YYYYMMDD}.html  (self-contained, no server needed)

Optional: if yfinance is installed and the network is up, a 5-minute
candlestick of the underlying is drawn behind the scanner data. If the
fetch fails, the dashboard still builds from archives alone.
"""

import glob
import json
import os
import sys
import webbrowser
from datetime import datetime, date

import plotly.graph_objects as go
from plotly.subplots import make_subplots

TICKER = os.environ.get("SCANNER_TICKER", "AAPL")
ARCHIVE_DIR = "archive"
TF_ORDER = ["5M", "10M", "15M", "1H", "4H", "1D"]

DIR_COLOR = {"BULLISH": "#22c55e", "BEARISH": "#ef4444", "NEUTRAL": "#eab308", None: "#9ca3af"}


# ── load ─────────────────────────────────────────────────────────────────────

def load_day(day: date):
    pattern = os.path.join(ARCHIVE_DIR, f"{TICKER}_{day.strftime('%Y%m%d')}_*.json")
    runs = []
    for path in sorted(glob.glob(pattern)):
        try:
            with open(path) as f:
                d = json.load(f)
            d["_ts"] = datetime.fromisoformat(d["timestamp"])
            d["_file"] = os.path.basename(path)
            runs.append(d)
        except Exception as e:
            print(f"  skip {path}: {e}")
    return runs


def fetch_candles(day: date):
    """Optional 5m candles for context. Failure is fine — return None."""
    try:
        import yfinance as yf
        df = yf.Ticker(TICKER).history(period="5d", interval="5m")
        if df.index.tz is not None:
            df.index = df.index.tz_localize(None)
        df = df[df.index.date == day]
        return df if not df.empty else None
    except Exception as e:
        print(f"  candles unavailable ({e.__class__.__name__}) — archive-only mode")
        return None


# ── build ────────────────────────────────────────────────────────────────────

def build(day: date, runs, candles):
    ts      = [r["_ts"] for r in runs]
    spots   = [r.get("spot") for r in runs]
    pcs     = [r.get("volume", {}).get("pc_ratio") for r in runs]
    dirs    = [r.get("direction") for r in runs]

    fig = make_subplots(
        rows=3, cols=1, shared_xaxes=True,
        row_heights=[0.62, 0.18, 0.20], vertical_spacing=0.04,
        specs=[[{"secondary_y": False}], [{}], [{}]],
        subplot_titles=(
            f"{TICKER} — price, opening range, qualified magnets",
            "P/C volume ratio per run",
            "RSI by timeframe per run",
        ),
    )

    # 1) price context ---------------------------------------------------------
    if candles is not None:
        fig.add_trace(go.Candlestick(
            x=candles.index, open=candles["Open"], high=candles["High"],
            low=candles["Low"], close=candles["Close"],
            name="5m", increasing_line_color="#16a34a",
            decreasing_line_color="#dc2626", opacity=0.9,
        ), row=1, col=1)
    # spot dots from archives always drawn (they're the scanner's own record)
    fig.add_trace(go.Scatter(
        x=ts, y=spots, mode="markers+lines", name="scan spot",
        marker=dict(size=9, color=[DIR_COLOR.get(d) for d in dirs],
                    line=dict(width=1, color="#111")),
        line=dict(width=1, dash="dot", color="#6b7280"),
        text=[f"{r['_ts'].strftime('%H:%M')}  dir={r.get('direction')}" for r in runs],
        hovertemplate="%{text}<br>spot $%{y:.2f}<extra></extra>",
    ), row=1, col=1)

    # OR band — from the LAST run of the day that has 15M OR data
    or_src = next((r for r in reversed(runs)
                   if r.get("or_data") and r["or_data"].get("15M")), None)
    if or_src:
        d15 = or_src["or_data"]["15M"]
        x0 = datetime.combine(day, datetime.strptime("09:30", "%H:%M").time())
        x1 = datetime.combine(day, datetime.strptime("16:00", "%H:%M").time())
        for y, lbl in [(d15["high"], f"15M OR high {d15['high']}"),
                       (d15["low"],  f"15M OR low {d15['low']}")]:
            fig.add_shape(type="line", x0=x0, x1=x1, y0=y, y1=y,
                          line=dict(color="#3b82f6", width=1.5, dash="dash"), row=1, col=1)
            fig.add_annotation(x=x1, y=y, text=lbl, showarrow=False,
                               font=dict(size=10, color="#3b82f6"),
                               xanchor="left", row=1, col=1)
        fig.add_shape(type="rect", x0=x0, x1=x1, y0=d15["low"], y1=d15["high"],
                      fillcolor="rgba(59,130,246,0.06)", line_width=0, row=1, col=1)

    # qualified magnet strikes per run (call=green, put=red), drawn at run time
    for side, color, symbol in [("call", "#16a34a", "triangle-up"),
                                ("put", "#dc2626", "triangle-down")]:
        xs, ys, txt = [], [], []
        for r in runs:
            m = (r.get("signal_magnets") or {}).get(side)
            if m:
                xs.append(r["_ts"]); ys.append(m["strike"])
                txt.append(f"{side.upper()} magnet ${m['strike']:.1f} "
                           f"{m['expiry']} ({m['dte']}d) vol {int(m['volume']):,} "
                           f"OI {int(m['openInterest']):,}")
        if xs:
            fig.add_trace(go.Scatter(
                x=xs, y=ys, mode="markers", name=f"{side} magnet",
                marker=dict(size=11, color=color, symbol=symbol,
                            line=dict(width=1, color="#111")),
                text=txt, hovertemplate="%{text}<extra></extra>",
            ), row=1, col=1)

    # 2) P/C ratio -------------------------------------------------------------
    fig.add_trace(go.Scatter(
        x=ts, y=pcs, mode="lines+markers", name="P/C",
        line=dict(color="#8b5cf6", width=2),
        hovertemplate="%{x|%H:%M}  P/C %{y:.2f}<extra></extra>",
    ), row=2, col=1)
    fig.add_hline(y=0.7, line=dict(color="#22c55e", width=1, dash="dot"), row=2, col=1)
    fig.add_hline(y=1.3, line=dict(color="#ef4444", width=1, dash="dot"), row=2, col=1)

    # 3) RSI heatmap ------------------------------------------------------------
    z, hover = [], []
    for tf in TF_ORDER:
        zrow, hrow = [], []
        for r in runs:
            v = r.get("timeframes", {}).get(tf, {}).get("rsi")
            zrow.append(v)
            hrow.append(f"{tf} @ {r['_ts'].strftime('%H:%M')}: RSI {v}")
        z.append(zrow); hover.append(hrow)
    fig.add_trace(go.Heatmap(
        x=ts, y=TF_ORDER, z=z, text=hover, hoverinfo="text",
        colorscale=[[0, "#3b82f6"], [0.5, "#f3f4f6"], [1, "#ef4444"]],
        zmin=20, zmax=90, colorbar=dict(title="RSI", len=0.25, y=0.08),
    ), row=3, col=1)

    n_closed = sum(1 for r in runs if r["_ts"].time() >= datetime.strptime("16:00", "%H:%M").time())
    fig.update_layout(
        title=(f"{TICKER} scanner review — {day.isoformat()}  ·  {len(runs)} runs"
               + (f"  ({n_closed} after close)" if n_closed else "")),
        template="plotly_white", height=900,
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", y=1.06),
        margin=dict(l=60, r=140, t=90, b=40),
    )
    fig.update_yaxes(title_text="price $", row=1, col=1)
    fig.update_yaxes(title_text="P/C", row=2, col=1)
    return fig


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    day = date.fromisoformat(args[0]) if args else date.today()

    runs = load_day(day)
    if not runs:
        sys.exit(f"No archives for {TICKER} on {day} in ./{ARCHIVE_DIR}/ — run the scanner first.")
    print(f"  {len(runs)} runs loaded for {day}")

    candles = fetch_candles(day)
    fig = build(day, runs, candles)

    out = f"dashboard_{day.strftime('%Y%m%d')}.html"
    fig.write_html(out, include_plotlyjs=True)
    print(f"  → {out}")
    if "--open" in sys.argv:
        webbrowser.open("file://" + os.path.abspath(out))


if __name__ == "__main__":
    main()
