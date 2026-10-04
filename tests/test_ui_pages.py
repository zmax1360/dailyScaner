"""UI pages moved out of app.py: Market News, Spread Gate, Journal."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PAGES_DIR = ROOT / "ui" / "pages"
PAGE_MODULES = sorted(p.stem for p in PAGES_DIR.glob("*.py") if p.stem != "__init__")

# app.py may only shrink. Lower this number whenever a page moves out.
APP_LINE_BUDGET = 5110


def _run(script: str):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(script, default_timeout=30).run()
    assert not at.exception, at.exception
    return at


# ── Market News ─────────────────────────────────────────────────────────────

_NEWS = """
from ui.pages import news
news._cached_market_news = lambda tickers, limit=15: {articles!r}
news.render({cfg!r})
"""

_ARTICLES = [
    {"headline": "Apple <b>beats</b> estimates", "url": "https://example.com/a",
     "source": "Wire", "datetime": 1791000000, "news_bias": "BULLISH"},
    {"headline": "Supply worries", "url": "", "source": "Desk", "datetime": 0,
     "news_bias": "BEARISH"},
]


def test_news_renders_headlines_for_the_selected_ticker():
    at = _run(_NEWS.format(articles=_ARTICLES, cfg={"ticker": "aapl"}))
    text = " ".join(m.value for m in at.markdown)
    assert "Latest 2 headlines — AAPL" in text
    assert "Apple &lt;b&gt;beats&lt;/b&gt; estimates" in text      # HTML is escaped
    assert "BULLISH" in text and "BEARISH" in text


def test_news_empty_states():
    at = _run(_NEWS.format(articles=[], cfg={"ticker": "AAPL"}))
    assert any("No headlines found for AAPL" in i.value for i in at.info)
    at = _run(_NEWS.format(articles=_ARTICLES, cfg={"ticker": ""}))
    assert any("Pick a ticker" in i.value for i in at.info)


# ── Spread Gate ─────────────────────────────────────────────────────────────

_GATE = """
import snapshot_store
snapshot_store.load_gate_history = lambda: []
snapshot_store.save_gate_history = lambda history: None
from ui.pages import spread_gate
spread_gate.render({cfg!r})
"""


@pytest.mark.parametrize("latest, spot", [({"spot": 333.69}, 333.69), (None, 200.0)])
def test_spread_gate_form_prefills_spot_from_the_latest_archive(latest, spot):
    at = _run(_GATE.format(cfg={"ticker": "AAPL", "latest_archive": latest}))
    assert any("Spread Gate Evaluator" in h.value for h in at.header)
    spot_input = next(n for n in at.number_input if n.label == "Spot ($)")
    assert spot_input.value == pytest.approx(spot)


# ── Journal ─────────────────────────────────────────────────────────────────

_JOURNAL = """
import scanner.journal_io as jio
jio.JOURNAL_DIR = {journal_dir!r}
from ui.pages import journal
journal.render()
"""


def test_journal_renders_without_any_journal_files(tmp_path):
    _run(_JOURNAL.format(journal_dir=str(tmp_path / "empty")))


def test_journal_renders_the_golden_journal():
    at = _run(_JOURNAL.format(journal_dir=str(ROOT / "tests" / "golden" / "journal")))
    assert len(at.error) == 0, [e.value for e in at.error]


# ── the page contract ───────────────────────────────────────────────────────

def test_expected_pages_exist():
    assert {"news", "spread_gate", "journal"} <= set(PAGE_MODULES)


@pytest.mark.parametrize("name", PAGE_MODULES)
def test_every_page_exposes_render(name):
    mod = importlib.import_module(f"ui.pages.{name}")
    assert callable(getattr(mod, "render", None)), name


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
            out |= {f"{node.module}.{a.name}" for a in node.names}
    return out


@pytest.mark.parametrize("name", PAGE_MODULES)
def test_pages_never_import_the_app_or_another_page(name):
    imports = _imports(PAGES_DIR / f"{name}.py")
    assert "app" not in imports
    others = {f"ui.pages.{n}" for n in PAGE_MODULES if n != name}
    assert not (imports & others), imports & others


def test_components_never_import_pages():
    for path in (ROOT / "ui" / "components").glob("*.py"):
        assert not any(i.startswith("ui.pages") for i in _imports(path)), path.name


def test_app_no_longer_defines_the_moved_pages():
    tree = ast.parse((ROOT / "app.py").read_text())
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    moved = {"_render_tab3", "_render_tab5", "_render_tab_journal", "_cached_market_news",
             "_fmt_news_ts", "_fmt_journal_money", "_fmt_journal_pct", "_fmt_journal_ts"}
    assert not (defined & moved)


def test_app_stays_within_its_line_budget():
    n = len((ROOT / "app.py").read_text().splitlines())
    assert n <= APP_LINE_BUDGET, f"app.py grew to {n} lines (budget {APP_LINE_BUDGET})"


def test_et_has_a_single_definition():
    from ui.common import ET

    assert str(ET) == "America/New_York"
    assert 'ET = ZoneInfo("America/New_York")' not in (ROOT / "app.py").read_text()
