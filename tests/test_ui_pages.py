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
APP_LINE_BUDGET = 395


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


# ── Settings and Telegram push ──────────────────────────────────────────────

_SETTINGS = """
import ui.telegram_push as tg
tg._ENV_FILE = {env!r}
from ui.pages import settings
settings.render({cfg!r})
"""


def test_settings_page_shows_scan_filters_and_telegram(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                     # no archives, no real .env
    cfg = {"ticker": "AAPL", "top_n": 5, "min_dte": 1, "sort_by": "Volume",
           "latest_archive": None}
    at = _run(_SETTINGS.format(env=str(tmp_path / "absent.env"), cfg=cfg))
    assert any("Run Scan for AAPL" in b.label for b in at.button)
    assert {n.key for n in at.number_input} == {"w_min_dte", "w_top_n"}
    assert any("Not configured" in w.value for w in at.warning)
    assert any("Gamma exposure" in c.label for c in at.checkbox)
    send = next(b for b in at.button if "Send to Telegram" in b.label)
    assert send.disabled                            # nothing to send, nowhere to send it


def test_telegram_config_is_read_from_the_env_file(tmp_path, monkeypatch):
    import ui.telegram_push as tg

    env = tmp_path / ".env"
    env.write_text('# comment\nTELEGRAM_BOT_TOKEN="abc:123"\nTELEGRAM_CHAT_ID = 42\nOTHER=x\n')
    monkeypatch.setattr(tg, "_ENV_FILE", str(env))
    assert tg._load_telegram_config() == ("abc:123", "42")
    monkeypatch.setattr(tg, "_ENV_FILE", str(tmp_path / "missing.env"))
    assert tg._load_telegram_config() == (None, None)


def test_scan_message_is_built_from_an_archive_payload():
    import ui.telegram_push as tg

    payload = json.loads((GOLDEN / CURR).read_text())
    prev = json.loads((GOLDEN / PREV).read_text())
    include = {k: True for k in ("session", "mtf", "magnets", "volume_expiry", "orb",
                                 "deltas", "best_value")}
    msg = tg._format_scan_message(payload=payload, prev_payload=prev, ticker="AAPL",
                                  top_n=5, include=include)
    assert isinstance(msg, str) and "AAPL" in msg and len(msg) > 200
    bare = tg._format_scan_message(payload=payload, prev_payload=None, ticker="AAPL",
                                   top_n=5, include={k: False for k in include})
    assert len(bare) < len(msg)


_GAMMA = {"expiry": "2026-10-05", "spot": 333.69, "net": 34_210_000.0,
          "call_wall": 330.0, "call_wall_gex": 15_250_000.0,
          "put_wall": 320.0, "put_wall_gex": -384_388.0,
          "top": [(330.0, 15_250_000.0), (335.0, 6_910_000.0), (332.5, 5_460_000.0)],
          "first_negative_below_spot": 325.0}


def _gamma_summary():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return {**_GAMMA, "as_of": datetime(2026, 10, 4, 16, 18, tzinfo=ZoneInfo("America/New_York"))}


def test_gamma_lines_for_telegram():
    import ui.telegram_push as tg

    assert tg.format_gamma_lines(_gamma_summary()) == [
        "🧱 <b>GAMMA · Oct 5</b>",
        "Net <b>+34.21M</b> — positive: dealer hedging tends to dampen moves",
        "Call wall <b>$330</b> (15.25M) · Put wall <b>$320</b> (-384,388)",
        "Largest: $330 15.25M · $335 6.91M · $332.5 5.46M",
        "First negative strike below spot: <b>$325</b>",
        "<i>dollars of hedging per $1 move · snapshot Sun Oct 04 16:18 ET</i>",
    ]


def test_gamma_lines_say_so_when_gamma_is_negative_or_missing():
    import ui.telegram_push as tg

    neg = {**_gamma_summary(), "net": -2_500_000.0, "call_wall": None, "call_wall_gex": None,
           "first_negative_below_spot": None}
    lines = tg.format_gamma_lines(neg)
    assert lines[1] == "Net <b>-2.50M</b> — negative: dealer hedging tends to amplify moves"
    assert lines[2] == "Put wall <b>$320</b> (-384,388)"
    assert not any("First negative" in line for line in lines)
    assert tg.format_gamma_lines(None) == [
        "🧱 <b>GAMMA</b> — no chain snapshot with usable open interest yet"]


def test_scan_message_includes_gamma_only_when_asked():
    import ui.telegram_push as tg

    payload = json.loads((GOLDEN / CURR).read_text())
    off = {k: False for k in ("session", "mtf", "magnets", "volume_expiry", "orb", "deltas",
                              "best_value")}
    base = tg._format_scan_message(payload=payload, prev_payload=None, ticker="AAPL",
                                   top_n=5, include=off, gamma=_gamma_summary())
    assert "GAMMA" not in base
    with_gamma = tg._format_scan_message(payload=payload, prev_payload=None, ticker="AAPL",
                                         top_n=5, include={**off, "gamma": True},
                                         gamma=_gamma_summary())
    assert "🧱 <b>GAMMA · Oct 5</b>" in with_gamma and "Call wall <b>$330</b>" in with_gamma
    assert len(with_gamma) < 4096                       # Telegram's message limit


def test_gamma_summary_for_reads_the_latest_snapshot(tmp_path, monkeypatch):
    import pandas as pd

    import ui.telegram_push as tg
    import volume_history as vh
    from datetime import datetime
    from zoneinfo import ZoneInfo

    db = str(tmp_path / "vh.db")
    monkeypatch.setattr(vh, "DB_PATH", db)
    assert tg.gamma_summary_for("AAPL", 333.0) is None          # no snapshot yet
    cols = ["strike", "lastPrice", "volume", "openInterest", "impliedVolatility", "bid", "ask",
            "expiry"]
    far = "2099-01-16"                                          # never expires under the test
    calls = pd.DataFrame([dict(zip(cols, (335.0, 9.0, 5000, 9000, 0.30, 8.9, 9.1, far)))])
    puts = pd.DataFrame([dict(zip(cols, (325.0, 7.0, 3000, 6000, 0.33, 6.9, 7.1, far)))])
    vh.record_scan(calls, puts, ticker="AAPL", scan_id="AAPL_20261002_100000",
                   ts=datetime(2026, 10, 2, 10, 0, tzinfo=ZoneInfo("America/New_York")),
                   db_path=db)
    s = tg.gamma_summary_for("aapl", "333.0")
    assert s["expiry"] == far and s["call_wall"] == 335.0 and s["put_wall"] == 325.0
    assert tg.gamma_summary_for("AAPL", None) is None
    assert tg.gamma_summary_for("AAPL", 0) is None


def test_archive_lookups(tmp_path, monkeypatch):
    from ui.archives import _latest_archive, _latest_archive_stamp

    monkeypatch.chdir(tmp_path)
    assert _latest_archive_stamp("AAPL") is None
    (tmp_path / "archive").mkdir()
    for name in (CURR, PREV):
        shutil.copy(GOLDEN / name, tmp_path / "archive" / name)
    assert _latest_archive_stamp("AAPL").startswith(CURR + "|")
    assert _latest_archive("AAPL") == json.loads((GOLDEN / CURR).read_text())


def test_unused_functions_are_gone_for_good():
    """Seven functions had no caller anywhere; they must not creep back unused."""
    gone = {"_render_expiry_vol_table", "_render_daily_run", "_render_weekly_run",
            "_load_archive_chain", "_check_card", "_rsi_label", "_bg_scan_worker",
            "_fmt_dollars"}
    for path in [ROOT / "app.py", *(ROOT / "ui").rglob("*.py")]:
        tree = ast.parse(path.read_text())
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert not (defined & gone), (path.name, defined & gone)


def test_app_has_no_unused_imports():
    tree = ast.parse((ROOT / "app.py").read_text())
    imported: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported |= {(a.asname or a.name).split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {a.asname or a.name for a in node.names}
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not (imported - used - {"annotations"}), sorted(imported - used)


# ── shared widgets and market helpers ───────────────────────────────────────

_CHOICE = """
import streamlit as st
from ui.widgets import _choice_control
{pre}
val = _choice_control("Sort by", ["Volume", "Premium $", "Strike"], default={default!r},
                      key="k")
st.json({{"val": val}})
"""


def test_choice_control_returns_the_default_then_the_selection():
    at = _run(_CHOICE.format(pre="", default="Premium $"))
    assert json.loads(at.json[0].value)["val"] == "Premium $"
    if at.radio:                      # Streamlit < 1.40 falls back to a horizontal radio
        at.radio[0].set_value("Strike").run()
    else:                             # pills / segmented control on newer versions
        at.session_state["k"] = "Strike"
        at.run()
    assert not at.exception
    assert json.loads(at.json[0].value)["val"] == "Strike"


def test_choice_control_accepts_a_value_preset_in_session_state():
    """A jump sets the key before the widget is drawn; passing a default too would raise."""
    at = _run(_CHOICE.format(pre='st.session_state["k"] = "Strike"', default="Volume"))
    assert json.loads(at.json[0].value)["val"] == "Strike"


def test_choice_control_falls_back_to_the_first_option_for_an_unknown_default():
    at = _run(_CHOICE.format(pre="", default="Nope"))
    assert json.loads(at.json[0].value)["val"] == "Volume"


def test_streamlit_version_check():
    from ui.widgets import _streamlit_ge

    assert _streamlit_ge(1, 0) is True
    assert _streamlit_ge(99, 0) is False


def test_market_clock_is_timezone_aware_eastern():
    from ui.market import _market_is_closed, _now_et

    now = _now_et()
    assert now.tzinfo is not None and str(now.tzinfo) == "America/New_York"
    assert isinstance(_market_is_closed(), bool)


# ── display fixes on Options Flow ───────────────────────────────────────────

_BANNER = """
import ui.market as market
market._market_is_closed = lambda: {closed!r}
market.render_market_banner()
"""


def test_market_closed_banner_shows_a_single_red_circle():
    """Regression: the icon and the text each carried a red circle."""
    at = _run(_BANNER.format(closed=True))
    assert len(at.error) == 1
    assert at.error[0].icon == "🔴"
    assert "🔴" not in at.error[0].value and "MARKET CLOSED" in at.error[0].value


def test_market_banner_is_absent_while_the_market_is_open():
    assert len(_run(_BANNER.format(closed=False)).error) == 0


def test_dollar_amounts_in_captions_are_escaped():
    """Regression: '1SD Expected Range: $a – $b · EM ±$c' rendered as LaTeX math."""
    from ui.pages import flow

    assert flow._usd(324.63) == r"\$324.63"
    assert flow._usd("9.064") == r"\$9.06"
    src = (ROOT / "ui" / "pages" / "flow.py").read_text()
    strip = src[src.index("# 1SD expected range strip"):src.index("# ZONE 2 — Main Workspace Grid")]
    assert "${" not in strip and strip.count("_usd(") == 3


def test_best_value_callout_escapes_dollars_and_has_one_star_per_line():
    """Regression: '$335.0 ... $1.25' rendered as math, and the first line had two stars."""
    import pandas as pd

    from ui.pages import flow

    best = pd.DataFrame([{"pool": "1DTE+", "side": "CALL", "strike": 335.0,
                          "expiry": "2026-10-05", "dte": 1, "Value_Score": 0.21,
                          "last": 1.25, "volume": 35463, "openInterest": 2851}])
    lines = flow._best_value_callout_lines(best)
    assert lines == [
        r"⭐ **1DTE+** CALL \$335.0 2026-10-05 (1d) · score 0.21 · \$1.25 · vol/OI 12.4x",
        "⭐ **0DTE** — not ranked",
    ]
    assert all(line.count("⭐") == 1 for line in lines)
    src = (ROOT / "ui" / "pages" / "flow.py").read_text()
    assert 'icon="⭐"' not in src                      # the icon doubled the first star


def test_best_value_caption_has_no_bare_dollar_and_points_at_settings():
    src = (ROOT / "ui" / "pages" / "flow.py").read_text()
    cap = src[src.index('"Ranks every contract by a composite score: "'):
              src.index('f"Velocity threshold')]
    assert "Price > \\\\$0.01" in cap
    assert "sidebar" not in cap and "Settings → Flow filters" in cap


def test_open_positions_has_its_own_row_below_volume_and_timeframes():
    src = (ROOT / "ui" / "pages" / "flow.py").read_text()
    block = src[src.index("# Row 1: Volume Analysis | Multi-Timeframe"):
                src.index("# ZONE 3 — Catalyst")]
    row1, row2 = block.split("# Row 2: My Open Positions, full width")
    assert "st.columns([1, 1.4])" in row1
    assert "_render_volume_analysis(" in row1 and "_render_mtf_matrix(" in row1
    assert "_render_portfolio_manager(" in row2 and "with sub_c" not in row2


def _flow_render():
    src = (ROOT / "ui" / "pages" / "flow.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "render")
    return src, fn


def test_every_flow_section_is_guarded_exactly_once():
    from ui.pages import flow

    src, fn = _flow_render()
    guards = [
        n.test.left.value for n in fn.body
        if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
        and isinstance(n.test.left, ast.Constant)
        and isinstance(n.test.comparators[0], ast.Name) and n.test.comparators[0].id == "sections"
    ]
    assert guards == list(flow.SECTIONS)


def test_flow_sections_do_not_depend_on_each_other():
    """A section may only use names set before the first section, or by itself."""
    from ui.pages import flow

    src, fn = _flow_render()
    section_of = {}
    shared: set[str] = set()
    for stmt in fn.body:
        name = None
        if isinstance(stmt, ast.If) and isinstance(stmt.test, ast.Compare) \
                and isinstance(stmt.test.left, ast.Constant) \
                and stmt.test.left.value in flow.SECTIONS:
            name = stmt.test.left.value
        stores = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)
                  and isinstance(n.ctx, ast.Store)}
        if name is None:
            shared |= stores
        else:
            section_of[name] = (stmt, stores)
    for name, (stmt, _) in section_of.items():
        loads = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)
                 and isinstance(n.ctx, ast.Load)}
        for other, (_, other_stores) in section_of.items():
            if other != name:
                leaked = (loads & other_stores) - shared - section_of[name][1]
                assert not leaked, f"{name} uses {sorted(leaked)} set by {other}"


def test_options_flow_overview_leaves_the_chart_and_best_value_to_their_own_pages():
    from ui.pages import flow

    assert flow.OVERVIEW == {"header", "context", "positions", "news", "details"}
    assert not ({"chart", "best_value"} & flow.OVERVIEW)
    titles = {e.id: e.title for e in __import__("ui.shell", fromlist=["x"]).entries()}
    assert titles["best_value"] == "Best Value" and titles["price_chart"] == "Chart"


def test_flow_render_rejects_unknown_sections():
    from ui.pages import flow

    with pytest.raises(ValueError):
        flow.render({"ticker": "AAPL"}, sections=frozenset({"nope"}))


def test_best_value_page_draws_only_the_best_value_section(monkeypatch):
    from ui.pages import flow

    seen = {}
    monkeypatch.setattr(flow, "render", lambda cfg, sections=None: seen.update(s=sections))
    flow.render_best_value({"ticker": "AAPL"})
    assert seen["s"] == frozenset({"best_value"})


def test_direction_card_says_what_it_measures():
    src = (ROOT / "ui" / "pages" / "flow.py").read_text()
    assert "Scanner direction · multi-timeframe score" in src


# ── the page contract ───────────────────────────────────────────────────────

def test_expected_pages_exist():
    assert {"news", "spread_gate", "journal", "tickers", "archive", "flow",
            "settings"} <= set(PAGE_MODULES)


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
             "_run_daily_scanner", "_scan_archive_metadata", "_render_tab1",
             "_render_best_value_panel", "_render_portfolio_manager", "evaluate_portfolio",
             "_choice_control", "_streamlit_ge", "_now_et", "_market_is_closed",
             "_cached_vwap_state", "_build_best_value_df", "_rsi_plain",
             "_render_settings", "_format_scan_message", "_send_telegram",
             "_load_telegram_config", "_latest_archive", "_latest_archive_stamp"}
    assert not (defined & moved)


def test_app_stays_within_its_line_budget():
    n = len((ROOT / "app.py").read_text().splitlines())
    assert n <= APP_LINE_BUDGET, f"app.py grew to {n} lines (budget {APP_LINE_BUDGET})"


def test_et_has_a_single_definition():
    from ui.common import ET

    assert str(ET) == "America/New_York"
    assert 'ET = ZoneInfo("America/New_York")' not in (ROOT / "app.py").read_text()
