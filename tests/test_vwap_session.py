"""VWAP uses regular-session bars only, and says so when the opening bar's volume looks
missing. On Oct 15 the Game Plan showed VWAP $344.37 while a regular-hours chart showed
$344.99, and the plan put a calls stop "under VWAP" with price actually below it."""

import pandas as pd
import pytest

import volume_analysis as va


def _day(date: str, *, open_volume: float = 900_000, pre_post: bool = False) -> pd.DataFrame:
    """One session of naive-ET 5-minute bars: a high, heavy open, then a slow drift."""
    times = pd.date_range(f"{date} 09:30", f"{date} 15:55", freq="5min")
    n = len(times)
    close = [347.0 - 3.0 * i / n for i in range(n)]
    vol = [open_volume] + [300_000.0] * (n - 1)
    df = pd.DataFrame({"Open": close, "High": [c + 0.3 for c in close],
                       "Low": [c - 0.3 for c in close], "Close": close, "Volume": vol},
                      index=times)
    if pre_post:
        pre = pd.date_range(f"{date} 04:00", f"{date} 09:25", freq="5min")
        post = pd.date_range(f"{date} 16:00", f"{date} 19:55", freq="5min")
        extra = pd.DataFrame({"Open": 340.0, "High": 340.2, "Low": 339.8, "Close": 340.0,
                              "Volume": 400_000.0}, index=pre.append(post))
        df = pd.concat([df, extra]).sort_index()
    return df


class _Source:
    name = "fake"

    def __init__(self, bars):
        self.bars = bars

    def fetch_history(self, ticker, *, interval, period):
        return self.bars.copy()


def _rth_vwap(df):
    tp = (df["High"] + df["Low"] + df["Close"]) / 3
    return float((tp * df["Volume"]).sum() / df["Volume"].sum())


def test_regular_session_only_drops_pre_and_after_hours():
    df = _day("2026-10-15", pre_post=True)
    rth = va.regular_session_only(df)
    assert rth.index.min().strftime("%H:%M") == "09:30"
    assert rth.index.max().strftime("%H:%M") == "15:55"
    assert len(rth) == 78


def test_extended_hours_bars_do_not_move_vwap():
    """A source that sends pre-market and after-hours bars (Massive does) gives the same
    VWAP as one that sends the regular session only."""
    plain = _day("2026-10-15")
    with_ext = _day("2026-10-15", pre_post=True)
    a = va.fetch_intraday_vwap_df("AAPL", timeframe="5M", source=_Source(plain))
    b = va.fetch_intraday_vwap_df("AAPL", timeframe="5M", source=_Source(with_ext))
    assert len(a) == len(b) == 78
    assert b["VWAP"].iloc[-1] == pytest.approx(a["VWAP"].iloc[-1])
    assert a["VWAP"].iloc[-1] == pytest.approx(_rth_vwap(plain))


def test_timezone_aware_bars_are_read_in_et():
    aware = _day("2026-10-15", pre_post=True).tz_localize("America/New_York")
    out = va.fetch_intraday_vwap_df("AAPL", timeframe="5M", source=_Source(aware))
    assert len(out) == 78


def test_each_session_starts_its_own_vwap():
    two = pd.concat([_day("2026-10-14"), _day("2026-10-15")])
    out = va.fetch_intraday_vwap_df("AAPL", timeframe="5M", last_session_only=False,
                                    source=_Source(two))
    first_bar_15 = out.loc["2026-10-15 09:30"]
    assert first_bar_15["VWAP"] == pytest.approx(
        (first_bar_15["High"] + first_bar_15["Low"] + first_bar_15["Close"]) / 3)


def test_missing_opening_volume_lowers_vwap_and_is_flagged():
    """The suspected Oct 15 cause: the heavy, high-priced opening bar arrives with no
    volume, and VWAP drops below the true value."""
    good = va.fetch_intraday_vwap_df("AAPL", timeframe="5M", source=_Source(_day("2026-10-15")))
    bad = va.fetch_intraday_vwap_df(
        "AAPL", timeframe="5M", source=_Source(_day("2026-10-15", open_volume=0)))
    assert bad["VWAP"].iloc[-1] < good["VWAP"].iloc[-1]
    assert good.attrs["vwap_warning"] is None
    assert bad.attrs["vwap_warning"] == va.OPEN_BAR_WARNING


def test_opening_bar_check_needs_enough_bars_and_the_0930_bar():
    early = _day("2026-10-15", open_volume=0).iloc[:5]
    assert va.opening_bar_warning(early) is None
    late_start = _day("2026-10-15", open_volume=0).iloc[1:]
    assert va.opening_bar_warning(late_start) is None
    assert va.opening_bar_warning(pd.DataFrame()) is None


def test_vwap_state_carries_the_warning(monkeypatch):
    bad = _day("2026-10-15", open_volume=0)
    monkeypatch.setattr(va, "_resolve_source", lambda source=None: _Source(bad))
    state = va.get_intraday_vwap_state("AAPL")
    assert state["VWAP_warning"] == va.OPEN_BAR_WARNING
    good = _day("2026-10-15")
    monkeypatch.setattr(va, "_resolve_source", lambda source=None: _Source(good))
    assert va.get_intraday_vwap_state("AAPL")["VWAP_warning"] is None


def test_daily_bars_are_not_filtered():
    days = pd.bdate_range("2026-04-01", "2026-10-02")
    df = pd.DataFrame({"Open": 330.0, "High": 331.0, "Low": 329.0, "Close": 330.0,
                       "Volume": 1e6}, index=days)
    out = va.fetch_intraday_vwap_df("AAPL", timeframe="1D", source=_Source(df))
    assert len(out) == len(df)
