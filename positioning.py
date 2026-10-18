"""positioning — today's volume next to existing open interest, per strike.

Volume far above open interest at a strike means new positions are likely being opened
there today (confirmed or not by tomorrow's OI change). Pure: no network, no Streamlit.
Input is ``volume_history.latest_scan`` (contracts that have traded today), so a strike
with open interest but no volume yet is absent, never shown as 0.
"""

from __future__ import annotations

import pandas as pd

COLS = ["call_volume", "call_oi", "put_volume", "put_oi"]


def by_strike(latest: pd.DataFrame, expiry: str, *, spot: float | None = None,
              n_strikes: int | None = None) -> pd.DataFrame:
    """Strikes (ascending) x call/put volume and OI for one expiry. ``n_strikes`` keeps the
    N strikes nearest spot. Missing values stay NaN."""
    if latest is None or latest.empty:
        return pd.DataFrame(columns=COLS)
    t = latest[latest["expiry"].astype(str).str[:10] == str(expiry)[:10]]
    if t.empty:
        return pd.DataFrame(columns=COLS)
    out = pd.DataFrame(index=sorted(t["strike"].astype(float).unique()), columns=COLS,
                       dtype=float)
    for side, pre in (("CALL", "call"), ("PUT", "put")):
        s = t[t["side"].str.upper() == side].groupby("strike")
        out[f"{pre}_volume"] = s["volume"].sum(min_count=1)
        out[f"{pre}_oi"] = s["open_interest"].sum(min_count=1)
    if n_strikes and spot:
        keep = sorted(out.index, key=lambda k: (abs(k - float(spot)), k))[: int(n_strikes)]
        out = out.loc[sorted(keep)]
    out.index.name = "strike"
    return out


def new_positioning(table: pd.DataFrame, *, ratio: float = 1.0,
                    min_volume: float = 500) -> pd.DataFrame:
    """Strike/side rows where today's volume is at least ``ratio`` x open interest and at
    least ``min_volume``: likely opening, to be confirmed by tomorrow's OI change."""
    rows = []
    for side, pre in (("CALL", "call"), ("PUT", "put")):
        v, oi = table[f"{pre}_volume"], table[f"{pre}_oi"]
        ok = v.notna() & (v >= min_volume) & ((oi.isna()) | (v >= ratio * oi.clip(lower=1)))
        for k in table.index[ok]:
            rows.append({"side": side, "strike": float(k), "volume": float(v[k]),
                         "open_interest": None if pd.isna(oi[k]) else float(oi[k]),
                         "vol_oi": None if pd.isna(oi[k]) or oi[k] <= 0 else float(v[k] / oi[k])})
    return pd.DataFrame(rows, columns=["side", "strike", "volume", "open_interest", "vol_oi"])
