"""UI shell: left-hand menu, ticker bar, settings that persist across pages."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from ui import shell

ROOT = Path(__file__).resolve().parents[1]

_SCRIPT = """
import streamlit as st
from ui import shell

def choice(label, options, *, default=None, key=None, help=None):
    kw = {"key": key, "horizontal": True}
    if key not in st.session_state:
        kw["index"] = options.index(default)
    return st.radio(label, options, **kw)

page = shell.render_menu(st)
ticker = shell.render_ticker_bar(st, TICKERS, status="Last scan 2026-10-02 16:05 ET")
if page == "settings":
    shell.render_flow_filters(st, choice)
st.json({"page": page, "ticker": ticker, **shell.flow_settings(st)})
"""


def _app(tickers=("AAPL", "NVDA")):
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(f"TICKERS = {list(tickers)!r}\n" + _SCRIPT).run()
    assert not at.exception, at.exception
    return at


def _state(at) -> dict:
    return json.loads(at.json[0].value)


def _click(at, page_id):
    at.sidebar.button(key=f"nav_btn_{page_id}").click().run()
    assert not at.exception, at.exception
    return at


# ── menu definition ─────────────────────────────────────────────────────────

def test_menu_ids_are_unique_and_settings_is_last():
    ids = shell.page_ids()
    assert len(ids) == len(set(ids))
    assert ids[-1] == "settings"
    assert shell.default_page() == ids[0] == "flow"


def test_every_entry_has_a_title_and_both_icon_styles():
    for e in shell.entries():
        assert e.title and e.material and e.emoji
        assert e.label(material_icons=True) == f":material/{e.material}: {e.title}"
        assert e.label(material_icons=False) == f"{e.emoji} {e.title}"


def test_go_rejects_an_unknown_page():
    class _St:
        session_state: dict = {}

    with pytest.raises(ValueError):
        shell.go(_St, "nope")


# ── menu behaviour ──────────────────────────────────────────────────────────

def test_menu_draws_one_button_per_entry_and_lands_on_the_first():
    at = _app()
    assert len(at.sidebar.button) == len(shell.entries())
    assert _state(at)["page"] == "flow"


def test_clicking_a_menu_entry_switches_page():
    at = _click(_app(), "gamma")
    assert _state(at)["page"] == "gamma"
    at = _click(at, "settings")
    assert _state(at)["page"] == "settings"


def test_unknown_stored_page_falls_back_to_the_landing_page():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string("TICKERS = ['AAPL']\n" + _SCRIPT)
    at.session_state[shell.NAV_KEY] = "removed_page"
    at.run()
    assert not at.exception
    assert _state(at)["page"] == "flow"


def test_go_selects_a_page_before_the_menu_is_drawn():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_string(
        "import streamlit as st\nfrom ui import shell\nshell.go(st, 'pretrade')\n"
        "TICKERS = ['AAPL']\n" + _SCRIPT
    ).run()
    assert not at.exception
    assert _state(at)["page"] == "pretrade"


# ── settings persist when the Settings page is not on screen ────────────────

def test_flow_settings_defaults():
    s = _state(_app())
    assert (s["min_dte"], s["top_n"], s["sort_by"]) == (1, 5, "Volume")


def test_flow_filters_are_only_drawn_on_settings():
    at = _app()
    assert len(at.number_input) == 0
    at = _click(at, "settings")
    assert {n.key for n in at.number_input} == {"w_min_dte", "w_top_n"}


def test_changed_settings_survive_leaving_and_returning():
    at = _click(_app(), "settings")
    at.number_input(key="w_top_n").set_value(9).run()
    at.number_input(key="w_min_dte").set_value(3).run()
    at.radio(key="flow_sort_by").set_value("Strike").run()
    assert not at.exception

    at = _click(at, "volume")                       # filters no longer drawn
    s = _state(at)
    assert (s["page"], s["min_dte"], s["top_n"], s["sort_by"]) == ("volume", 3, 9, "Strike")

    at = _click(at, "settings")                     # widgets come back with the stored values
    assert at.number_input(key="w_top_n").value == 9
    assert at.number_input(key="w_min_dte").value == 3
    assert at.radio(key="flow_sort_by").value == "Strike"


def test_setting_rejects_unknown_names():
    class _St:
        session_state: dict = {}

    with pytest.raises(KeyError):
        shell.setting(_St, "nope")


# ── ticker bar ──────────────────────────────────────────────────────────────

def test_ticker_bar_prefers_aapl_and_keeps_the_choice_across_pages():
    at = _app(("NVDA", "AAPL", "TSLA"))
    assert _state(at)["ticker"] == "AAPL"
    at.selectbox(key="w_ticker").set_value("TSLA").run()
    at = _click(at, "gamma")
    assert _state(at)["ticker"] == "TSLA"
    assert any("Last scan" in c.value for c in at.caption)


def test_stored_ticker_sees_a_change_made_in_this_run():
    class _St:
        session_state = {"w_ticker": "TSLA", shell.TICKER_STORE: "AAPL"}

    assert shell.stored_ticker(_St, ["AAPL", "TSLA"]) == "TSLA"
    _St.session_state = {shell.TICKER_STORE: "TSLA"}           # widget state wiped by a rerun
    assert shell.stored_ticker(_St, ["AAPL", "TSLA"]) == "TSLA"
    _St.session_state = {shell.TICKER_STORE: "GONE"}           # ticker no longer active
    assert shell.stored_ticker(_St, ["AAPL", "TSLA"]) == "AAPL"


def test_ticker_bar_falls_back_to_the_first_ticker_without_aapl():
    assert _state(_app(("NVDA", "TSLA")))["ticker"] == "NVDA"


def test_ticker_bar_refuses_an_empty_list():
    class _St:
        session_state: dict = {}

    with pytest.raises(ValueError):
        shell.render_ticker_bar(_St, [])


# ── app.py wiring (source checks; importing app.py would launch services) ───

def _app_tree() -> ast.Module:
    return ast.parse((ROOT / "app.py").read_text())


def _func(tree: ast.Module, name: str) -> ast.FunctionDef:
    return next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)


def test_main_routes_every_menu_entry():
    main = _func(_app_tree(), "main")
    routed = {
        c.value for n in ast.walk(main) if isinstance(n, ast.Compare)
        for c in n.comparators if isinstance(c, ast.Constant) and isinstance(c.value, str)
    }
    assert set(shell.page_ids()) <= routed, set(shell.page_ids()) - routed


def test_sidebar_holds_only_the_menu_and_the_scan_watcher():
    tree = _app_tree()
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "_sidebar" not in names and "_shell" in names and "_render_settings" in names
    src = ast.get_source_segment((ROOT / "app.py").read_text(), _func(tree, "_shell"))
    assert "shell.render_menu(" in src and "shell.render_ticker_bar(" in src
    for widget in ("number_input", "st.button(", "checkbox", "multiselect"):
        assert widget not in src, f"{widget} must live on a page, not in the shell"
