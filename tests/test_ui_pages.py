"""UI pages moved out of app.py, and the shared services they use."""

from __future__ import annotations

import ast
import importlib
import json
import shutil
from pathlib import Path

import pytest

import ui.services as svc

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
CURR, PREV = "AAPL_20260728_095049.json", "AAPL_20260728_093102.json"
PAGES_DIR = ROOT / "ui" / "pages"
PAGE_MODULES = sorted(p.stem for p in PAGES_DIR.glob("*.py") if p.stem != "__init__")

# app.py may only shrink. Lower this number whenever a page moves out.
APP_LINE_BUDGET = 3880


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


# ── Tickers ─────────────────────────────────────────────────────────────────

_TICKERS = """
import ui.services as svc
from ui.pages import tickers
svc._EXCLUDED_FILE = tickers._EXCLUDED_FILE = {excluded!r}
tickers._SCHED_CFG_FILE = {sched!r}
tickers.render()
"""


def test_tickers_page_renders_with_and_without_archives(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    script = _TICKERS.format(excluded=str(tmp_path / "excluded.json"),
                             sched=str(tmp_path / "sched.json"))
    _run(script)                                             # nothing scanned yet
    (tmp_path / "archive").mkdir()
    shutil.copy(GOLDEN / CURR, tmp_path / "archive" / CURR)
    _run(script)
    assert not (tmp_path / "excluded.json").exists()         # rendering writes nothing
    assert not (tmp_path / "sched.json").exists()


# ── Scanner Archive ─────────────────────────────────────────────────────────

_ARCHIVE = """
from ui.pages import archive
archive.render({cfg!r})
"""


def test_archive_page_renders_with_and_without_archives(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = {"ticker": "AAPL", "top_n": 5, "min_dte": 1, "sort_by": "Volume",
           "latest_archive": None}
    _run(_ARCHIVE.format(cfg=cfg))
    (tmp_path / "archive").mkdir()
    for name in (CURR, PREV):
        shutil.copy(GOLDEN / name, tmp_path / "archive" / name)
    svc._scan_archive_metadata.clear()
    _run(_ARCHIVE.format(cfg=cfg))


# ── shared services and option math ─────────────────────────────────────────

def test_scanner_dir_is_the_repo_root():
    assert Path(svc._SCANNER_DIR) == ROOT
    assert (Path(svc._SCANNER_DIR) / "dailyScaner.py").exists()


def test_discover_tickers_falls_back_to_aapl_and_honours_exclusions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(svc, "_EXCLUDED_FILE", str(tmp_path / "excluded.json"))
    assert svc._discover_tickers() == ["AAPL"]
    (tmp_path / "archive").mkdir()
    for t in ("AAPL", "NVDA"):
        (tmp_path / "archive" / f"{t}_20261002_100000.json").write_text("{}")
    assert svc._discover_tickers() == ["AAPL", "NVDA"]
    (tmp_path / "excluded.json").write_text(json.dumps(["NVDA"]))
    assert svc._discover_tickers() == ["AAPL"]


def test_background_scan_state_starts_idle():
    state = svc._bg_state("ZZZZ_TEST")
    assert state == {"running": False, "last_ok": None, "last_ts": None, "t0": 0.0}
    assert svc._bg_state("ZZZZ_TEST") is state               # same object on re-read
    svc._BG.pop("ZZZZ_TEST", None)


def test_display_greeks_are_sane_and_obey_put_call_parity():
    from ui.option_math import _bs_greeks

    call = _bs_greeks(333.0, 335.0, 0.30, 7, r=0.045, is_call=True)
    put = _bs_greeks(333.0, 335.0, 0.30, 7, r=0.045, is_call=False)
    assert 0 < call[0] < 1 and -1 < put[0] < 0
    assert call[0] - put[0] == pytest.approx(1.0, abs=1e-9)
    assert call[1] == pytest.approx(put[1]) and call[1] > 0   # same gamma
    assert call[2] < 0                                        # long options decay


# ── the page contract ───────────────────────────────────────────────────────

def test_expected_pages_exist():
    assert {"news", "spread_gate", "journal", "tickers", "archive"} <= set(PAGE_MODULES)


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
    moved = {"_render_tab2", "_render_tab3", "_render_tab4", "_render_tab5",
             "_render_tab_journal", "_cached_market_news", "_fmt_news_ts",
             "_fmt_journal_money", "_fmt_journal_pct", "_fmt_journal_ts",
             "_render_greeks_panel", "_ticker_summary", "_bs_greeks", "_discover_tickers",
             "_run_daily_scanner", "_scan_archive_metadata"}
    assert not (defined & moved)


def test_app_stays_within_its_line_budget():
    n = len((ROOT / "app.py").read_text().splitlines())
    assert n <= APP_LINE_BUDGET, f"app.py grew to {n} lines (budget {APP_LINE_BUDGET})"


def test_et_has_a_single_definition():
    from ui.common import ET

    assert str(ET) == "America/New_York"
    assert 'ET = ZoneInfo("America/New_York")' not in (ROOT / "app.py").read_text()
