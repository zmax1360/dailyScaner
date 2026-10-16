"""Best Value is scored once per scan; the archive snapshot and the attribution rows
come from that one result (F-S4-01b, piece 1)."""

from pathlib import Path

import pandas as pd
import pytest

import dailyScaner as ds

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def fakes(monkeypatch):
    import best_value
    import news_service

    calls = []
    scored = pd.DataFrame({"side": ["CALL"], "strike": [350.0], "Value_Score": [0.34]})

    def build(vol_curr, spot, vol_prev, **kw):
        calls.append(kw)
        return scored

    monkeypatch.setattr(best_value, "build_best_value_df", build)
    monkeypatch.setattr(best_value, "resolve_biases_for_ticker",
                        lambda t, s, p: ("HEAVY BULLISH", "BULLISH TAILWIND"))
    monkeypatch.setattr(news_service, "get_news_sentiment",
                        lambda t: {"news_bias": "BEARISH"})
    return calls, scored


def _score():
    return ds._score_best_value(ticker="AAPL", spot=344.0,
                                vol_curr={"top_calls": [], "top_puts": []},
                                vol_prev=None, session={})


def test_one_scoring_with_daily_macro_and_news(fakes):
    calls, scored = fakes
    out = _score()
    assert len(calls) == 1
    kw = calls[0]
    assert kw["daily_bias"] == "HEAVY BULLISH"
    assert kw["market_state"] == "BULLISH TAILWIND"
    assert kw["news_bias"] == "BEARISH"
    assert kw["min_volume"] == 500
    assert out["df"] is scored
    assert (out["daily_bias"], out["market_state"], out["news_bias"]) == \
        ("HEAVY BULLISH", "BULLISH TAILWIND", "BEARISH")


def test_news_failure_still_scores_without_news(fakes, monkeypatch):
    import news_service
    calls, _ = fakes

    def boom(t):
        raise RuntimeError("finnhub down")
    monkeypatch.setattr(news_service, "get_news_sentiment", boom)
    out = _score()
    assert out["news_bias"] is None and calls[0]["news_bias"] is None
    assert not out["df"].empty


def test_scoring_failure_is_fail_soft(fakes, monkeypatch):
    import best_value

    def boom(*a, **k):
        raise ValueError("bad chain")
    monkeypatch.setattr(best_value, "build_best_value_df", boom)
    out = _score()
    assert out["df"].empty and out["daily_bias"] == "HEAVY BULLISH"


def test_attribution_logs_the_given_scoring_and_never_scores_again(monkeypatch):
    import attribution
    import best_value

    def no_rescore(*a, **k):
        raise AssertionError("attribution must not score again")
    monkeypatch.setattr(best_value, "build_best_value_df", no_rescore)
    logged = {}
    monkeypatch.setattr(attribution, "log_run",
                        lambda **kw: logged.update(kw) or "run-123456789")
    monkeypatch.setattr(attribution, "build_control_rows_per_pool", lambda chain, spot: [])
    monkeypatch.setattr(attribution, "engine_sha", lambda: "sha")
    df = pd.DataFrame({"side": ["CALL"], "Value_Score": [0.34]})
    chain = pd.DataFrame({"strike": [350.0]})
    ds._log_scan_attribution(
        ticker="AAPL", spot=344.0, calls_all=chain, puts_all=chain,
        scored={"df": df, "daily_bias": "NEUTRAL", "market_state": None,
                "news_bias": "BULLISH"})
    assert logged["scored_df"] is df
    assert (logged["daily_bias"], logged["market_state"], logged["news_bias"]) == \
        ("NEUTRAL", None, "BULLISH")


def test_the_scan_scores_in_exactly_one_place():
    src = (ROOT / "dailyScaner.py").read_text()
    assert src.count("build_best_value_df(") == 1
    assert src.count("_score_best_value(") == 2          # definition + the one call
    assert '"inputs"' in src                             # the snapshot records its inputs
