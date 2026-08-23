#!/usr/bin/env python3
"""
eod_report.py — daily scorecard for the scanner.

Run after the close:
    python eod_report.py                 # today, all tickers
    python eod_report.py --date 2026-08-03 --ticker AAPL
    python eod_report.py --days 15       # rolling window across sessions

Grades the engine against recorded outcomes. Every number comes from
data/attribution.db — nothing is recomputed or estimated here.

Design notes:
  * Rows are CLUSTERED by (date, contract) before averaging. A contract scored
    every 5 minutes is ONE observation, not 78. Un-clustered counts overstate
    the sample by ~50x and make noise look significant.
  * Only marks taken within MAX_HOURS of the flag are used for T+1h / T+1d.
    A mark written the next morning is an overnight move wearing a 1-hour label.
  * 0DTE, 1DTE+, and UNKNOWN (NULL dte, pre-migration) are reported separately.
  * Expiry intrinsic marks have no lag filter — settlement value does not depend
    on when the mark was written (see MAX_HOURS_EXPIRY note below).
  * Both mean and median are shown. Option returns are bounded at -100% and
    unbounded up, so a mean can be positive while most contracts lost money.
"""

from __future__ import annotations

import argparse
import html
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

_BASE = os.path.dirname(os.path.abspath(__file__))
REPORT_DIR = os.path.join(_BASE, "report")
DB = os.environ.get(
    "SCANNER_DB",
    os.path.join(_BASE, "data", "attribution.db"),
)
ET = ZoneInfo("America/New_York")
MAX_HOURS_T1H = 2.0          # reject marks taken more than 2h after the flag
MAX_HOURS_T1D = 30.0         # ~24h target + overnight slack
# Expiry marks are intrinsic from the underlying close on expiry day — lag
# between flag time and write time does not change the settlement value.
# Do NOT apply a hours_expiry guard if/when an expiry outcomes section is added.
MAX_HOURS_EXPIRY = None
# Short-horizon exit marks use minutes_* lag (not hours_*). Cap ≈ 2× due lag.
MAX_MINUTES_T15M = 30.0
MAX_MINUTES_T30M = 60.0
OUTLIER = 1.0                # returns >= +100% treated as tail events
FACTOR_LOW_N = 30            # buckets below this get "(low n)"
# Collection window — factor section aggregates from here through report date.
WINDOW_START = "2026-08-10"
WINDOW_END_NOTE = "2026-08-28"
SHORT_MARK_OK = ("quote", "trade")  # usable exit observations only

# ── Paper-strategy analysis parameters (NOT in config.py — must not move hash)
ENTRY_TIME_ET = "10:00"      # CLOCK_1000: first scan at/after this ET clock
CONFIRM_N = 5                # CONFIRM_5: consecutive rank-1 scans before entry
_LOCKED_ENTRY_TIME_ET = "10:00"
_LOCKED_CONFIRM_N = 5

PAPER_RULES = ("FIRST_SEEN", "CLOCK_1000", "CONFIRM_5")
PAPER_VARIANTS = ("ALL", "0DTE", "1DTE+", "CONTROL")

# Shared by section_buckets and section_verdict — must never diverge.
# Single definition lives in scoring_pool.py (also used by the scorer).
from scoring_pool import DTE_BUCKET_SQL, dte_bucket as _dte_bucket_fn  # noqa: E402

# Live-quote window end (exclusive) — same constant mark_runner uses (09:30–16:15).
from mark_runner import MARK_WINDOW_END, load_cap_hits  # noqa: E402


def _et_clock_str(t: dtime) -> str:
    return f"{t.hour:02d}:{t.minute:02d}:{t.second:02d}"


def last_markable_flag_clock(horizon: str) -> str:
    """
    Latest ET flag clock whose mark due still falls inside the live-quote window.

    T+1h due = ts+1h → flags at/after (MARK_WINDOW_END − 1h) are unmarkable.
    T+1d due = ts+1d (same clock next day) → flags at/after MARK_WINDOW_END
    are unmarkable the next session.
    """
    end_dt = datetime.combine(date(2000, 1, 1), MARK_WINDOW_END)
    if horizon == "t1h":
        return _et_clock_str((end_dt - timedelta(hours=1)).time())
    if horizon == "t1d":
        return _et_clock_str(MARK_WINDOW_END)
    raise ValueError(f"last_markable_flag_clock: unsupported {horizon}")

# SQLite date()/time()/strftime() reinterpret offset-aware ISO strings
# (…T16:19:37-04:00 → 20:19:37 UTC wall). Read the stored ET fields instead.
def session_date_sql(column: str = "ts_et") -> str:
    """ET calendar date YYYY-MM-DD from an offset-aware ISO timestamp column."""
    return f"substr({column}, 1, 10)"


def et_clock_sql(column: str = "ts_et") -> str:
    """ET wall-clock HH:MM:SS from an offset-aware ISO timestamp column."""
    return f"substr({column}, 12, 8)"


@dataclass(frozen=True)
class ReportFilter:
    """Parsed CLI window — used to build table-specific WHERE clauses."""

    since: str | None = None          # session date >= since
    on_date: str | None = None        # session date = on_date
    ticker: str | None = None

    def where_sql(self, *, table_alias: str = "") -> tuple[str, list]:
        """
        Build an explicit WHERE for a given table (no string rewriting).

        table_alias: '' for bare columns, or 'f.' / 'v.' to qualify.
        Session dates use substr — never SQLite date() on offset-aware ISO.
        """
        p = f"{table_alias}." if table_alias and not table_alias.endswith(".") else table_alias
        sess = session_date_sql(f"{p}ts_et")
        clauses = ["1=1"]
        args: list = []
        if self.since is not None:
            clauses.append(f"{sess} >= ?")
            args.append(self.since)
        if self.on_date is not None:
            clauses.append(f"{sess} = ?")
            args.append(self.on_date)
        if self.ticker is not None:
            clauses.append(f"{p}ticker = ?")
            args.append(self.ticker)
        return " AND ".join(clauses), args


def max_hours_for(horizon: str) -> float | None:
    if horizon == "t1h":
        return MAX_HOURS_T1H
    if horizon == "t1d":
        return MAX_HOURS_T1D
    if horizon == "expiry":
        return MAX_HOURS_EXPIRY  # intentionally None — intrinsic, lag-irrelevant
    raise ValueError(f"unknown horizon: {horizon}")


def max_minutes_for(horizon: str) -> float:
    if horizon == "t15m":
        return MAX_MINUTES_T15M
    if horizon == "t30m":
        return MAX_MINUTES_T30M
    raise ValueError(f"not a short horizon: {horizon}")


def _short_method_col(horizon: str) -> str:
    return f"method_{horizon}"


def _short_minutes_col(horizon: str) -> str:
    return f"minutes_{horizon}"


def report_path(
    *,
    span_key: str,
    ticker: str | None,
    generated: datetime,
    ext: str = "txt",
) -> str:
    os.makedirs(REPORT_DIR, exist_ok=True)
    parts = ["eod", span_key]
    if ticker:
        parts.append(ticker.upper())
    parts.append(generated.strftime("%Y%m%d_%H%M%S"))
    return os.path.join(REPORT_DIR, "_".join(parts) + f".{ext.lstrip('.')}")


def q(conn, sql, args=()):
    return conn.execute(sql, args).fetchall()


def pct(x):
    return "     -" if x is None else f"{x:+6.1%}"


def hr(title=""):
    print("\n" + (f"── {title} " + "─" * (66 - len(title)) if title else "─" * 68))


# ── dual-render document (compute once → text + HTML) ─────────────────────────

CellKind = Literal["text", "num", "mean", "pct"]


@dataclass
class Cell:
    text: str
    kind: CellKind = "text"
    raw: float | None = None  # for mean colouring in HTML


@dataclass
class TableBlock:
    headers: list[str]
    rows: list[list[Cell]]
    muted_rows: list[bool] = field(default_factory=list)  # "(low n)" etc.


@dataclass
class ChartBlock:
    """Inline SVG — HTML only. Caption is HTML-only (txt skips this block)."""

    svg: str
    caption: list[str] = field(default_factory=list)
    callout: bool = False


@dataclass
class ReportDoc:
    """Structured report — one compute path, two renderers."""

    blocks: list[tuple[str, Any]] = field(default_factory=list)

    def banner(self, lines: list[str]) -> None:
        self.blocks.append(("banner", list(lines)))

    def section(self, title: str) -> None:
        self.blocks.append(("section", title))

    def lines(self, *lines: str, callout: bool = False) -> None:
        """Plain body lines (already without leading indent; renderers add it)."""
        self.blocks.append(("callout" if callout else "lines", list(lines)))

    def subhead(self, text: str) -> None:
        self.blocks.append(("subhead", text))

    def table(self, table: TableBlock) -> None:
        self.blocks.append(("table", table))

    def chart(self, chart: ChartBlock) -> None:
        self.blocks.append(("chart", chart))

    def html_section(self, title: str) -> None:
        self.blocks.append(("html_section", title))

    def html_lines(self, *lines: str) -> None:
        self.blocks.append(("html_lines", list(lines)))

    def blank(self) -> None:
        self.blocks.append(("blank", None))


class DocBuilder:
    """Append to a ReportDoc; optionally mirror historical stdout for tests."""

    def __init__(self, doc: ReportDoc | None = None, *, echo: bool | None = None):
        self.doc = doc if doc is not None else ReportDoc()
        # Echo text as we build when no shared doc (section_* unit tests).
        self.echo = (doc is None) if echo is None else echo

    def section(self, title: str) -> None:
        self.doc.section(title)
        if self.echo:
            hr(title)

    def lines(self, *lines: str, callout: bool = False) -> None:
        cleaned = [ln[2:] if ln.startswith("  ") else ln for ln in lines]
        self.doc.lines(*cleaned, callout=callout)
        if self.echo:
            for ln in cleaned:
                print(f"  {ln}" if ln else "")

    def subhead(self, text: str) -> None:
        self.doc.subhead(text)
        if self.echo:
            print(f"\n  {text}")

    def table(
        self,
        table: TableBlock,
        *,
        text_lines: list[str],
        html_only: bool = False,
    ) -> None:
        """Structured table for HTML + exact preformatted lines for .txt."""
        if not html_only:
            self.doc.blocks.append(("pre", list(text_lines)))
            if self.echo:
                for ln in text_lines:
                    print(ln)
        self.doc.table(table)

    def chart(self, chart: ChartBlock) -> None:
        self.doc.chart(chart)

    def html_section(self, title: str) -> None:
        self.doc.html_section(title)

    def html_lines(self, *lines: str) -> None:
        self.doc.html_lines(*lines)

    def pre(self, text_lines: list[str]) -> None:
        """Preformatted text-only chunk (no HTML table companion)."""
        self.doc.blocks.append(("pre", list(text_lines)))
        if self.echo:
            for ln in text_lines:
                print(ln)

    def blank(self) -> None:
        self.doc.blank()
        if self.echo:
            print()


_HTML_CSS = """
:root { color-scheme: light; }
body {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px; line-height: 1.45; color: #111; background: #fafafa;
  margin: 24px; max-width: 1100px;
}
.banner {
  border: 2px solid #111; padding: 12px 14px; margin-bottom: 20px;
  background: #fff; font-weight: 600;
}
h2 {
  margin: 28px 0 10px; padding-bottom: 4px;
  border-bottom: 1px solid #bbb; font-size: 15px; font-weight: 700;
}
h3 { margin: 18px 0 6px; font-size: 13px; font-weight: 700; }
.lines { margin: 4px 0 10px; }
.lines div { white-space: pre-wrap; }
.callout {
  margin: 8px 0 14px; padding: 10px 12px;
  border-left: 4px solid #111; background: #fff3c4;
  font-weight: 600;
}
.callout div { white-space: pre-wrap; }
.warn { color: #111; }
.info { color: #333; }
table {
  border-collapse: collapse; margin: 6px 0 14px; width: auto;
  background: #fff;
}
th, td {
  border: 1px solid #ccc; padding: 3px 10px; text-align: left;
  white-space: nowrap;
}
th { background: #f0f0f0; font-weight: 700; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.mean-pos { color: #0a7a2f; font-weight: 700; text-align: right; }
td.mean-neg { color: #b00020; font-weight: 700; text-align: right; }
td.mean-zero { text-align: right; font-weight: 700; }
tr.low-n td { color: #888; }
tr.low-n td.mean-pos, tr.low-n td.mean-neg { color: #888; font-weight: 600; }
.blank { height: 8px; }
.chart { margin: 12px 0 20px; background: #fff; padding: 8px 8px 4px; overflow-x: auto; }
.chart svg { display: block; max-width: 100%; height: auto; }
"""


def render_html(doc: ReportDoc) -> str:
    """Self-contained HTML: inline CSS only, no external assets / JS."""
    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8"/>',
        "<title>Scanner EOD Report</title>",
        "<style>",
        _HTML_CSS,
        "</style>",
        "</head>",
        "<body>",
    ]
    blocks = doc.blocks
    for idx, (kind, payload) in enumerate(blocks):
        if kind == "banner":
            parts.append('<header class="banner">')
            for ln in payload:
                parts.append(f"<div>{html.escape(ln)}</div>")
            parts.append("</header>")
        elif kind == "section":
            parts.append(f"<h2>{html.escape(payload)}</h2>")
        elif kind == "callout":
            parts.append('<div class="callout">')
            for ln in payload:
                parts.append(f"<div>{html.escape(ln)}</div>")
            parts.append("</div>")
        elif kind == "lines":
            parts.append('<div class="lines">')
            for ln in payload:
                esc = html.escape(ln)
                if ln.startswith("⚠"):
                    parts.append(f'<div class="warn">{esc}</div>')
                elif ln.startswith("i  "):
                    parts.append(f'<div class="info">{esc}</div>')
                else:
                    parts.append(f"<div>{esc}</div>")
            parts.append("</div>")
        elif kind == "html_section":
            parts.append(f"<h2>{html.escape(payload)}</h2>")
        elif kind == "html_lines":
            parts.append('<div class="lines">')
            for ln in payload:
                parts.append(f"<div>{html.escape(ln)}</div>")
            parts.append("</div>")
        elif kind == "subhead":
            parts.append(f"<h3>{html.escape(payload)}</h3>")
        elif kind == "chart":
            ch: ChartBlock = payload
            parts.append('<div class="chart">')
            parts.append(ch.svg)
            if ch.caption:
                cap_cls = "callout" if ch.callout else "lines"
                parts.append(f'<div class="{cap_cls}">')
                for ln in ch.caption:
                    parts.append(f"<div>{html.escape(ln)}</div>")
                parts.append("</div>")
            parts.append("</div>")
        elif kind == "table":
            tbl: TableBlock = payload
            parts.append("<table>")
            parts.append("<thead><tr>")
            for h in tbl.headers:
                parts.append(f"<th>{html.escape(h)}</th>")
            parts.append("</tr></thead><tbody>")
            for ri, row in enumerate(tbl.rows):
                muted = (
                    bool(tbl.muted_rows[ri]) if ri < len(tbl.muted_rows) else False
                )
                cls = ' class="low-n"' if muted else ""
                parts.append(f"<tr{cls}>")
                for c in row:
                    parts.append(_html_td(c))
                parts.append("</tr>")
            parts.append("</tbody></table>")
        elif kind == "blank":
            parts.append('<div class="blank"></div>')
        elif kind == "pre":
            nxt = blocks[idx + 1][0] if idx + 1 < len(blocks) else None
            if nxt == "table":
                continue
            parts.append('<div class="lines">')
            for ln in payload:
                body = ln[2:] if ln.startswith("  ") else ln
                parts.append(f"<div>{html.escape(body)}</div>")
            parts.append("</div>")
    parts.extend(["</body>", "</html>"])
    return "\n".join(parts) + "\n"


def _html_td(c: Cell) -> str:
    esc = html.escape(c.text.strip() if c.kind != "text" else c.text.strip())
    if c.kind == "mean":
        if c.raw is None:
            return f'<td class="num">{esc}</td>'
        if c.raw > 0:
            return f'<td class="mean-pos">{esc}</td>'
        if c.raw < 0:
            return f'<td class="mean-neg">{esc}</td>'
        return f'<td class="mean-zero">{esc}</td>'
    if c.kind in ("num", "pct"):
        return f'<td class="num">{esc}</td>'
    return f"<td>{html.escape(c.text.strip())}</td>"


def _mean_cell(x: float | None) -> Cell:
    return Cell(text=pct(x), kind="mean", raw=None if x is None else float(x))


# ── inline SVG charts (HTML only) ────────────────────────────────────────────

HIST_N_BINS = 20  # [-100%, +100%] in 10% steps
HIST_OVERFLOW = HIST_N_BINS  # index of ">+100%"
EXIT_TIMING_XS = ("15m", "30m", "1h", "close")
EXIT_TIMING_RANKS = ("01-03", "04-10", "11-20", "21+", "CONTROL")
_RANK_BUCKET_SQL = """CASE WHEN is_control = 1 THEN 'CONTROL'
                      WHEN rank <= 3  THEN '01-03'
                      WHEN rank <= 10 THEN '04-10'
                      WHEN rank <= 20 THEN '11-20'
                      ELSE '21+' END"""


def hist_bucket(r: float) -> int:
    """Map a return to histogram bin: 0..19 = [-100%,+100%] 10% steps; 20 = >+100%."""
    if r is None or r != r:
        return -1
    x = float(r)
    if x > 1.0:
        return HIST_OVERFLOW
    if x <= -1.0:
        return 0
    idx = int((x + 1.0) * 10 + 1e-12)
    return HIST_N_BINS - 1 if idx >= HIST_N_BINS else max(0, idx)


def hist_bucket_label(i: int) -> str:
    if i == HIST_OVERFLOW:
        return ">+100%"
    lo = -100 + 10 * i
    hi = lo + 10
    return f"{lo:+d}"


def histogram_counts(returns: list[float]) -> tuple[list[int], int, float | None]:
    """Counts per hist_bucket from the same clustered returns used in the table."""
    counts = [0] * (HIST_N_BINS + 1)
    clean = [float(x) for x in returns if x is not None and x == x]
    for x in clean:
        b = hist_bucket(x)
        if b >= 0:
            counts[b] += 1
    mean = statistics.mean(clean) if clean else None
    return counts, len(clean), mean


def _svg(parts: list[str], *, w: int, h: int) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" '
        f'width="{w}" height="{h}" role="img">'
        + "".join(parts)
        + "</svg>"
    )


def _tick_y(vmin: float, vmax: float, n: int = 5) -> list[float]:
    if vmin == vmax:
        vmin, vmax = vmin - 0.05, vmax + 0.05
    span = vmax - vmin
    step = span / max(1, n - 1)
    return [vmin + i * step for i in range(n)]


def svg_histogram(
    *,
    title: str,
    counts: list[int],
    n: int,
    mean: float | None,
) -> str:
    w, h = 720, 260
    l, r, t, b = 48, 16, 28, 44
    pw, ph = w - l - r, h - t - b
    nb = HIST_N_BINS + 1
    bar_w = pw / nb
    ymax = max(counts) if any(counts) else 1
    ymax = max(1, ymax)

    def x_at(ret: float) -> float:
        # -1.0 → left of first bar; +1.0 → left of overflow bar
        if ret > 1.0:
            return l + HIST_N_BINS * bar_w + bar_w * 0.5
        frac = (max(-1.0, min(1.0, ret)) + 1.0) / 2.0
        return l + frac * (HIST_N_BINS * bar_w)

    def y_at(c: float) -> float:
        return t + ph * (1 - c / ymax)

    parts = [
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#fff"/>',
        f'<text x="{l}" y="18" font-size="12" font-weight="700" '
        f'font-family="ui-monospace,monospace">'
        f'{html.escape(title)}  n={n}</text>',
        f'<line x1="{l}" y1="{t}" x2="{l}" y2="{t+ph}" stroke="#111"/>',
        f'<line x1="{l}" y1="{t+ph}" x2="{l+pw}" y2="{t+ph}" stroke="#111"/>',
    ]
    for c in _tick_y(0, ymax):
        yi = y_at(c)
        parts.append(
            f'<line x1="{l}" y1="{yi:.1f}" x2="{l+pw}" y2="{yi:.1f}" '
            f'stroke="#eee"/>'
        )
        parts.append(
            f'<text x="{l-6}" y="{yi+4:.1f}" text-anchor="end" font-size="10" '
            f'font-family="ui-monospace,monospace">{int(round(c))}</text>'
        )
    for i, c in enumerate(counts):
        x = l + i * bar_w
        bh = ph * (c / ymax)
        parts.append(
            f'<rect x="{x+1:.1f}" y="{t+ph-bh:.1f}" width="{bar_w-2:.1f}" '
            f'height="{bh:.1f}" fill="#333"/>'
        )
    # x labels every 20% plus overflow
    for i in range(0, HIST_N_BINS + 1, 2):
        x = l + i * bar_w
        lab = hist_bucket_label(i) if i < HIST_N_BINS else ">+100%"
        parts.append(
            f'<text x="{x:.1f}" y="{t+ph+14}" font-size="9" '
            f'font-family="ui-monospace,monospace" text-anchor="start">'
            f'{html.escape(lab)}</text>'
        )
    # 0% line
    x0 = x_at(0.0)
    parts.append(
        f'<line x1="{x0:.1f}" y1="{t}" x2="{x0:.1f}" y2="{t+ph}" '
        f'stroke="#111" stroke-width="1.2"/>'
    )
    parts.append(
        f'<text x="{x0+3:.1f}" y="{t+10}" font-size="10" '
        f'font-family="ui-monospace,monospace">0%</text>'
    )
    if mean is not None:
        xm = x_at(mean)
        parts.append(
            f'<line x1="{xm:.1f}" y1="{t}" x2="{xm:.1f}" y2="{t+ph}" '
            f'stroke="#b00020" stroke-dasharray="4 3"/>'
        )
        parts.append(
            f'<text x="{xm+3:.1f}" y="{t+22}" font-size="10" fill="#b00020" '
            f'font-family="ui-monospace,monospace">mean {mean:+.1%}</text>'
        )
    return _svg(parts, w=w, h=h)


def svg_exit_timing(series: list[dict]) -> str:
    """
    series items: {rank, n, means: {15m,30m,1h,close}, low_n: bool}
    Straight segments only — no smoothing.
    """
    w, h = 720, 300
    l, r, t, btm = 56, 140, 28, 36
    pw, ph = w - l - r, h - t - btm
    xs = EXIT_TIMING_XS
    vals = [
        m
        for s in series
        for m in (s["means"].get(k) for k in xs)
        if m is not None
    ]
    if not vals:
        return _svg(
            [f'<text x="20" y="40" font-family="ui-monospace,monospace">'
             f'no matched sample</text>'],
            w=w, h=80,
        )
    ymin, ymax = min(vals + [0.0]), max(vals + [0.0])
    pad = max(0.02, (ymax - ymin) * 0.12)
    ymin, ymax = ymin - pad, ymax + pad

    def x_at(i: int) -> float:
        return l + (i / (len(xs) - 1)) * pw

    def y_at(v: float) -> float:
        return t + ph * (1 - (v - ymin) / (ymax - ymin))

    marks = {
        "01-03": ("#111", "8,0", "circle"),
        "04-10": ("#333", "8,0", "sq"),
        "11-20": ("#555", "6,3", "tri"),
        "21+": ("#777", "2,3", "diamond"),
        "CONTROL": ("#111", "5,4", "plus"),
    }
    parts = [
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#fff"/>',
        f'<text x="{l}" y="18" font-size="12" font-weight="700" '
        f'font-family="ui-monospace,monospace">EXIT TIMING (matched)</text>',
        f'<line x1="{l}" y1="{t}" x2="{l}" y2="{t+ph}" stroke="#111"/>',
        f'<line x1="{l}" y1="{t+ph}" x2="{l+pw}" y2="{t+ph}" stroke="#111"/>',
    ]
    y0 = y_at(0.0)
    parts.append(
        f'<line x1="{l}" y1="{y0:.1f}" x2="{l+pw}" y2="{y0:.1f}" '
        f'stroke="#ccc"/>'
    )
    for tv in _tick_y(ymin, ymax):
        yi = y_at(tv)
        parts.append(
            f'<text x="{l-6}" y="{yi+3:.1f}" text-anchor="end" font-size="10" '
            f'font-family="ui-monospace,monospace">{tv:+.0%}</text>'
        )
    for i, lab in enumerate(xs):
        xi = x_at(i)
        parts.append(
            f'<text x="{xi:.1f}" y="{t+ph+16}" text-anchor="middle" '
            f'font-size="11" font-family="ui-monospace,monospace">'
            f'{html.escape(lab)}</text>'
        )
    for s in series:
        rank = s["rank"]
        color, dash, shape = marks.get(rank, ("#111", "8,0", "circle"))
        opacity = "0.5" if s["low_n"] else "1"
        pts = []
        for i, k in enumerate(xs):
            m = s["means"].get(k)
            if m is None:
                continue
            pts.append((x_at(i), y_at(m)))
        if len(pts) >= 2:
            d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            parts.append(
                f'<polyline fill="none" stroke="{color}" stroke-width="1.6" '
                f'stroke-dasharray="{dash}" opacity="{opacity}" '
                f'points="{d}"/>'
            )
        for x, y in pts:
            if shape == "sq":
                parts.append(
                    f'<rect x="{x-3:.1f}" y="{y-3:.1f}" width="6" height="6" '
                    f'fill="{color}" opacity="{opacity}"/>'
                )
            elif shape == "tri":
                parts.append(
                    f'<polygon points="{x:.1f},{y-4:.1f} {x+4:.1f},{y+3:.1f} '
                    f'{x-4:.1f},{y+3:.1f}" fill="{color}" opacity="{opacity}"/>'
                )
            elif shape == "diamond":
                parts.append(
                    f'<polygon points="{x:.1f},{y-4:.1f} {x+4:.1f},{y:.1f} '
                    f'{x:.1f},{y+4:.1f} {x-4:.1f},{y:.1f}" fill="{color}" '
                    f'opacity="{opacity}"/>'
                )
            elif shape == "plus":
                parts.append(
                    f'<line x1="{x-4:.1f}" y1="{y:.1f}" x2="{x+4:.1f}" '
                    f'y2="{y:.1f}" stroke="{color}" stroke-width="1.6" '
                    f'opacity="{opacity}"/>'
                    f'<line x1="{x:.1f}" y1="{y-4:.1f}" x2="{x:.1f}" '
                    f'y2="{y+4:.1f}" stroke="{color}" stroke-width="1.6" '
                    f'opacity="{opacity}"/>'
                )
            else:
                parts.append(
                    f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.2" fill="{color}" '
                    f'opacity="{opacity}"/>'
                )
        low = " (low n)" if s["low_n"] else ""
        li = EXIT_TIMING_RANKS.index(rank) if rank in EXIT_TIMING_RANKS else 0
        ly = t + 14 + li * 16
        lx = l + pw + 12
        parts.append(
            f'<text x="{lx}" y="{ly}" font-size="11" fill="{color}" '
            f'opacity="{opacity}" font-family="ui-monospace,monospace">'
            f'{html.escape(rank)}{html.escape(low)}  n={s["n"]}</text>'
        )
    return _svg(parts, w=w, h=h)


def svg_engine_vs_control(groups: list[dict]) -> str:
    """
    groups: [{pool, engine_n, engine_mean, ctrl_n, ctrl_mean,
              engine_low, ctrl_low}]
    """
    w, h = 640, 280
    l, r, t, btm = 56, 24, 28, 36
    pw, ph = w - l - r, h - t - btm
    vals = []
    for g in groups:
        for m in (g.get("engine_mean"), g.get("ctrl_mean")):
            if m is not None:
                vals.append(m)
    if not vals:
        return _svg(
            ['<text x="20" y="40" font-family="ui-monospace,monospace">'
             'no T15M observations</text>'],
            w=w, h=80,
        )
    ymin, ymax = min(vals + [0.0]), max(vals + [0.0])
    pad = max(0.02, (ymax - ymin) * 0.15)
    ymin, ymax = ymin - pad, ymax + pad

    def y_at(v: float) -> float:
        return t + ph * (1 - (v - ymin) / (ymax - ymin))

    y0 = y_at(0.0)
    n_g = max(1, len(groups))
    slot = pw / n_g
    bar_w = slot * 0.28
    parts = [
        f'<rect x="0" y="0" width="{w}" height="{h}" fill="#fff"/>',
        f'<text x="{l}" y="18" font-size="12" font-weight="700" '
        f'font-family="ui-monospace,monospace">'
        f'ENGINE 01-03 vs CONTROL  @ T15M</text>',
        f'<line x1="{l}" y1="{t}" x2="{l}" y2="{t+ph}" stroke="#111"/>',
        f'<line x1="{l}" y1="{t+ph}" x2="{l+pw}" y2="{t+ph}" stroke="#111"/>',
        f'<line x1="{l}" y1="{y0:.1f}" x2="{l+pw}" y2="{y0:.1f}" '
        f'stroke="#ccc"/>',
    ]
    for tv in _tick_y(ymin, ymax):
        yi = y_at(tv)
        parts.append(
            f'<text x="{l-6}" y="{yi+3:.1f}" text-anchor="end" font-size="10" '
            f'font-family="ui-monospace,monospace">{tv:+.0%}</text>'
        )

    def bar(x, mean, n, low, hatch: bool):
        if mean is None:
            return
        y = y_at(mean)
        top, bot = (y, y0) if mean >= 0 else (y0, y)
        fill = "#0a7a2f" if mean > 0 else ("#b00020" if mean < 0 else "#333")
        op = "0.4" if low else "1"
        if hatch:
            parts.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
                f'height="{max(1, bot-top):.1f}" fill="none" stroke="{fill}" '
                f'stroke-width="1.6" stroke-dasharray="3 2" opacity="{op}"/>'
            )
        else:
            parts.append(
                f'<rect x="{x:.1f}" y="{top:.1f}" width="{bar_w:.1f}" '
                f'height="{max(1, bot-top):.1f}" fill="{fill}" '
                f'opacity="{op}"/>'
            )
        low_s = " (low n)" if low else ""
        parts.append(
            f'<text x="{x+bar_w/2:.1f}" y="{top-4:.1f}" text-anchor="middle" '
            f'font-size="10" font-family="ui-monospace,monospace">'
            f'n={n}{html.escape(low_s)}</text>'
        )

    for i, g in enumerate(groups):
        cx = l + i * slot + slot / 2
        bar(cx - bar_w - 4, g.get("engine_mean"), g.get("engine_n") or 0,
            bool(g.get("engine_low")), False)
        bar(cx + 4, g.get("ctrl_mean"), g.get("ctrl_n") or 0,
            bool(g.get("ctrl_low")), True)
        parts.append(
            f'<text x="{cx:.1f}" y="{t+ph+16}" text-anchor="middle" '
            f'font-size="11" font-family="ui-monospace,monospace">'
            f'{html.escape(g["pool"])}</text>'
        )
    parts.append(
        f'<text x="{l}" y="{h-6}" font-size="10" '
        f'font-family="ui-monospace,monospace">'
        f'solid = engine 01-03   dashed outline = CONTROL</text>'
    )
    return _svg(parts, w=w, h=h)


def _row_to_obs(row) -> dict:
    if hasattr(row, "keys"):
        return {
            "dte_b": row["dte_b"],
            "rank_b": row["rank_b"],
            "d": row["d"],
            "contract": row["contract"],
            "r": row["r"],
        }
    return {
        "dte_b": row[0], "rank_b": row[1], "d": row[2],
        "contract": row[3], "r": row[4],
    }


def clustered_short_obs(conn, where: str, args, horizon: str) -> list[dict]:
    """
    One clustered (date, contract) observation — same filter as the
    T15M/T30M outcome tables. Charts reuse this list; they do not re-query.
    """
    ret = f"ret_{horizon}"
    mins = _short_minutes_col(horizon)
    mcol = _short_method_col(horizon)
    max_m = max_minutes_for(horizon)
    rows = q(conn, f"""
        SELECT {DTE_BUCKET_SQL} AS dte_b,
               {_RANK_BUCKET_SQL} AS rank_b,
               {session_date_sql("ts_et")} d,
               ticker||side||strike||expiry contract,
               AVG({ret}) r
        FROM v_outcomes
        WHERE {where}
          AND {ret} IS NOT NULL
          AND {mcol} IN ('quote', 'trade')
          AND ask IS NOT NULL AND ask > 0
          AND ({mins} IS NULL OR {mins} <= {max_m})
        GROUP BY dte_b, rank_b, d, contract
    """, args)
    return [_row_to_obs(r) for r in rows]


def _aggregate_outcome_rows(obs: list[dict]):
    """Table rows from clustered obs — same grouping as the old SQL aggregate."""
    groups: dict[tuple[str, str], list[float]] = defaultdict(list)
    for o in obs:
        r = o.get("r")
        if r is None or r != r:
            continue
        groups[(str(o["dte_b"]), str(o["rank_b"]))].append(float(r))
    out = []
    for dte_b, rank_b in sorted(groups):
        rs = groups[(dte_b, rank_b)]
        n = len(rs)
        mean = statistics.mean(rs)
        kept = [x for x in rs if x < OUTLIER]
        ex = statistics.mean(kept) if kept else None
        tails = sum(1 for x in rs if x >= OUTLIER)
        win = sum(1 for x in rs if x > 0) / n
        out.append((dte_b, rank_b, n, mean, ex, tails, win))
    return out


def engine_control_t15m_groups(obs: list[dict]) -> list[dict]:
    """Chart 3 — means from the same T15M clustered obs as the outcome table."""
    by: dict[tuple[str, str], list[float]] = defaultdict(list)
    for o in obs:
        pool, rank = str(o["dte_b"]), str(o["rank_b"])
        if pool not in ("0DTE", "1DTE+") or rank not in ("01-03", "CONTROL"):
            continue
        r = o.get("r")
        if r is None or r != r:
            continue
        by[(pool, rank)].append(float(r))
    groups = []
    for pool in ("0DTE", "1DTE+"):
        e = by.get((pool, "01-03"), [])
        c = by.get((pool, "CONTROL"), [])
        groups.append({
            "pool": pool,
            "engine_n": len(e),
            "engine_mean": statistics.mean(e) if e else None,
            "engine_low": len(e) < FACTOR_LOW_N,
            "ctrl_n": len(c),
            "ctrl_mean": statistics.mean(c) if c else None,
            "ctrl_low": len(c) < FACTOR_LOW_N,
        })
    return groups


def exit_timing_matched_obs(conn, where: str, args) -> list[dict]:
    """
    Clustered (date, contract) rows with usable marks at 15m, 30m, 1h, AND close.
    One query — table and line chart both consume this list.
    """
    rows = q(conn, f"""
        SELECT {_RANK_BUCKET_SQL} AS rank_b,
               {session_date_sql("ts_et")} d,
               ticker||side||strike||expiry contract,
               AVG(CASE
                     WHEN method_t15m IN ('quote', 'trade')
                      AND ask IS NOT NULL AND ask > 0
                      AND (minutes_t15m IS NULL OR minutes_t15m <= {MAX_MINUTES_T15M})
                     THEN ret_t15m END) AS r15,
               AVG(CASE
                     WHEN method_t30m IN ('quote', 'trade')
                      AND ask IS NOT NULL AND ask > 0
                      AND (minutes_t30m IS NULL OR minutes_t30m <= {MAX_MINUTES_T30M})
                     THEN ret_t30m END) AS r30,
               AVG(CASE
                     WHEN ret_t1h IS NOT NULL AND hours_t1h <= {MAX_HOURS_T1H}
                     THEN ret_t1h END) AS r1h,
               AVG(ret_close) AS rclose
        FROM v_outcomes
        WHERE {where}
        GROUP BY rank_b, d, contract
        HAVING r15 IS NOT NULL AND r30 IS NOT NULL
           AND r1h IS NOT NULL AND rclose IS NOT NULL
    """, args)
    out = []
    for row in rows:
        if hasattr(row, "keys"):
            rec = {
                "rank_b": row["rank_b"],
                "r15": row["r15"], "r30": row["r30"],
                "r1h": row["r1h"], "rclose": row["rclose"],
            }
        else:
            rec = {
                "rank_b": row[0],
                "r15": row[3], "r30": row[4],
                "r1h": row[5], "rclose": row[6],
            }
        out.append(rec)
    return out


def exit_timing_series(obs: list[dict]) -> list[dict]:
    """Aggregate matched obs → one series per rank bucket (n + means)."""
    by: dict[str, list[dict]] = defaultdict(list)
    for o in obs:
        by[str(o["rank_b"])].append(o)
    series = []
    key_map = {"15m": "r15", "30m": "r30", "1h": "r1h", "close": "rclose"}
    for rank in EXIT_TIMING_RANKS:
        rows = by.get(rank, [])
        if not rows:
            continue
        n = len(rows)
        means = {}
        for lab, col in key_map.items():
            vals = [float(r[col]) for r in rows if r[col] is not None and r[col] == r[col]]
            means[lab] = statistics.mean(vals) if vals else None
        series.append({
            "rank": rank,
            "n": n,
            "means": means,
            "low_n": n < FACTOR_LOW_N,
        })
    return series


def _timing_table(series: list[dict]) -> tuple[TableBlock, list[str]]:
    headers = ["rank", "n", "15m", "30m", "1h", "close"]
    text_lines = [
        f"  {'rank':<10} {'n':>5} {'15m':>8} {'30m':>8} {'1h':>8} {'close':>8}"
    ]
    rows: list[list[Cell]] = []
    muted: list[bool] = []
    for s in series:
        low = " (low n)" if s["low_n"] else ""
        text_lines.append(
            f"  {s['rank']:<10} {s['n']:>5} "
            f"{pct(s['means'].get('15m'))} {pct(s['means'].get('30m'))} "
            f"{pct(s['means'].get('1h'))} {pct(s['means'].get('close'))}{low}"
        )
        rows.append([
            Cell(s["rank"] + low),
            Cell(str(s["n"]), kind="num"),
            _mean_cell(s["means"].get("15m")),
            _mean_cell(s["means"].get("30m")),
            _mean_cell(s["means"].get("1h")),
            _mean_cell(s["means"].get("close")),
        ])
        muted.append(s["low_n"])
    return TableBlock(headers=headers, rows=rows, muted_rows=muted), text_lines


def section_charts(
    *,
    t15m_obs: list[dict],
    timing_obs: list[dict],
    doc: ReportDoc | None = None,
) -> None:
    """HTML-only charts. Values come from the same obs lists as the tables."""
    b = DocBuilder(doc, echo=False)
    b.html_section("CHARTS")
    b.html_lines(
        "inline SVG — same clustered observations as the tables above; "
        "n < 30 drawn muted/dashed",
    )

    # Chart 1 — distribution from T15M clustered obs, rank 01-03 per pool
    for pool in ("0DTE", "1DTE+"):
        rets = [
            float(o["r"]) for o in t15m_obs
            if str(o["dte_b"]) == pool
            and str(o["rank_b"]) == "01-03"
            and o.get("r") is not None and o["r"] == o["r"]
        ]
        counts, n, mean = histogram_counts(rets)
        b.chart(ChartBlock(
            svg=svg_histogram(
                title=f"RETURN DISTRIBUTION  {pool}  rank 01-03",
                counts=counts, n=n, mean=mean,
            ),
        ))

    # Chart 2 — matched sample (one query, table + line)
    timing_series = exit_timing_series(timing_obs)
    b.html_section("EXIT TIMING  (matched sample)")
    if not timing_series:
        b.html_lines(
            "no contracts with usable marks at 15m, 30m, 1h, AND close",
        )
    else:
        table, text_lines = _timing_table(timing_series)
        b.table(table, text_lines=text_lines, html_only=True)
        b.chart(ChartBlock(
            svg=svg_exit_timing(timing_series),
            caption=[
                "1h and close points use mid and are optimistic relative to "
                "15m/30m (ask-entry / bid-exit)",
            ],
            callout=True,
        ))

    # Chart 3 — same T15M obs, 01-03 vs CONTROL per pool
    groups = engine_control_t15m_groups(t15m_obs)
    b.chart(ChartBlock(svg=svg_engine_vs_control(groups)))


def _render_text_faithful(doc: ReportDoc) -> str:
    """
    Text renderer that uses preformatted chunks ('pre') when present so the
    .txt file stays identical to the historical print layout.
    """
    out: list[str] = []
    for kind, payload in doc.blocks:
        if kind == "banner":
            out.append("=" * 68)
            for ln in payload:
                out.append(f"  {ln}" if ln else "")
            out.append("=" * 68)
        elif kind == "section":
            title = payload or ""
            out.append("")
            out.append(
                f"── {title} " + "─" * (66 - len(title)) if title else "─" * 68
            )
        elif kind in ("lines", "callout"):
            for ln in payload:
                out.append(f"  {ln}" if ln else "")
        elif kind == "subhead":
            out.append("")
            out.append(f"  {payload}")
        elif kind == "pre":
            out.extend(payload)
        elif kind in ("table", "chart", "html_section", "html_lines"):
            # Structured tables/charts are HTML companions; text came via 'pre'.
            continue
        elif kind == "blank":
            out.append("")
    return "\n".join(out) + ("\n" if out else "")


# Public alias used by main / tests
render_text = _render_text_faithful  # type: ignore[misc,assignment]


def count_late_marks(conn, where: str, args, horizon: str) -> int:
    """Rows with a return present but hours_* above the horizon cap."""
    max_h = max_hours_for(horizon)
    if max_h is None:
        return 0
    ret, hrs = f"ret_{horizon}", f"hours_{horizon}"
    return int(q(conn, f"""
        SELECT COUNT(*) FROM v_outcomes
        WHERE {where}
          AND {ret} IS NOT NULL
          AND {hrs} IS NOT NULL
          AND {hrs} > ?
    """, (*args, max_h))[0][0])


def clustered_bucket_rows(conn, where: str, args, horizon: str = "t1h"):
    """
    Clustered (date, contract) outcomes by dte_bucket × rank_bucket.
    Returns list of (dte_b, rank_b, n, mean, ex_out, tails, win).
    """
    ret, hrs = f"ret_{horizon}", f"hours_{horizon}"
    max_h = max_hours_for(horizon)
    guard = f"AND {hrs} <= {max_h}" if max_h is not None else ""
    return q(conn, f"""
        WITH pc AS (
          SELECT {DTE_BUCKET_SQL} AS dte_b,
                 CASE WHEN is_control = 1 THEN 'CONTROL'
                      WHEN rank <= 3  THEN '01-03'
                      WHEN rank <= 10 THEN '04-10'
                      WHEN rank <= 20 THEN '11-20'
                      ELSE '21+' END rank_b,
                 {session_date_sql("ts_et")} d,
                 ticker||side||strike||expiry contract,
                 AVG({ret}) r
          FROM v_outcomes
          WHERE {where} AND {ret} IS NOT NULL {guard}
          GROUP BY dte_b, rank_b, d, contract
        )
        SELECT dte_b, rank_b, COUNT(*) n,
               AVG(r) mean,
               AVG(CASE WHEN r < {OUTLIER} THEN r END) ex_out,
               SUM(r >= {OUTLIER}) tails,
               SUM(r > 0)*1.0/COUNT(*) win
        FROM pc GROUP BY dte_b, rank_b ORDER BY dte_b, rank_b
    """, args)


def verdict_rows(conn, where: str, args):
    """
    Clustered T+1h verdict rows for 1DTE+ only (same DTE_BUCKET_SQL as buckets).
    Returns list of (bucket, n, mean) where bucket in TOP3 / CONTROL / REST.
    """
    return q(conn, f"""
        WITH pc AS (
          SELECT CASE WHEN is_control = 1 THEN 'CONTROL'
                      WHEN rank <= 3 THEN 'TOP3' ELSE 'REST' END b,
                 {session_date_sql("ts_et")} d,
                 ticker||side||strike||expiry c,
                 AVG(ret_t1h) r
          FROM v_outcomes
          WHERE {where}
            AND ret_t1h IS NOT NULL
            AND hours_t1h <= {MAX_HOURS_T1H}
            AND ({DTE_BUCKET_SQL}) = '1DTE+'
          GROUP BY b, d, c
        )
        SELECT b, COUNT(*), AVG(r) FROM pc GROUP BY b
    """, args)


# ── sections ─────────────────────────────────────────────────────────────────

def section_coverage(conn, where, args, *, doc: ReportDoc | None = None) -> bool:
    b = DocBuilder(doc)
    b.section("COVERAGE")
    r = q(conn, f"""
        SELECT COUNT(*) flags,
               SUM(is_control) ctrl,
               COUNT(DISTINCT run_id) runs,
               COUNT(DISTINCT {session_date_sql("ts_et")}) days,
               COUNT(DISTINCT config_hash) engines,
               SUM(mark_t1h IS NOT NULL) m1h,
               SUM(mark_t1d IS NOT NULL) m1d,
               SUM(mark_expiry IS NOT NULL) mexp,
               SUM(mark_t15m IS NOT NULL) m15,
               SUM(mark_t30m IS NOT NULL) m30,
               SUM(method_t15m IN ('quote', 'trade')) ok15,
               SUM(method_t30m IN ('quote', 'trade')) ok30
        FROM v_outcomes WHERE {where}""", args)[0]
    (flags, ctrl, runs, days, engines, m1h, m1d, mexp,
     m15, m30, ok15, ok30) = r
    if not flags:
        b.lines("no rows — check --date / --ticker")
        return False
    m15 = int(m15 or 0)
    m30 = int(m30 or 0)
    ok15 = int(ok15 or 0)
    ok30 = int(ok30 or 0)
    b.lines(
        f"runs {runs:<6} days {days:<4} flags {flags:<7} controls {ctrl}",
        f"marked   T+15m {m15:<6} T+30m {m30:<6} "
        f"T+1h {m1h:<7} T+1d {m1d:<7} expiry {mexp}",
        f"coverage T+15m {m15/flags:.0%}  T+30m {m30/flags:.0%}  "
        f"T+1h {m1h/flags:.0%}",
        f"usable   T+15m {ok15:<6} T+30m {ok30:<6} "
        f"(method quote|trade only — sealed stale/unavailable excluded)",
    )
    if engines > 1:
        b.lines(
            f"⚠  {engines} distinct config_hash values — sample spans "
            f"multiple engines and must be segmented before interpreting"
        )
    return True


def _outcome_table_from_rows(rows) -> tuple[TableBlock, list[str], int]:
    """Shared outcome-table builder for T15M/T30M/T1H/T1D (one compute path)."""
    headers = ["", "bucket", "n", "mean", "ex-tail", "tails", "win%"]
    text_lines = [
        f"  {'':<8} {'bucket':<9} {'n':>5} {'mean':>8} {'ex-tail':>8} "
        f"{'tails':>6} {'win%':>7}"
    ]
    table_rows: list[list[Cell]] = []
    last = None
    unknown_n = 0
    for dte_b, rank_b, n, mean, ex, tails, win in rows:
        lead = dte_b if dte_b != last else ""
        last = dte_b
        if dte_b == "UNKNOWN":
            unknown_n += int(n)
        text_lines.append(
            f"  {lead:<8} {rank_b:<9} {n:>5} {pct(mean)} {pct(ex)} "
            f"{tails:>6} {win:>6.0%}"
        )
        table_rows.append([
            Cell(str(lead)),
            Cell(str(rank_b)),
            Cell(str(int(n)), kind="num"),
            _mean_cell(mean),
            Cell(pct(ex).strip(), kind="pct"),
            Cell(str(int(tails)), kind="num"),
            Cell(f"{win:.0%}", kind="pct"),
        ])
    return TableBlock(headers=headers, rows=table_rows), text_lines, unknown_n


def count_short_sealed(conn, where: str, args, horizon: str) -> tuple[int, int]:
    """Return (n_stale, n_unavailable) for a short horizon."""
    mcol = _short_method_col(horizon)
    row = q(conn, f"""
        SELECT SUM({mcol} = 'stale'),
               SUM({mcol} = 'unavailable')
        FROM v_outcomes WHERE {where}
    """, args)[0]
    return int(row[0] or 0), int(row[1] or 0)


def count_late_short_marks(conn, where: str, args, horizon: str) -> int:
    ret = f"ret_{horizon}"
    mins = _short_minutes_col(horizon)
    mcol = _short_method_col(horizon)
    max_m = max_minutes_for(horizon)
    return int(q(conn, f"""
        SELECT COUNT(*) FROM v_outcomes
        WHERE {where}
          AND {ret} IS NOT NULL
          AND {mcol} IN ('quote', 'trade')
          AND {mins} IS NOT NULL
          AND {mins} > ?
    """, (*args, max_m))[0][0])


def clustered_short_bucket_rows(conn, where: str, args, horizon: str):
    """
    Clustered short-horizon outcomes (ask-entry ret_* from v_outcomes).
    Aggregates clustered_short_obs — the same list charts consume.
    """
    return _aggregate_outcome_rows(clustered_short_obs(conn, where, args, horizon))


def section_short_buckets(
    conn, where, args, horizon: str, *,
    doc: ReportDoc | None = None,
    obs: list[dict] | None = None,
):
    """OUTCOMES @ T15M / T30M — ask entry, bid exit, quote|trade only."""
    b = DocBuilder(doc)
    max_m = max_minutes_for(horizon)
    mins = _short_minutes_col(horizon)
    excluded = count_late_short_marks(conn, where, args, horizon)
    n_stale, n_unavail = count_short_sealed(conn, where, args, horizon)
    title = (
        f"OUTCOMES @ {horizon.upper()}  (clustered, ask-entry / bid-exit, "
        f"{mins} <= {max_m:g}; excluded {excluded} late; "
        f"sealed stale={n_stale} unavailable={n_unavail})"
    )
    b.section(title)

    if obs is None:
        obs = clustered_short_obs(conn, where, args, horizon)
    rows = _aggregate_outcome_rows(obs)
    if not rows:
        b.lines(
            "no usable short-horizon outcomes in window "
            "(need method quote|trade + ask > 0)"
        )
        if n_stale or n_unavail:
            b.lines(
                f"sealed (not observations): stale={n_stale}  "
                f"unavailable={n_unavail}"
            )
        return rows

    table, text_lines, unknown_n = _outcome_table_from_rows(rows)
    b.table(table, text_lines=text_lines)
    if unknown_n > 0:
        b.lines(
            "",
            f"⚠  UNKNOWN n={unknown_n}: rows predate the dte column and "
            f"cannot be classified — never merge into 0DTE or 1DTE+",
        )
    # Same text as before; callout flag makes the bid-exit caveat prominent in HTML.
    b.lines("", callout=False)
    b.lines(
        "mean    = ask-entry / bid-exit return (trade we actually make)",
        callout=True,
    )
    b.lines(
        "ex-tail = mean with >= +100% removed",
        f"sealed excluded from n: stale={n_stale}  unavailable={n_unavail}",
    )
    return rows


def section_buckets(
    conn, where, args, horizon="t1h", *, doc: ReportDoc | None = None,
):
    """Clustered by (date, contract). One observation per contract per day."""
    b = DocBuilder(doc)
    hrs = f"hours_{horizon}"
    max_h = max_hours_for(horizon)
    excluded = count_late_marks(conn, where, args, horizon)
    if max_h is None:
        title = (
            f"OUTCOMES @ {horizon.upper()}  (clustered, no lag filter — "
            f"intrinsic/settlement)"
        )
    else:
        title = (
            f"OUTCOMES @ {horizon.upper()}  (clustered, {hrs} <= {max_h}"
            f"; excluded {excluded} marks taken > {max_h}h late)"
        )
    b.section(title)
    if horizon == "t1h":
        cut_hm = last_markable_flag_clock("t1h")[:5]
        b.lines(
            f"note: excludes flags after ~{cut_hm} ET "
            f"(T+1h falls past the quote window)",
            "      — hour_et buckets for the final hour are "
            "structurally empty at T1H",
        )

    rows = clustered_bucket_rows(conn, where, args, horizon)
    if not rows:
        b.lines("no marked outcomes in window")
        return rows

    table, text_lines, unknown_n = _outcome_table_from_rows(rows)
    b.table(table, text_lines=text_lines)
    if unknown_n > 0:
        b.lines(
            "",
            f"⚠  UNKNOWN n={unknown_n}: rows predate the dte column and "
            f"cannot be classified — never merge into 0DTE or 1DTE+",
        )
    b.lines(
        "",
        "mean    = what an equal-weight basket returned (this is your P&L)",
        "ex-tail = mean with >= +100% removed (is the edge broad or 3 lucky rows?)",
    )
    return rows


def section_verdict(conn, where, args, *, doc: ReportDoc | None = None):
    b = DocBuilder(doc)
    b.section("VERDICT")
    rows = verdict_rows(conn, where, args)
    d = {bkt: (n, m) for bkt, n, m in rows}
    top, ctl = d.get("TOP3"), d.get("CONTROL")
    if not top or not ctl:
        b.lines("insufficient data — need both TOP3 and CONTROL rows (1DTE+)")
        return rows
    edge = top[1] - ctl[1]
    b.lines(
        f"1DTE+ TOP3   n={top[0]:<5} {pct(top[1])}",
        f"1DTE+ CONTROL n={ctl[0]:<5} {pct(ctl[1])}",
        f"edge over control: {edge:+.1%}",
    )
    n = min(top[0], ctl[0])
    if n < 200:
        b.lines(
            f"⚠  n={n} is too small to call. Need ~200+ contract-days per bucket."
        )
    elif abs(edge) < 0.02:
        b.lines("→ no meaningful edge over picking nearest-ATM")
    elif edge > 0:
        b.lines(
            "→ engine beats control. Check it holds across days before believing it."
        )
    else:
        b.lines("→ control beats the engine.")
    return rows


# ── paper strategy ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PaperTrade:
    rule: str
    session: str
    ticker: str
    contract: str
    entry_mid: float
    mark_close: float
    pnl: float
    dte_bucket: str          # 0DTE / 1DTE+ / UNKNOWN
    persist_scans: int       # consecutive rank-1 (or control) scans from entry
    is_control: bool


def _dte_bucket(dte) -> str:
    return _dte_bucket_fn(dte)


def _persist_from(scans: list, start_idx: int, contract: str) -> int:
    """Count consecutive scans from start_idx where contract stays selected."""
    n = 0
    for i in range(start_idx, len(scans)):
        if scans[i]["contract"] != contract:
            break
        n += 1
    return n


def _pick_entry_first_seen(scans: list) -> int | None:
    return 0 if scans else None


def _pick_entry_clock(scans: list) -> int | None:
    for i, s in enumerate(scans):
        if s["clock"] >= ENTRY_TIME_ET:
            return i
    return None


def _pick_entry_confirm(scans: list) -> int | None:
    if CONFIRM_N < 1:
        return None
    streak_c = None
    streak_n = 0
    for i, s in enumerate(scans):
        c = s["contract"]
        if c == streak_c:
            streak_n += 1
        else:
            streak_c = c
            streak_n = 1
        if streak_n >= CONFIRM_N:
            return i
    return None


_ENTRY_PICKERS = {
    "FIRST_SEEN": _pick_entry_first_seen,
    "CLOCK_1000": _pick_entry_clock,
    "CONFIRM_5": _pick_entry_confirm,
}


def _row_to_scan(r) -> dict:
    mc = r["mark_close"]
    return {
        "clock": r["clock"],
        "ts_et": r["ts_et"],
        "contract": r["contract"],
        "mid": float(r["mid"]),
        "mark_close": None if mc is None else float(mc),
        "dte_bucket": _dte_bucket(r["dte"]),
    }


def _load_paper_scans(
    conn,
    where: str,
    args,
    *,
    control: bool,
) -> dict[tuple[str, str], list[dict]]:
    """
    Scans keyed by (session, ticker), ordered by ts_et.

    Engine path: rank = 1 within pool, is_control = 0.
    Post-v1.2: both pools may have rank=1 in one run — load both; variants
    filter by dte_bucket. Pre-migration (pool NULL): treat as 1DTE+ timeline.

    Control path: is_control = 1; one row per (run_id, pool) (prefer CALL).

    Requires conn.row_factory = sqlite3.Row.
    """
    if control:
        sql = f"""
            SELECT {session_date_sql("ts_et")} AS sess,
                   {et_clock_sql("ts_et")} AS clock,
                   ts_et, run_id, ticker, side, strike, expiry, dte,
                   mid, mark_close, pool,
                   ticker || side || strike || expiry AS contract
            FROM flags
            WHERE {where}
              AND is_control = 1
              AND mid IS NOT NULL AND mid > 0
            ORDER BY sess, ticker, ts_et,
                     CASE pool WHEN '1DTE+' THEN 0 WHEN '0DTE' THEN 1 ELSE 2 END,
                     CASE side WHEN 'CALL' THEN 0 ELSE 1 END
        """
        rows = q(conn, sql, args)
        by: dict[tuple[str, str], list[dict]] = defaultdict(list)
        # One control per (run, pool) — not one per run (two pools now)
        seen_run: set[tuple[str, str, str, str]] = set()
        for r in rows:
            key = (r["sess"], r["ticker"])
            pool_k = r["pool"] if r["pool"] is not None else ""
            run_key = (r["sess"], r["ticker"], r["run_id"], pool_k)
            if run_key in seen_run:
                continue
            seen_run.add(run_key)
            by[key].append(_row_to_scan(r))
        return by

    sql = f"""
        SELECT {session_date_sql("ts_et")} AS sess,
               {et_clock_sql("ts_et")} AS clock,
               ts_et, ticker, side, strike, expiry, dte,
               mid, mark_close, pool,
               ticker || side || strike || expiry AS contract
        FROM flags
        WHERE {where}
          AND is_control = 0
          AND rank = 1
          AND mid IS NOT NULL AND mid > 0
        ORDER BY sess, ticker, ts_et,
                 CASE pool WHEN '1DTE+' THEN 0 WHEN '0DTE' THEN 1 ELSE 2 END
    """
    rows = q(conn, sql, args)
    by = defaultdict(list)
    for r in rows:
        by[(r["sess"], r["ticker"])].append(_row_to_scan(r))
    return by


def paper_trades_for_rule(
    scans_by_day: dict[tuple[str, str], list[dict]],
    rule: str,
    *,
    is_control: bool,
) -> list[PaperTrade]:
    """
    One trade per (rule, session, contract). Re-entries of the same contract
    the same day collapse — the first entry under the rule wins.
    """
    picker = _ENTRY_PICKERS[rule]
    out: list[PaperTrade] = []
    seen: set[tuple[str, str, str]] = set()  # session, ticker, contract
    for (sess, ticker), scans in sorted(scans_by_day.items()):
        idx = picker(scans)
        if idx is None:
            continue
        s = scans[idx]
        contract = s["contract"]
        key = (sess, ticker, contract)
        if key in seen:
            continue
        if s["mark_close"] is None:
            continue
        seen.add(key)
        pnl = (s["mark_close"] - s["mid"]) * 100.0
        out.append(PaperTrade(
            rule=rule,
            session=sess,
            ticker=ticker,
            contract=contract,
            entry_mid=s["mid"],
            mark_close=s["mark_close"],
            pnl=pnl,
            dte_bucket=s["dte_bucket"],
            persist_scans=_persist_from(scans, idx, contract),
            is_control=is_control,
        ))
    return out


def _summarize_pnls(trades: list[PaperTrade]) -> dict:
    if not trades:
        return {
            "n_days": 0, "n": 0, "win": None, "total": None, "mean": None,
            "median": None, "best": None, "worst": None, "persist": None,
        }
    pnls = [t.pnl for t in trades]
    days = len({t.session for t in trades})
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n_days": days,
        "n": len(trades),
        "win": wins / len(pnls),
        "total": sum(pnls),
        "mean": statistics.mean(pnls),
        "median": statistics.median(pnls),
        "best": max(pnls),
        "worst": min(pnls),
        "persist": statistics.mean(t.persist_scans for t in trades),
    }


def _fmt_money(x) -> str:
    if x is None:
        return "      -"
    return f"{x:+8.0f}"


def _fmt_win(x) -> str:
    if x is None:
        return "    -"
    return f"{x:5.0%}"


def section_paper_strategy(
    conn, filt: ReportFilter, *, doc: ReportDoc | None = None,
):
    """
    Buy-one-contract, hold-to-close under three fixed entry rules.

    Anti-curve-fitting: rules and parameters are locked; do not search over
    entry times / confirm counts or pick entries using the exit.
    """
    b = DocBuilder(doc)
    b.section("PAPER STRATEGY")
    b.lines(
        "three entry rules, fixed in advance — "
        "do not add, remove, or tune after seeing results",
        f"params  ENTRY_TIME_ET={ENTRY_TIME_ET}  CONFIRM_N={CONFIRM_N}  "
        f"exit=mark_close  pnl=(mark_close-mid)*100",
    )
    if (
        ENTRY_TIME_ET != _LOCKED_ENTRY_TIME_ET
        or CONFIRM_N != _LOCKED_CONFIRM_N
    ):
        b.lines(
            "⚠  ENTRY_TIME_ET/CONFIRM_N differ from locked defaults "
            f"({_LOCKED_ENTRY_TIME_ET} / {_LOCKED_CONFIRM_N}) — "
            "results across parameter values are not comparable"
        )

    where, args = filt.where_sql()
    engine_scans = _load_paper_scans(conn, where, args, control=False)
    control_scans = _load_paper_scans(conn, where, args, control=True)

    engine_by_rule = {
        rule: paper_trades_for_rule(engine_scans, rule, is_control=False)
        for rule in PAPER_RULES
    }
    control_by_rule = {
        rule: paper_trades_for_rule(control_scans, rule, is_control=True)
        for rule in PAPER_RULES
    }

    n_close = q(conn, f"""
        SELECT COUNT(*) FROM flags
        WHERE {where} AND mark_close IS NOT NULL
    """, args)[0][0]
    if not n_close:
        b.lines(
            "",
            "no mark_close rows in window — paper P&L empty until "
            "mark_runner writes session-close marks",
        )

    header = (
        f"  {'variant':<8} {'n_days':>6} {'n':>4} {'win%':>6} "
        f"{'total$':>8} {'mean$':>8} {'med$':>8} "
        f"{'best':>8} {'worst':>8} {'persist':>7}"
    )
    headers = [
        "variant", "n_days", "n", "win%", "total$", "mean$", "med$",
        "best", "worst", "persist",
    ]

    for rule in PAPER_RULES:
        b.subhead(rule)
        engine = engine_by_rule[rule]
        control = control_by_rule[rule]
        buckets = {
            "ALL": list(engine),
            "0DTE": [t for t in engine if t.dte_bucket == "0DTE"],
            "1DTE+": [t for t in engine if t.dte_bucket == "1DTE+"],
            "CONTROL": control,
        }
        text_lines = [header]
        table_rows: list[list[Cell]] = []
        for variant in PAPER_VARIANTS:
            s = _summarize_pnls(buckets[variant])
            persist = (
                "      -" if s["persist"] is None else f"{s['persist']:7.1f}"
            )
            text_lines.append(
                f"  {variant:<8} {s['n_days']:>6} {s['n']:>4} "
                f"{_fmt_win(s['win'])} "
                f"{_fmt_money(s['total'])} {_fmt_money(s['mean'])} "
                f"{_fmt_money(s['median'])} {_fmt_money(s['best'])} "
                f"{_fmt_money(s['worst'])} {persist}"
            )
            mean_raw = s["mean"]
            table_rows.append([
                Cell(variant),
                Cell(str(s["n_days"]), kind="num"),
                Cell(str(s["n"]), kind="num"),
                Cell(_fmt_win(s["win"]).strip(), kind="pct"),
                Cell(_fmt_money(s["total"]).strip(), kind="num"),
                Cell(
                    _fmt_money(s["mean"]).strip(),
                    kind="mean",
                    raw=None if mean_raw is None else float(mean_raw),
                ),
                Cell(_fmt_money(s["median"]).strip(), kind="num"),
                Cell(_fmt_money(s["best"]).strip(), kind="num"),
                Cell(_fmt_money(s["worst"]).strip(), kind="num"),
                Cell(persist.strip(), kind="num"),
            ])
        b.table(
            TableBlock(headers=headers, rows=table_rows),
            text_lines=text_lines,
        )

    b.lines(
        "",
        "persist = mean consecutive rank-1 (or control) scans from entry "
        "(diagnostic only; not extra observations)",
        "clustering: one trade per (rule, date, contract) — "
        "re-entries the same day do not add n",
    )


def _pool_expr_flags() -> str:
    """Prefer flags.pool; fall back to dte CASE (never silent-merge NULL)."""
    return f"""CASE
        WHEN pool IN ('0DTE', '1DTE+') THEN pool
        ELSE ({DTE_BUCKET_SQL})
    END"""


def _factor_quartile_edges(values: list[float]) -> tuple[float, float, float] | None:
    """Three cut points for Q1|Q2|Q3|Q4 from the full window sample."""
    clean = sorted(float(v) for v in values if v is not None and v == v)
    if len(clean) < 4:
        return None
    try:
        cuts = statistics.quantiles(clean, n=4, method="inclusive")
    except statistics.StatisticsError:
        return None
    if len(cuts) != 3:
        return None
    return float(cuts[0]), float(cuts[1]), float(cuts[2])


def _bucket_label_fixed(var: str, value) -> str | None:
    """Fixed pre-registered edges — do not retune."""
    if var == "hour_et":
        try:
            h = int(value)
        except (TypeError, ValueError):
            return None
        if 10 <= h <= 15:
            return f"{h:02d}"
        return None
    if value is None:
        if var == "delta":
            return "NULL"
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v:
        if var == "delta":
            return "NULL"
        return None
    if var == "iv":
        if v < 0.20:
            return "<0.20"
        if v <= 0.35:
            return "0.20-0.35"
        return ">0.35"
    if var == "spread_pct":
        if v < 0.03:
            return "<0.03"
        if v <= 0.08:
            return "0.03-0.08"
        return ">0.08"
    if var == "delta":
        if v < 0.15:
            return "<0.15"
        if v <= 0.40:
            return "0.15-0.40"
        return ">0.40"
    if var == "iv_premium":
        if v < 0.5:
            return "<0.5"
        if v <= 1.0:
            return "0.5-1.0"
        return ">1.0"
    if var == "vol_oi":
        if v < 1.0:
            return "<1"
        if v <= 10.0:
            return "1-10"
        return ">10"
    return None


def _quartile_label(value, edges: tuple[float, float, float]) -> str:
    v = float(value)
    q1, q2, q3 = edges
    if v <= q1:
        return "Q1"
    if v <= q2:
        return "Q2"
    if v <= q3:
        return "Q3"
    return "Q4"


# Display order for fixed buckets (quartiles appended dynamically).
_FACTOR_BUCKET_ORDER = {
    "iv": ("<0.20", "0.20-0.35", ">0.35"),
    "spread_pct": ("<0.03", "0.03-0.08", ">0.08"),
    "delta": ("NULL", "<0.15", "0.15-0.40", ">0.40"),
    "hour_et": ("10", "11", "12", "13", "14", "15"),
    "iv_premium": ("<0.5", "0.5-1.0", ">1.0"),
    "vol_oi": ("<1", "1-10", ">10"),
    "flow_raw": ("Q1", "Q2", "Q3", "Q4"),
    "leverage_raw": ("Q1", "Q2", "Q3", "Q4"),
}


def _load_factor_observations(
    conn,
    *,
    since: str,
    until: str,
    ticker: str | None,
) -> list[dict]:
    """
    One clustered (session, contract, pool, is_control) observation with
    T15M ask-entry return and factor inputs from flags (read-only).
    """
    sess = session_date_sql("ts_et")
    pool_x = _pool_expr_flags()
    clauses = [
        f"{sess} >= ?",
        f"{sess} <= ?",
        "method_t15m IN ('quote', 'trade')",
        "mark_t15m IS NOT NULL",
        "ask IS NOT NULL AND ask > 0",
        f"{pool_x} IN ('0DTE', '1DTE+')",
    ]
    args: list = [since, until]
    if ticker:
        clauses.append("ticker = ?")
        args.append(ticker)
    where = " AND ".join(clauses)
    rows = q(conn, f"""
        SELECT {sess} AS sess,
               ticker || side || strike || expiry AS contract,
               {pool_x} AS pool,
               is_control,
               AVG((mark_t15m - ask) / ask) AS ret,
               AVG(iv) AS iv,
               AVG(CASE
                     WHEN mid IS NOT NULL AND mid > 0
                          AND ask IS NOT NULL AND bid IS NOT NULL
                     THEN (ask - bid) / mid
                   END) AS spread_pct,
               AVG(ABS(delta)) AS delta_abs,
               SUM(CASE WHEN delta IS NULL THEN 1 ELSE 0 END) AS delta_nulls,
               COUNT(*) AS n_scans,
               CAST(substr(MIN(ts_et), 12, 2) AS INTEGER) AS hour_et,
               AVG(flow_raw) AS flow_raw,
               AVG(leverage_raw) AS leverage_raw,
               AVG(iv_premium) AS iv_premium,
               AVG(CASE
                     WHEN open_interest IS NOT NULL AND open_interest > 0
                     THEN CAST(volume AS REAL) / open_interest
                   END) AS vol_oi
        FROM flags
        WHERE {where}
        GROUP BY sess, contract, pool, is_control
    """, args)
    out = []
    for r in rows:
        d = dict(r) if hasattr(r, "keys") else None
        if d is None:
            continue
        # delta: if every scan null → NULL bucket; else use avg |delta|
        if int(d["delta_nulls"] or 0) >= int(d["n_scans"] or 0):
            d["delta"] = None
        else:
            d["delta"] = d["delta_abs"]
        out.append(d)
    return out


def section_factor_separation(
    conn,
    *,
    since: str,
    until: str,
    ticker: str | None,
    doc: ReportDoc | None = None,
):
    """
    Diagnostic: which logged variables separate T15M winners from losers.
    Aggregates window-to-date. Does not feed scoring.
    """
    b = DocBuilder(doc)
    b.section("FACTOR SEPARATION")
    b.lines(
        "diagnostic only — do not act on these until the window closes "
        f"{WINDOW_END_NOTE}",
        callout=True,
    )
    obs = _load_factor_observations(
        conn, since=since, until=until, ticker=ticker,
    )
    sessions = sorted({o["sess"] for o in obs}) if obs else []
    b.lines(
        f"range {since} → {until}  sessions={len(sessions)}  "
        f"clustered_obs={len(obs)}"
        f"{'  ticker=' + ticker if ticker else ''}",
    )
    b.lines(
        "returns = (mark_t15m - ask) / ask  |  method quote|trade only",
        callout=True,
    )
    if not obs:
        b.lines("no usable T15M observations in factor window")
        return

    q_edges: dict[tuple[str, str], tuple[float, float, float]] = {}
    for pool in ("0DTE", "1DTE+"):
        for var in ("flow_raw", "leverage_raw"):
            vals = [
                float(o[var])
                for o in obs
                if o["pool"] == pool
                and not o["is_control"]
                and o[var] is not None
                and o[var] == o[var]
            ]
            edges = _factor_quartile_edges(vals)
            if edges:
                q_edges[(pool, var)] = edges

    fixed_vars = (
        "iv", "spread_pct", "delta", "hour_et",
        "flow_raw", "leverage_raw", "iv_premium", "vol_oi",
    )

    for pool in ("0DTE", "1DTE+"):
        pool_obs = [o for o in obs if o["pool"] == pool]
        b.subhead(f"── {pool}  (n_obs={len(pool_obs)}) ──")
        if not pool_obs:
            b.lines("(no observations)")
            continue

        for var in fixed_vars:
            b.subhead(var)
            if var in ("flow_raw", "leverage_raw"):
                edges = q_edges.get((pool, var))
                if not edges:
                    b.lines("(insufficient data for quartile edges)")
                    continue
                b.lines(
                    f"quartile cuts (engine, window): "
                    f"{edges[0]:.4g} | {edges[1]:.4g} | {edges[2]:.4g}"
                )

            labeled: list[tuple[str, dict]] = []
            for o in pool_obs:
                if var in ("flow_raw", "leverage_raw"):
                    edges = q_edges.get((pool, var))
                    if not edges or o[var] is None or o[var] != o[var]:
                        continue
                    lab = _quartile_label(o[var], edges)
                elif var == "hour_et":
                    lab = _bucket_label_fixed(var, o.get("hour_et"))
                else:
                    lab = _bucket_label_fixed(var, o.get(var))
                if lab is None:
                    continue
                labeled.append((lab, o))

            headers = ["bucket", "who", "n", "mean", "win%"]
            text_lines = [
                f"  {'bucket':<12} {'who':<8} {'n':>5} {'mean':>8} {'win%':>7}"
            ]
            table_rows: list[list[Cell]] = []
            muted_rows: list[bool] = []
            order = list(_FACTOR_BUCKET_ORDER[var])
            seen_extra = sorted({lab for lab, _ in labeled if lab not in order})
            for lab in order + seen_extra:
                for who, ctrl_flag in (("engine", 0), ("CONTROL", 1)):
                    subset = [
                        o for lab_i, o in labeled
                        if lab_i == lab and int(o["is_control"] or 0) == ctrl_flag
                    ]
                    if not subset and ctrl_flag == 1:
                        continue
                    if not subset and ctrl_flag == 0:
                        continue
                    n = len(subset)
                    rets = [float(o["ret"]) for o in subset]
                    mean = statistics.mean(rets) if rets else None
                    win = (
                        sum(1 for r in rets if r > 0) / n if n else None
                    )
                    low_n = n < FACTOR_LOW_N
                    low = " (low n)" if low_n else ""
                    win_s = "     -" if win is None else f"{win:>6.0%}"
                    text_lines.append(
                        f"  {lab:<12} {who:<8} {n:>5} {pct(mean)} {win_s}{low}"
                    )
                    table_rows.append([
                        Cell(lab),
                        Cell(who),
                        Cell(str(n), kind="num"),
                        _mean_cell(mean),
                        Cell(
                            (win_s.strip() + low).strip(),
                            kind="pct",
                        ),
                    ])
                    muted_rows.append(low_n)
            b.table(
                TableBlock(
                    headers=headers, rows=table_rows, muted_rows=muted_rows,
                ),
                text_lines=text_lines,
            )


def _cap_hit_flag_line(filt: ReportFilter) -> str | None:
    """Surface mark_runner runtime-cap shortfalls recorded that session/window."""
    if filt.on_date:
        hits = load_cap_hits(on_date=filt.on_date)
    elif filt.since:
        hits = load_cap_hits(since=filt.since, until=date.today().isoformat())
    else:
        hits = load_cap_hits(on_date=date.today().isoformat())
    if not hits:
        return None
    by_h: dict[str, list[int]] = defaultdict(list)
    for rec in hits:
        by_h[str(rec.get("horizon") or "?")].append(int(rec.get("remaining") or 0))
    parts = [
        f"{h}×{len(ns)} (≈{sum(ns)} left)"
        for h, ns in sorted(by_h.items())
    ]
    n_events = len(hits)
    total_left = sum(int(r.get("remaining") or 0) for r in hits)
    return (
        f"mark_runner runtime cap hit — {n_events} horizon truncation"
        f"{'s' if n_events != 1 else ''} "
        f"[{'; '.join(parts)}]; ≈{total_left} rows left unmarked "
        f"(exit 0 for launchd — see data/mark_runner_cap_hits.jsonl)"
    )


def section_flags(
    conn, filt: ReportFilter, *, doc: ReportDoc | None = None,
):
    """Data-quality checks with explicit WHERE per table (no string rewrite)."""
    b = DocBuilder(doc)
    b.section("DATA-QUALITY FLAGS")
    out = []
    v_where, v_args = filt.where_sql()
    f_where, f_args = filt.where_sql()  # flags shares ts_et / ticker columns

    cap_line = _cap_hit_flag_line(filt)
    if cap_line:
        out.append(cap_line)

    n = q(conn, f"SELECT COUNT(*) FROM v_outcomes WHERE {v_where} AND score > 1.0",
          v_args)[0][0]
    if n:
        out.append(f"{n} rows scored above 1.0  (F-03: multipliers uncapped)")

    n = q(conn, f"""SELECT COUNT(*) FROM v_outcomes
                    WHERE {v_where} AND hours_t1h > ?""",
          (*v_args, MAX_HOURS_T1H))[0][0]
    if n:
        out.append(f"{n} T+1h marks taken > {MAX_HOURS_T1H}h late (excluded above)")

    n = q(conn, f"""SELECT COUNT(*) FROM v_outcomes
                    WHERE {v_where} AND hours_t1d > ?""",
          (*v_args, MAX_HOURS_T1D))[0][0]
    if n:
        out.append(f"{n} T+1d marks taken > {MAX_HOURS_T1D}h late (excluded above)")

    for horizon, label in (("t15m", "T+15m"), ("t30m", "T+30m")):
        n_stale, n_unavail = count_short_sealed(conn, v_where, v_args, horizon)
        if n_stale:
            out.append(
                f"{n_stale} {label} sealed method=stale "
                f"(not an observation)"
            )
        if n_unavail:
            out.append(
                f"{n_unavail} {label} sealed method=unavailable "
                f"(due after 16:00 ET — not an observation)"
            )
        n_late = count_late_short_marks(conn, v_where, v_args, horizon)
        if n_late:
            out.append(
                f"{n_late} {label} marks taken > "
                f"{max_minutes_for(horizon):g}m late (excluded above)"
            )

    n = q(conn, f"""SELECT COUNT(*) FROM flags
                    WHERE {f_where} AND notes LIKE '%stale:%'""", f_args)[0][0]
    if n:
        out.append(f"{n} rows refused as stale — permanently unmarkable")

    r = q(conn, f"""
        SELECT AVG(open_interest) FROM flags
        WHERE {f_where} AND rank <= 3 AND is_control = 0
          AND open_interest IS NOT NULL
        """, f_args)
    if r and r[0][0] is not None and r[0][0] < 200:
        out.append(f"top-3 avg open interest {r[0][0]:.0f} — vol/OI likely inflated")

    info: list[str] = []
    now_et = datetime.now(ET)
    clock = "substr(ts_et, 12, 8)"
    win_hm = _et_clock_str(MARK_WINDOW_END)[:5]
    overdue_age = {"t1h": timedelta(hours=4), "t1d": timedelta(hours=28)}

    for horizon, label in (("t1h", "T+1h"), ("t1d", "T+1d")):
        mark_col = f"mark_{horizon}"
        last_ok = last_markable_flag_clock(horizon)
        n_unmark = int(q(conn, f"""
            SELECT COUNT(*) FROM v_outcomes
            WHERE {v_where}
              AND {mark_col} IS NULL
              AND {clock} >= ?
        """, (*v_args, last_ok))[0][0])
        if n_unmark:
            info.append(
                f"{n_unmark} rows unmarkable for {label} "
                f"(due after {win_hm} ET — late-session flags)"
            )
        cutoff = (now_et - overdue_age[horizon]).isoformat(timespec="seconds")
        n_over = int(q(conn, f"""
            SELECT COUNT(*) FROM v_outcomes
            WHERE {v_where}
              AND {mark_col} IS NULL
              AND ts_et < ?
              AND {clock} < ?
        """, (*v_args, cutoff, last_ok))[0][0])
        if n_over:
            out.append(
                f"{n_over} rows overdue for a {label} mark — "
                f"is mark_runner alive?"
            )

    lines = []
    for x in info:
        lines.append(f"i  {x}")
    for x in out:
        lines.append(f"⚠ {x}")
    if lines:
        b.lines(*lines)
    else:
        b.lines("none")


# ── main ─────────────────────────────────────────────────────────────────────

def parse_filter(a: argparse.Namespace) -> tuple[ReportFilter, str, str]:
    """Return (filter, human span label, filename span_key)."""
    if a.days:
        since = (date.today() - timedelta(days=a.days)).isoformat()
        filt = ReportFilter(
            since=since,
            ticker=a.ticker.upper() if a.ticker else None,
        )
        return filt, f"last {a.days} days", f"last{a.days}d"
    d = a.date or date.today().isoformat()
    filt = ReportFilter(
        on_date=d,
        ticker=a.ticker.upper() if a.ticker else None,
    )
    return filt, d, d


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--date", help="YYYY-MM-DD (default: today)")
    p.add_argument("--days", type=int, help="rolling window instead of one day")
    p.add_argument("--ticker")
    p.add_argument("--db", default=DB)
    p.add_argument(
        "--factor-since",
        default=WINDOW_START,
        help=(
            f"start date for FACTOR SEPARATION window "
            f"(default: {WINDOW_START})"
        ),
    )
    p.add_argument(
        "--out",
        default=None,
        help="explicit output path (default: report/eod_<span>[_TICKER]_<ts>.txt)",
    )
    a = p.parse_args(argv)

    filt, span, span_key = parse_filter(a)
    w, args = filt.where_sql()
    conn = sqlite3.connect(a.db)
    conn.row_factory = sqlite3.Row

    # Factor section: window start → report date (not just today's session).
    factor_since = a.factor_since or WINDOW_START
    if filt.on_date:
        factor_until = filt.on_date
    else:
        factor_until = date.today().isoformat()
    if factor_since > factor_until:
        factor_since = factor_until

    generated = datetime.now()
    out_path = a.out or report_path(
        span_key=span_key, ticker=filt.ticker, generated=generated,
    )
    html_path = (
        out_path[:-4] + ".html"
        if out_path.lower().endswith(".txt")
        else report_path(
            span_key=span_key, ticker=filt.ticker, generated=generated, ext="html",
        )
    )
    os.makedirs(os.path.dirname(os.path.abspath(out_path)) or ".", exist_ok=True)

    # Compute once into ReportDoc; render text + HTML from the same blocks.
    doc = ReportDoc()
    doc.banner([
        f"SCANNER REPORT — {span}"
        f"{' — ' + filt.ticker if filt.ticker else ''}",
        f"generated {generated.strftime('%Y-%m-%d %H:%M')}",
        f"db {a.db}",
    ])

    if not section_coverage(conn, w, args, doc=doc):
        text = render_text(doc)
        html = render_html(doc)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        print(text, end="")
        print(f"\n  wrote {out_path}")
        print(f"  wrote {html_path}")
        return 1

    t15m_obs = clustered_short_obs(conn, w, args, "t15m")
    section_short_buckets(conn, w, args, "t15m", doc=doc, obs=t15m_obs)
    section_short_buckets(conn, w, args, "t30m", doc=doc)
    section_buckets(conn, w, args, "t1h", doc=doc)
    section_buckets(conn, w, args, "t1d", doc=doc)
    section_verdict(conn, w, args, doc=doc)
    section_paper_strategy(conn, filt, doc=doc)
    section_factor_separation(
        conn,
        since=factor_since,
        until=factor_until,
        ticker=filt.ticker,
        doc=doc,
    )
    timing_obs = exit_timing_matched_obs(conn, w, args)
    section_charts(t15m_obs=t15m_obs, timing_obs=timing_obs, doc=doc)
    section_flags(conn, filt, doc=doc)
    doc.blank()
    doc.section("SAVED")
    doc.lines(out_path)  # .txt path only — keep text report unchanged
    doc.blank()

    text = render_text(doc)
    html_doc = ReportDoc(blocks=list(doc.blocks))
    # HTML SAVED footer also points at the companion .html file.
    for i in range(len(html_doc.blocks) - 1, -1, -1):
        if html_doc.blocks[i][0] == "lines":
            html_doc.blocks[i] = ("lines", [out_path, html_path])
            break
    html = render_html(html_doc)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(text, end="")
    print(f"  also wrote {html_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
