"""News & Moves: move finder and fade stats (news_moves.py), saved headlines
(news_history.py), extended-hours bars, and the page."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import news_history as nh
import news_moves as nm

ET = ZoneInfo("America/New_York")
DAY = "2026-10-14"


def _bars(events):
    """5-minute bars 04:00-19:55 on DAY around 100 with tiny noise; ``events`` maps a
    'HH:MM' bar to a price path override applied from that bar on."""
    idx = pd.date_range(f"{DAY} 04:00", f"{DAY} 19:55", freq="5min")
    rng = np.random.default_rng(0)
    close = 100 + np.cumsum(rng.normal(0, 0.01, len(idx)))
    s = pd.Series(close, index=idx)
    for start, path in events.items():
        i = idx.get_loc(pd.Timestamp(f"{DAY} {start}"))
        base = s.iloc[i - 1]
        for j, step in enumerate(path):
            s.iloc[i + j] = base * (1 + step / 100)
        e = i + len(path)
        if e < len(s):                       # the rest of the day continues from the path
            s.iloc[e:] = s.iloc[e:] - s.iloc[e] + s.iloc[e - 1]
    return pd.DataFrame({"Open": s.shift(1).fillna(s), "High": s + 0.02, "Low": s - 0.02,
                         "Close": s, "Volume": 1000.0}, index=idx)


SPIKE_FADES = {"10:00": [1.0, 0.8, 0.6, 0.4, 0.2, 0.1, 0.05]}     # +1% then gives it back
DROP_HOLDS = {"13:00": [-1.0] + [-1.0] * 20}                       # -1% and stays there
PRE_SPIKE = {"08:00": [0.8] + [0.8] * 5}


def test_finds_each_move_with_session_direction_and_size():
    m = nm.find_moves(_bars({**SPIKE_FADES, **DROP_HOLDS, **PRE_SPIKE}))
    got = {(r.session, r.direction): r for r in m.itertuples()}
    assert set(got) == {("pre", "up"), ("regular", "up"), ("regular", "down")}
    assert got[("regular", "up")].pct == pytest.approx(1.0, abs=0.05)
    assert got[("regular", "down")].pct == pytest.approx(-1.0, abs=0.05)


def test_a_spike_that_gives_it_back_faded_and_a_drop_that_holds_did_not():
    m = nm.find_moves(_bars({**SPIKE_FADES, **DROP_HOLDS}))
    up = m[m["direction"] == "up"].iloc[0]
    down = m[m["direction"] == "down"].iloc[0]
    assert up["retrace_60"] > 0.8 and up["faded_60"] and up["faded_close"]
    assert abs(down["retrace_60"]) < 0.1 and not down["faded_60"]


def test_no_outcome_where_the_bars_end_and_no_moves_in_quiet_data():
    m = nm.find_moves(_bars({"19:45": [1.0, 1.0, 1.0]}))
    late = m[m["start"] == pd.Timestamp(f"{DAY} 19:45")].iloc[0]
    assert late["ret_60"] is None or math.isnan(late["ret_60"])
    assert late["retrace_close"] is None or math.isnan(late["retrace_close"])   # after-hours
    assert nm.find_moves(_bars({})).empty
    assert nm.find_moves(pd.DataFrame()).empty


def test_headlines_just_before_a_move_are_attached():
    m = nm.find_moves(_bars({**SPIKE_FADES, **DROP_HOLDS}))
    news = pd.DataFrame({"ts_et": [pd.Timestamp(f"{DAY} 09:40"), pd.Timestamp(f"{DAY} 11:30")],
                         "headline": ["Apple announces Duo", "Unrelated later story"],
                         "source": ["x", "y"]})
    out = nm.attach_news(m, news)
    up = out[out["direction"] == "up"].iloc[0]
    assert up["headlines"] == ["Apple announces Duo"] and up["with_news"]
    assert not out[out["direction"] == "down"].iloc[0]["with_news"]


def test_fade_stats_split_by_direction_and_news():
    m = nm.attach_news(nm.find_moves(_bars({**SPIKE_FADES, **DROP_HOLDS})),
                       pd.DataFrame({"ts_et": [pd.Timestamp(f"{DAY} 09:55")],
                                     "headline": ["h"], "source": ["s"]}))
    s = nm.fade_stats(m).set_index(["direction", "with_news"])
    assert s.loc[("up", True), "faded_60"] == 1.0
    assert s.loc[("down", False), "faded_60"] == 0.0
    assert nm.fade_stats(pd.DataFrame()).empty


def test_headline_reaction_measures_from_the_price_when_it_came_out():
    bars = _bars(SPIKE_FADES)
    news = pd.DataFrame({"ts_et": [pd.Timestamp(f"{DAY} 10:00")], "headline": ["h"],
                         "source": ["s"]})
    r = nm.headline_reactions(news, bars).iloc[0]
    assert r["session"] == "regular" and r["chg_5"] == pytest.approx(1.0, abs=0.05)
    night = pd.DataFrame({"ts_et": [pd.Timestamp(f"{DAY} 23:00")], "headline": ["h"],
                          "source": ["s"]})
    assert nm.headline_reactions(night, bars).iloc[0]["price"] is None


def test_news_history_stores_once_skips_untimed_and_loads_in_new_york_time(tmp_path):
    db = str(tmp_path / "n.db")
    t = int(datetime(2026, 10, 14, 13, 40, tzinfo=ZoneInfo("UTC")).timestamp())  # 09:40 ET
    arts = [{"headline": "A", "datetime": t, "source": "s", "url": "u"},
            {"headline": "A", "datetime": t}, {"headline": "B", "datetime": 0}]
    assert nh.store("aapl", arts, db_path=db) == 1
    assert nh.store("AAPL", arts, db_path=db) == 0
    got = nh.load("AAPL", datetime(2026, 10, 14, tzinfo=ET), datetime(2026, 10, 15, tzinfo=ET),
                  db_path=db)
    assert list(got["headline"]) == ["A"]
    assert got.iloc[0]["ts_et"] == pd.Timestamp("2026-10-14 09:40")
    assert nh.load("AAPL", datetime(2026, 1, 1, tzinfo=ET), datetime(2026, 1, 2, tzinfo=ET),
                   db_path=str(tmp_path / "missing.db")).empty


def test_backfill_asks_for_the_date_range_and_stores(tmp_path):
    seen = {}

    def fake(ticker, start, end):
        seen.update(ticker=ticker, start=start, end=end)
        return [{"headline": "X", "datetime": 1_790_000_000}]
    n = nh.backfill("AAPL", 30, fetch=fake, db_path=str(tmp_path / "n.db"),
                    today=date(2026, 10, 18))
    assert n == 1 and seen == {"ticker": "AAPL", "start": date(2026, 9, 18),
                               "end": date(2026, 10, 18)}


def test_fetch_news_range_without_a_key_is_empty(monkeypatch):
    import news_service
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert news_service.fetch_news_range("AAPL", "2026-10-01", "2026-10-05") == []


def test_extended_history_asks_yahoo_for_pre_and_post_market(monkeypatch):
    from sources import yahoo
    calls = {}

    class FakeTicker:
        def __init__(self, t):
            pass

        def history(self, **kw):
            calls.update(kw)
            return pd.DataFrame({"Close": [1.0]},
                                index=pd.DatetimeIndex(["2026-10-14 04:00"]).tz_localize(ET))
    monkeypatch.setattr(yahoo.yf, "Ticker", FakeTicker)
    df = yahoo.YahooSource().fetch_extended_history("AAPL", period="1d")
    assert calls["prepost"] is True and calls["interval"] == "5m"
    assert df.index.tz is None and df.index[0] == pd.Timestamp("2026-10-14 04:00")


def test_page_figure_and_menu():
    from ui import shell
    from ui.pages import news_moves as page
    bars = _bars(SPIKE_FADES)
    moves = nm.attach_news(nm.find_moves(bars), pd.DataFrame(columns=["ts_et", "headline"]))
    fig = page.figure(bars, moves, pd.DataFrame(columns=["ts_et", "headline"]))
    assert len(fig.data) >= 2
    assert {e.id: e.title for e in shell.entries()}["news_moves"] == "News & Moves"
