"""news_moves — unusual stock moves, the headlines just before them, and whether they faded.

Built for one question: when the stock jumps on news, does the jump hold or give back
(a fade after an up-spike is a put setup)? Starting from the price is deliberate: a
ticker like AAPL gets dozens of headlines a day, mostly rewrites, so the moves pick the
headlines rather than the other way round.

Pure: no network, no Streamlit, no writes. Display / analysis only.
Bars: 5-minute OHLCV with a naive New York wall-clock index, extended hours included
(``YahooSource.fetch_extended_history``). Missing data is NaN, never 0.

A move is a 5-minute bar whose |return| is at least ``k`` times the median |return| of
the previous ``base_bars`` bars and at least ``min_pct`` percent; back-to-back bars in
the same direction are one move. "Retrace" is how much of the move was given back
later: 0 = held, 1 = all of it, above 1 = reversed past the start. A move "faded" when
it gave back at least half.
"""

from __future__ import annotations

from datetime import time, timedelta

import pandas as pd

PRE, REGULAR, POST = "pre", "regular", "post"
OPEN, CLOSE = time(9, 30), time(16, 0)
HORIZONS = (15, 30, 60)            # minutes after the move
FADE_AT = 0.5                      # gave back at least half
BAR = timedelta(minutes=5)


def session_of(ts: pd.Timestamp) -> str:
    t = ts.time()
    if t < OPEN:
        return PRE
    if t < CLOSE:
        return REGULAR
    return POST


def _price_at(close: pd.Series, when: pd.Timestamp) -> float | None:
    """Close of the last bar that ended by ``when`` (bar start <= when - 5m); None
    when the data does not reach that far, or the bar is on another day."""
    if close.empty or close.index[-1] + BAR < when:
        return None
    s = close[close.index <= when - BAR]
    if s.empty or s.index[-1].date() != when.date():
        return None
    v = s.iloc[-1]
    return None if pd.isna(v) else float(v)


def _regular_close(close: pd.Series, day) -> float | None:
    s = close[(close.index.date == day) & (close.index.time < CLOSE)
              & (close.index.time >= OPEN)]
    return None if s.empty else float(s.iloc[-1])


def find_moves(bars: pd.DataFrame, *, k: float = 3.0, min_pct: float = 0.25,
               base_bars: int = 48) -> pd.DataFrame:
    """One row per unusual move: start/end bar, session, size, and what happened next."""
    cols = ["start", "end", "session", "direction", "before", "after", "pct",
            "ret_15", "ret_30", "ret_60", "retrace_15", "retrace_30", "retrace_60",
            "retrace_close", "faded_60", "faded_close"]
    if bars is None or bars.empty or "Close" not in bars.columns:
        return pd.DataFrame(columns=cols)
    close = bars["Close"].astype(float).sort_index()
    r = close.pct_change() * 100.0
    typical = r.abs().rolling(base_bars, min_periods=12).median().shift(1)
    flag = (r.abs() >= (k * typical).clip(lower=min_pct)) & typical.notna()

    moves, cur = [], None
    for ts, f in flag.items():
        if not f:
            cur = None
            continue
        sign = 1 if r[ts] > 0 else -1
        if cur is not None and cur["sign"] == sign and ts - cur["end"] <= BAR:
            cur["end"] = ts
            continue
        cur = {"start": ts, "end": ts, "sign": sign}
        moves.append(cur)

    rows = []
    idx = list(close.index)
    for m in moves:
        i0 = idx.index(m["start"])
        if i0 == 0:
            continue
        before, after = float(close.iloc[i0 - 1]), float(close[m["end"]])
        if before <= 0 or after == before:
            continue
        end_t = m["end"] + BAR                       # the move is over when its bar closes
        row = {"start": m["start"], "end": m["end"], "session": session_of(m["start"]),
               "direction": "up" if m["sign"] > 0 else "down", "before": before,
               "after": after, "pct": (after / before - 1) * 100.0}
        for h in HORIZONS:
            p = _price_at(close, end_t + timedelta(minutes=h))
            row[f"ret_{h}"] = None if p is None else (p / after - 1) * 100.0
            row[f"retrace_{h}"] = None if p is None else (after - p) / (after - before)
        pc = _regular_close(close, m["end"].date()) if session_of(m["end"]) != POST else None
        row["retrace_close"] = None if pc is None else (after - pc) / (after - before)
        row["faded_60"] = None if row["retrace_60"] is None else row["retrace_60"] >= FADE_AT
        row["faded_close"] = (None if row["retrace_close"] is None
                              else row["retrace_close"] >= FADE_AT)
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def attach_news(moves: pd.DataFrame, news: pd.DataFrame, *, before_min: int = 30,
                after_min: int = 5) -> pd.DataFrame:
    """Adds ``headlines`` (list of str) and ``with_news`` to each move: headlines published
    from ``before_min`` minutes before the move started to ``after_min`` after it ended
    (news services stamp articles a little late)."""
    out = moves.copy()
    lists = []
    for _, m in out.iterrows():
        if news is None or news.empty:
            lists.append([])
            continue
        lo = m["start"] - timedelta(minutes=before_min)
        hi = m["end"] + BAR + timedelta(minutes=after_min)
        hit = news[(news["ts_et"] >= lo) & (news["ts_et"] <= hi)]
        lists.append(list(hit["headline"]))
    out["headlines"] = lists
    out["with_news"] = [bool(x) for x in lists]
    return out


def headline_reactions(news: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """For each headline: the price when it came out and the change 5, 15 and 60 minutes
    later (percent). NaN where the bars do not cover it (overnight, weekend)."""
    cols = ["ts_et", "session", "headline", "source", "price", "chg_5", "chg_15", "chg_60"]
    if news is None or news.empty or bars is None or bars.empty:
        return pd.DataFrame(columns=cols)
    close = bars["Close"].astype(float).sort_index()
    rows = []
    for _, n in news.iterrows():
        t = pd.Timestamp(n["ts_et"])
        p0 = _price_at(close, t)
        row = {"ts_et": t, "session": session_of(t), "headline": n["headline"],
               "source": n.get("source", ""), "price": p0}
        for h in (5, 15, 60):
            p = _price_at(close, t + timedelta(minutes=h))
            row[f"chg_{h}"] = None if p is None or p0 is None else (p / p0 - 1) * 100.0
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def fade_stats(moves: pd.DataFrame) -> pd.DataFrame:
    """How often moves faded, by direction and by whether news came with them."""
    cols = ["direction", "with_news", "moves", "faded_60", "faded_close", "avg_pct"]
    if moves is None or moves.empty:
        return pd.DataFrame(columns=cols)
    rows = []
    for (d, w), g in moves.groupby(["direction", "with_news"]):
        f60 = g["faded_60"].dropna()
        fc = g["faded_close"].dropna()
        rows.append({"direction": d, "with_news": bool(w), "moves": int(len(g)),
                     "faded_60": None if f60.empty else float(f60.astype(bool).mean()),
                     "faded_close": None if fc.empty else float(fc.astype(bool).mean()),
                     "avg_pct": float(g["pct"].mean())})
    return pd.DataFrame(rows, columns=cols)
