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

from ui.widgets import _choice_control as choice

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
    assert shell.default_page() == ids[0] == "game_plan"       # the app opens on the plan


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
    assert _state(at)["page"] == "game_plan"


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
    assert _state(at)["page"] == "game_plan"


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


def test_sort_can_be_changed_again_after_returning_to_settings():
    """Regression: with a non-default sort stored, the next change snapped back to Volume."""
    at = _click(_app(), "settings")
    at.radio(key="flow_sort_by").set_value("Strike").run()
    at = _click(_click(at, "gamma"), "settings")
    at.radio(key="flow_sort_by").set_value("Premium $").run()
    assert not at.exception
    assert _state(at)["sort_by"] == "Premium $"
    assert at.radio(key="flow_sort_by").value == "Premium $"


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
    # Pages that take only cfg are routed through dispatch tables: their ids are dict keys.
    routed |= {k.value for n in ast.walk(main) if isinstance(n, ast.Dict)
               for k in n.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    # Component pages are routed through the registry rather than one branch each.
    from ui.components import REGISTRY

    src = ast.get_source_segment((ROOT / "app.py").read_text(), main)
    assert "page in COMPONENTS" in src and "COMPONENTS[page].render(" in src
    routed |= set(REGISTRY)
    assert set(shell.page_ids()) <= routed, set(shell.page_ids()) - routed


def test_sidebar_holds_only_the_menu_and_the_scan_watcher():
    tree = _app_tree()
    names = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "_sidebar" not in names and "_shell" in names
    assert (ROOT / "ui" / "pages" / "settings.py").exists()
    src = ast.get_source_segment((ROOT / "app.py").read_text(), _func(tree, "_shell"))
    assert "shell.render_menu(" in src and "shell.render_ticker_bar(" in src
    for widget in ("number_input", "st.button(", "checkbox", "multiselect"):
        assert widget not in src, f"{widget} must live on a page, not in the shell"


# ── settings saved to disk ──────────────────────────────────────────────────

class _FakeSt:
    def __init__(self, **state):
        self.session_state = dict(state)


def test_clean_keeps_valid_settings_and_drops_everything_else():
    good = {"min_dte": 3, "top_n": 9, "sort_by": "Strike", "show_all_ranked": True,
            "stop_buffer_pct": 0.25, "min_reward_to_risk": 2.0}
    assert shell.clean(good) == good
    for bad in (0.4, 5.5, "2", True, None):
        assert shell.clean({"min_reward_to_risk": bad}) == {}
    assert shell.clean({"stop_buffer_pct": 1}) == {"stop_buffer_pct": 1.0}
    for bad in (-0.1, 2.5, "0.1", True, None):
        assert shell.clean({"stop_buffer_pct": bad}) == {}
    assert shell.clean({"min_dte": -1, "top_n": 31, "sort_by": "Nope",
                        "show_all_ranked": "yes", "unknown": 1}) == {}
    assert shell.clean({"min_dte": True, "top_n": 2.5}) == {}        # bool / float are not ints
    assert shell.clean(None) == {} and shell.clean({}) == {}


def test_hydrate_loads_once_and_never_overrides_the_session():
    st = _FakeSt(cfg_top_n=7)
    shell.hydrate(st, {"top_n": 9, "show_all_ranked": True, "sort_by": "Bad"})
    assert shell.setting(st, "top_n") == 7                           # session value wins
    assert shell.setting(st, "show_all_ranked") is True
    assert shell.setting(st, "sort_by") == "Volume"                  # invalid -> default
    shell.hydrate(st, {"show_all_ranked": False})                    # second call is a no-op
    assert shell.setting(st, "show_all_ranked") is True


def test_save_if_changed_writes_only_on_a_real_change():
    st = _FakeSt()
    shell.hydrate(st, {})
    writes = []
    assert shell.save_if_changed(st, writes.append) is False         # nothing changed
    st.session_state["cfg_show_all_ranked"] = True
    assert shell.save_if_changed(st, writes.append) is True
    assert shell.save_if_changed(st, writes.append) is False
    assert writes == [{"min_dte": 1, "top_n": 5, "sort_by": "Volume",
                       "show_all_ranked": True, "stop_buffer_pct": 0.10,
                       "min_reward_to_risk": 1.5}]


def test_settings_file_round_trip_and_bad_files(tmp_path):
    from ui import settings_store

    path = str(tmp_path / "data" / "ui_settings.json")              # folder does not exist yet
    assert settings_store.load(path) == {}
    settings_store.save({"top_n": 9, "show_all_ranked": True}, path)
    assert settings_store.load(path) == {"top_n": 9, "show_all_ranked": True}
    assert sorted(p.name for p in (tmp_path / "data").iterdir()
                  if not p.name.endswith(".lock")) == ["ui_settings.json"]   # no temp files left
    (tmp_path / "data" / "ui_settings.json").write_text("{broken")
    assert settings_store.load(path) == {}
    (tmp_path / "data" / "ui_settings.json").write_text("[1, 2]")
    assert settings_store.load(path) == {}


def test_settings_file_lives_under_data_which_is_not_in_version_control():
    from ui import settings_store

    assert Path(settings_store.PATH) == ROOT / "data" / "ui_settings.json"


_SAVED = """
import streamlit as st
from ui import settings_store, shell
from ui.widgets import _choice_control
shell.hydrate(st, settings_store.load())
shell.render_flow_filters(st, _choice_control)
shell.render_best_value_settings(st)
shell.save_if_changed(st, settings_store.save)
st.json(shell.all_settings(st))
"""


def test_settings_survive_an_app_restart(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    from ui import settings_store

    monkeypatch.setattr(settings_store, "PATH", str(tmp_path / "ui_settings.json"))
    first = AppTest.from_string(_SAVED).run()
    assert not first.exception
    assert not (tmp_path / "ui_settings.json").exists()             # defaults are not written
    first.toggle(key="w_show_all_ranked").set_value(True).run()
    first.number_input(key="w_top_n").set_value(8).run()
    assert json.loads((tmp_path / "ui_settings.json").read_text()) == {
        "min_dte": 1, "top_n": 8, "sort_by": "Volume", "show_all_ranked": True,
        "stop_buffer_pct": 0.10, "min_reward_to_risk": 1.5}

    second = AppTest.from_string(_SAVED).run()                      # a brand-new session
    assert not second.exception
    assert json.loads(second.json[0].value) == {
        "min_dte": 1, "top_n": 8, "sort_by": "Volume", "show_all_ranked": True,
        "stop_buffer_pct": 0.10, "min_reward_to_risk": 1.5}
    assert second.toggle(key="w_show_all_ranked").value is True
    assert second.number_input(key="w_top_n").value == 8
