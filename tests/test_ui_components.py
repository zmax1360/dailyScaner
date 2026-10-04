"""UI components: Flow Magnets, Expiration Breakdown, Cost Distribution."""

from __future__ import annotations

import ast
import json
import shutil
from pathlib import Path

import pytest

from ui import shell
from ui.components import REGISTRY, cost_distribution, expiry_breakdown, flow_magnets
from ui.context import ScanContext, load_scan_context

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "golden"
CURR, PREV = "AAPL_20260728_095049.json", "AAPL_20260728_093102.json"


@pytest.fixture
def archive(tmp_path):
    d = tmp_path / "archive"
    d.mkdir()
    for name in (CURR, PREV):
        shutil.copy(GOLDEN / name, d / name)
    return str(d)


def _contract(strike, volume, oi, expiry="2026-10-05", price=1.25):
    return {"expiry": expiry, "strike": strike, "lastPrice": price, "volume": volume,
            "openInterest": oi}


# ── scan context ────────────────────────────────────────────────────────────

def test_context_loads_the_two_newest_archives(archive):
    ctx = load_scan_context("AAPL", top_n=7, archive_dir=archive)
    assert ctx is not None and ctx.ticker == "AAPL" and ctx.top_n == 7
    assert ctx.curr == json.loads((GOLDEN / CURR).read_text())
    assert ctx.prev == json.loads((GOLDEN / PREV).read_text())
    assert ctx.spot == float(ctx.curr["spot"]) and ctx.vol is ctx.curr["volume"]
    assert ctx.pc_ratio == float(ctx.curr["volume"]["pc_ratio"])
    assert ctx.prev_vol == ctx.prev["volume"]


def test_context_is_none_without_a_readable_archive(tmp_path):
    d = tmp_path / "archive"
    d.mkdir()
    assert load_scan_context("AAPL", archive_dir=str(d)) is None
    (d / "AAPL_20261002_100000.json").write_text("{not json")
    assert load_scan_context("AAPL", archive_dir=str(d)) is None
    (d / "AAPL_20261002_100000.json").write_text("[1, 2]")          # not a payload
    assert load_scan_context("AAPL", archive_dir=str(d)) is None


def test_context_with_a_single_archive_has_no_previous(tmp_path):
    d = tmp_path / "archive"
    d.mkdir()
    shutil.copy(GOLDEN / CURR, d / CURR)
    ctx = load_scan_context("AAPL", archive_dir=str(d))
    assert ctx.prev is None and ctx.prev_vol is None


def test_context_never_mixes_tickers(archive):
    assert load_scan_context("NVDA", archive_dir=archive) is None


# ── Flow Magnets ────────────────────────────────────────────────────────────

def test_contracts_table_formats_rows_and_flags_unusual_volume():
    styler = flow_magnets.contracts_table(
        [_contract(335.0, 35463, 2851), _contract(340.0, 14194, 11509)], n=5)
    df = styler.data
    assert list(df.columns) == ["EXPIRY", "STRIKE", "PRICE", "VOLUME", "OI", "VOL/OI"]
    assert df.loc[0, "STRIKE"] == "$335.0" and df.loc[0, "VOLUME"] == "35,463"
    assert df.loc[0, "VOL/OI"] == "12.44x 🔥"          # >= 2.0x is flagged
    assert df.loc[1, "VOL/OI"] == "1.23x"


def test_contracts_table_limits_rows_and_handles_empty_and_zero_oi():
    many = [_contract(300.0 + i, 1000, 100) for i in range(10)]
    assert len(flow_magnets.contracts_table(many, n=3).data) == 3
    assert flow_magnets.contracts_table([], n=5) is None
    zero_oi = flow_magnets.contracts_table([_contract(335.0, 500, 0)], n=5).data
    assert zero_oi.loc[0, "OI"] == "1"                 # divide-by-zero guard, as before


@pytest.mark.parametrize("val, hot", [("1.99x", False), ("2.00x 🔥", True),
                                      ("37.36x 🔥", True), ("n/a", False)])
def test_voi_style_heats_from_two_times(val, hot):
    assert bool(flow_magnets.voi_style(val)) is hot


# ── Expiration Breakdown ────────────────────────────────────────────────────

def test_expiry_table_from_a_real_archive(archive):
    ctx = load_scan_context("AAPL", archive_dir=archive)
    rows, chart_pc = expiry_breakdown.build_expiry_table(ctx.vol, ctx.prev_vol, ctx.pc_ratio)
    assert rows, "golden archive has expiries"
    assert {"EXPIRY", "DTE", "CALL VOL", "PUT VOL", "P/C", "BIAS", "CALL Δ", "PUT Δ"} \
        <= set(rows[0])
    assert [r["EXPIRY"] for r in rows] == sorted(r["EXPIRY"] for r in rows)
    assert set(chart_pc) == {r["EXPIRY"] for r in rows}


def test_expiry_table_is_empty_without_volume():
    rows, chart_pc = expiry_breakdown.build_expiry_table({}, None, 0.0)
    assert rows == [] and chart_pc == {}


# ── rendering (AppTest) ─────────────────────────────────────────────────────

_SCRIPT = """
from ui.context import load_scan_context
from ui.components import REGISTRY
ctx = load_scan_context("AAPL", top_n=5, archive_dir={archive!r})
REGISTRY[{cid!r}].render(ctx{extra})
"""

_COST = {"Average_Cost_POC": 306.21, "Profited_Shares_Pct": 91.6,
         "Cost_Range_90": (269.34, 341.28), "Cost_Range_70": (292.72, 341.28),
         "price_bins": [300.0, 305.0, 310.0, 335.0], "volume_bins": [5e8, 9e8, 7e8, 2e8],
         "days": 126, "total_volume": 6288770300, "spot": 333.69}


def _render(cid, archive, extra=""):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(_SCRIPT.format(archive=archive, cid=cid, extra=extra)).run()
    assert not at.exception, at.exception
    return at


def test_flow_magnets_renders_calls_and_puts(archive):
    at = _render("flow_magnets", archive)
    assert any("The Magnets" in m.value for m in at.markdown)
    assert len(at.dataframe) == 2


def test_expiry_breakdown_renders_its_table(archive):
    at = _render("expiry_breakdown", archive)
    assert any("Volume by Expiry" in m.value for m in at.markdown)
    assert len(at.dataframe) == 1


def test_cost_distribution_renders_metrics_from_supplied_data(archive):
    at = _render("cost_distribution", archive, extra=f", {_COST!r}")
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Average Cost (POC)"] == "$306.21"
    assert metrics["Profited Shares %"] == "91.6%"
    assert metrics["90% Cost Range"] == "$269.34 – $341.28"


def test_cost_distribution_caption_escapes_dollar_signs(archive):
    """Regression: '70% Cost Range: $a – $b' rendered as LaTeX math."""
    at = _render("cost_distribution", archive, extra=f", {_COST!r}")
    cap = next(c.value for c in at.caption if "70% Cost Range" in c.value)
    assert r"\$292.72 – \$341.28" in cap
    assert cap.count("$") == cap.count(r"\$")


def test_cost_distribution_reports_unavailable_data(archive):
    at = _render("cost_distribution", archive, extra=", {'price_bins': [1.0]}")
    assert any("unavailable" in i.value for i in at.info)


# ── the component contract ──────────────────────────────────────────────────

def test_every_component_has_a_title_and_a_render_entry_point():
    for cid, mod in REGISTRY.items():
        assert isinstance(mod.TITLE, str) and mod.TITLE, cid
        assert callable(mod.render), cid


def test_every_component_has_a_menu_entry_with_the_same_title():
    titles = {e.id: e.title for e in shell.entries()}
    for cid, mod in REGISTRY.items():
        assert titles.get(cid) == mod.TITLE, cid


def _imports(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            out |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module)
    return out


def test_components_never_import_the_app_or_each_other():
    comp_dir = ROOT / "ui" / "components"
    names = {p.stem for p in comp_dir.glob("*.py") if p.stem != "__init__"}
    for path in comp_dir.glob("*.py"):
        if path.stem == "__init__":
            continue
        imports = _imports(path)
        assert "app" not in imports, path.name
        siblings = {f"ui.components.{n}" for n in names - {path.stem}}
        assert not (imports & siblings), f"{path.name} imports {imports & siblings}"


def test_app_no_longer_defines_the_moved_helpers():
    tree = ast.parse((ROOT / "app.py").read_text())
    defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    moved = {"_voi_style", "_contracts_table", "_build_expiry_table",
             "_render_pc_term_chart", "_render_cost_distribution_panel"}
    assert not (defined & moved)


def test_scan_context_is_immutable():
    ctx = ScanContext(ticker="AAPL", curr={"spot": 333.0})
    with pytest.raises(Exception):
        ctx.ticker = "NVDA"
