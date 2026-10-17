"""oi_delta — day-over-day change in open interest per contract, from recorded scans.

Open interest is settled overnight: the OI a chain reports during session D is the
position count after session D-1's trading. So the change from session D-1's OI to
session D's OI is what D-1's trading did — contracts opened (OI up) or closed (OI down).
Without trade-side data this is the one free read on whether volume opened positions.

Pure: no network, no Streamlit, no writes. Display only; not a scoring input.
Input is ``volume_history.daily_summary`` (last scan per contract per session).
A contract missing on either day (it did not trade that day, so it was not recorded)
is left out, never counted as 0 — a missing OI is unknown, not zero.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

KEY = ["side", "strike", "expiry"]
TABLE_COLS = ["side", "strike", "expiry", "dte", "oi_before", "oi_after", "oi_change",
              "oi_change_pct", "traded", "opened_share", "suspect"]


def sessions(daily: pd.DataFrame) -> list[str]:
    """Recorded session dates, oldest first."""
    if daily is None or daily.empty:
        return []
    return sorted(str(s) for s in daily["session_date"].dropna().unique())


def change_table(daily: pd.DataFrame, *, session: str | None = None) -> tuple[pd.DataFrame, dict]:
    """OI change into ``session`` (default: the latest) from the session before it.

    Returns (table, meta). ``meta`` = {"session", "previous"}; the trading that moved
    OI happened on ``previous``. Columns:
      oi_before / oi_after   OI reported on ``previous`` / on ``session``
      oi_change, oi_change_pct (NaN when oi_before is 0)
      traded                 the contract's final volume on ``previous``
      opened_share           oi_change / traded: how much of that day's volume added to
                             open interest (NaN without volume). Near +1: mostly opening;
                             near 0: traded and closed or day trades; negative: closing.
      suspect                |change| is larger than that day's volume, which one day's
                             trading cannot do: one of the two OI readings is wrong (the
                             source's OI is sometimes stale or revised). Shown, but flagged.
    Expiries before ``session`` are dropped.
    """
    days = sessions(daily)
    empty = pd.DataFrame(columns=TABLE_COLS)
    if len(days) < 2:
        return empty, {"session": days[-1] if days else None, "previous": None}
    cur = session if session in days else days[-1]
    i = days.index(cur)
    if i == 0:
        return empty, {"session": cur, "previous": None}
    prev = days[i - 1]
    d = daily.copy()
    d["session_date"] = d["session_date"].astype(str)
    a = d[d["session_date"] == prev][KEY + ["open_interest", "volume"]]
    b = d[d["session_date"] == cur][KEY + ["open_interest"]]
    m = a.merge(b, on=KEY, suffixes=("_before", "_after"))
    m = m.rename(columns={"open_interest_before": "oi_before", "open_interest_after": "oi_after",
                          "volume": "traded"})
    m = m[m["oi_before"].notna() & m["oi_after"].notna()]
    cur_d = date.fromisoformat(cur)
    m["dte"] = [(date.fromisoformat(str(e)[:10]) - cur_d).days for e in m["expiry"]]
    m = m[m["dte"] >= 0]
    m["oi_change"] = m["oi_after"] - m["oi_before"]
    m["oi_change_pct"] = (m["oi_change"] / m["oi_before"].where(m["oi_before"] > 0)) * 100.0
    m["opened_share"] = m["oi_change"] / m["traded"].where(m["traded"] > 0)
    m["suspect"] = m["traded"].notna() & (m["oi_change"].abs() > m["traded"])
    m = m.reindex(columns=TABLE_COLS)
    m = m.sort_values("oi_change", key=lambda s: s.abs(), ascending=False, ignore_index=True)
    return m, {"session": cur, "previous": prev}


def filtered(table: pd.DataFrame, *, spot: float | None, max_dte: int | None = None,
             band_pct: float | None = None, min_change: float = 0.0) -> pd.DataFrame:
    """Rows within ``max_dte`` days, strikes within ``band_pct`` % of spot, and
    |change| >= ``min_change``."""
    if table is None or table.empty:
        return table
    t = table
    if max_dte is not None:
        t = t[t["dte"] <= int(max_dte)]
    if band_pct is not None and spot:
        t = t[(t["strike"] - float(spot)).abs() <= float(spot) * float(band_pct) / 100.0]
    if min_change:
        t = t[t["oi_change"].abs() >= float(min_change)]
    return t


def by_strike(table: pd.DataFrame) -> pd.DataFrame:
    """Net OI change per strike, calls and puts side by side (summed over expiries),
    strikes descending. A strike/side with no matched contract is NaN, not 0."""
    if table is None or table.empty:
        return pd.DataFrame(columns=["CALL", "PUT"])
    p = table.pivot_table(index="strike", columns="side", values="oi_change",
                          aggfunc=lambda x: x.sum(min_count=1))
    return p.reindex(columns=["CALL", "PUT"]).sort_index(ascending=False)


def headline(table: pd.DataFrame) -> dict[str, Any]:
    """Totals for the rows given: net call and put OI change, contracts matched, and the
    largest single increase on each side. Suspect rows are counted but not summed."""
    out: dict[str, Any] = {"contracts": 0, "suspect": 0, "call_change": None,
                           "put_change": None, "top_call": None, "top_put": None}
    if table is None or table.empty:
        return out
    out["contracts"] = int(len(table))
    out["suspect"] = int(table["suspect"].sum())
    table = table[~table["suspect"]]          # totals and top adds leave suspect rows out
    for side, key in (("CALL", "call"), ("PUT", "put")):
        t = table[table["side"] == side]
        out[f"{key}_change"] = float(t["oi_change"].sum()) if not t.empty else None
        up = t[t["oi_change"] > 0]
        if not up.empty:
            r = up.loc[up["oi_change"].idxmax()]
            out[f"top_{key}"] = {"strike": float(r["strike"]), "expiry": str(r["expiry"]),
                                 "oi_change": float(r["oi_change"])}
    return out
