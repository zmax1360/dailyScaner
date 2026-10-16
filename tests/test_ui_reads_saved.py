"""The app reads the Best Value ranking the scan saved; it never scores
(AGENTS.md: "UI never recomputes scores"; F-S4-01b piece 2b)."""

import ast
import json
from pathlib import Path

import pandas as pd
import pytest

import bv_saved

ROOT = Path(__file__).resolve().parents[1]

ROWS = [
    {"pool": "1DTE+", "side": "CALL", "strike": 350.0, "expiry": "2026-10-19", "dte": 4,
     "last": 0.57, "volume": 1500, "openInterest": 400, "dVol": 300, "Value_Score": 0.3438,
     "Status": "⭐ BEST VALUE", "iv": 0.24, "delta": 0.21, "bid": 0.55, "ask": 0.59,
     "extrinsic": 0.57, "Strategy_Tag": "", "Optimal_Strategy": "LONG CALL",
     "multipliers": {"news_with": 1.2}},
    {"pool": "1DTE+", "side": "CALL", "strike": 342.5, "expiry": "2026-10-16", "dte": 1,
     "last": 2.01, "volume": 900, "openInterest": 800, "dVol": 100, "Value_Score": 0.3425,
     "Status": "", "iv": None, "delta": None},
]


def _payload(rows=ROWS, inputs=None):
    bv = {"rows": rows, "n_scored": len(rows)}
    if inputs is not None:
        bv["inputs"] = inputs
    return {"spot": 344.0, "timestamp": "2026-10-15T16:30:32-04:00", "best_value": bv,
            "volume": {"top_calls": [], "top_puts": []}}


# ── nothing under ui/ scores ────────────────────────────────────────────────

def test_no_ui_module_imports_or_calls_the_scorer():
    offenders = []
    for path in (ROOT / "ui").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.Attribute):
                names = [node.attr]
            elif isinstance(node, ast.Name):
                names = [node.id]
            if {"build_best_value_df", "_build_best_value_df", "calculate_best_value"} & set(names):
                offenders.append(str(path.relative_to(ROOT)))
    assert not offenders, sorted(set(offenders))


# ── bv_saved ────────────────────────────────────────────────────────────────

def test_saved_rows_are_a_dataframe_with_nan_not_zero_for_missing():
    df = bv_saved.saved_rows_df(_payload())
    assert list(df["strike"]) == [350.0, 342.5]
    assert pd.isna(df.loc[1, "iv"]) and pd.isna(df.loc[1, "delta"])
    assert df.loc[1, "multipliers"] != df.loc[0, "multipliers"]
    assert bv_saved.saved_rows_df({}).empty
    assert bv_saved.saved_rows_df({"best_value": {"rows": []}}).empty


def test_input_notes_describe_what_moved_the_scores():
    notes = bv_saved.input_notes({"daily_bias": "HEAVY BULLISH", "market_state": None,
                                  "news_bias": "BULLISH"})
    assert notes == ["Daily **HEAVY BULLISH** → puts counter-trend −50%",
                     "News **BULLISH** → CALL ×1.2 · PUT ×0.8"]
    assert bv_saved.input_notes({"daily_bias": "NEUTRAL"}) == []
    assert bv_saved.input_notes(None) == [
        "Inputs not recorded for this scan (saved before 2026-10-16)"]
    assert bv_saved.saved_inputs(_payload()) is None
    assert bv_saved.saved_inputs(_payload(inputs={"news_bias": "BEARISH"})) == \
        {"news_bias": "BEARISH"}


def test_multiplier_text():
    assert bv_saved.multiplier_text({"news_with": 1.2, "macro_against": 0.3}) == \
        "macro_against ×0.3 · news_with ×1.2"
    assert bv_saved.multiplier_text({}) == "" and bv_saved.multiplier_text(None) == ""


# ── readers ─────────────────────────────────────────────────────────────────

@pytest.fixture
def no_scoring(monkeypatch):
    import best_value

    def boom(*a, **k):
        raise AssertionError("the UI must not score")
    monkeypatch.setattr(best_value, "build_best_value_df", boom)
    monkeypatch.setattr(best_value, "calculate_best_value", boom, raising=False)


def test_settings_send_shows_the_saved_top_pick(no_scoring):
    import ui.telegram_push as tg
    msg = tg._format_scan_message(payload=_payload(), prev_payload=None, ticker="AAPL",
                                  top_n=5, include={"best_value": True})
    assert "1DTE+</b>  CALL $350.0  2026-10-19 (4d)  score 0.34" in msg
    old = tg._format_scan_message(payload=_payload(rows=[]), prev_payload=None,
                                  ticker="AAPL", top_n=5, include={"best_value": True})
    assert "no saved ranking in this scan" in old


def test_settings_plan_candidates_are_the_saved_rows(no_scoring):
    import ui.telegram_push as tg
    plan = tg.plan_for_message(_payload(), None, "AAPL")
    assert plan is not None


def test_portfolio_prices_come_from_the_scan_quotes():
    from ui.pages.flow import quotes_for_portfolio
    vol = {"top_calls": [{"strike": 350, "expiry": "2026-10-19", "lastPrice": 0.57},
                         {"strike": 345, "expiry": "2026-10-16", "lastPrice": 0}],
           "top_puts": [{"strike": "bad"}]}
    df = quotes_for_portfolio("aapl", vol, {("CALL", 350.0, "2026-10-19"): 0.05})
    assert list(df["Strike"]) == [350.0, 345.0]
    assert df.loc[0, "Current_Price"] == 0.57 and pd.isna(df.loc[1, "Current_Price"])
    assert list(df["Score_Velocity"]) == [0.05, 0.0] and set(df["Ticker"]) == {"AAPL"}


def test_best_value_page_shows_the_saved_rows_and_their_inputs(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    payload = _payload(inputs={"daily_bias": "NEUTRAL", "market_state": None,
                               "news_bias": "BULLISH"})
    path = tmp_path / "p.json"
    path.write_text(json.dumps(payload))

    def page(path_str):
        import json as _j

        import best_value
        import best_value_archive
        import pre_trade_check
        from ui.pages import flow

        def boom(*a, **k):
            raise AssertionError("the UI must not score")
        best_value.build_best_value_df = boom
        flow.log_best_value_run = lambda *a, **k: True
        flow.ensure_archive_loaded = lambda: None
        pre_trade_check.store_scan_snapshot = lambda snap: None
        p = _j.loads(open(path_str).read())
        flow._render_best_value_panel(p["volume"], p["spot"], None, ticker="AAPL",
                                      run_timestamp=p["timestamp"], top_n=5,
                                      saved_payload=p)

    at = AppTest.from_function(page, args=(str(path),), default_timeout=30).run()
    assert not at.exception, at.exception
    text = " ".join(m.value for m in at.caption) + " ".join(m.value for m in at.markdown)
    assert "News **BULLISH** → CALL ×1.2" in text
    # The summary callout names each pool's saved top pick.
    callout = " ".join(x.value for x in at.success)
    assert "1DTE+" in callout and "350.0" in callout and "0.34" in callout
