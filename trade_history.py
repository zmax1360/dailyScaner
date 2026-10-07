"""trade_history — closed trades and results from a broker activity export.

Reads the Wealthsimple "activities export" CSV (one row per fill, option symbols in OCC
form). Pure: pandas only, no network, no Streamlit, no writes. Display and analysis
only; nothing here feeds scoring.

- Buys are matched to sells first-in, first-out per contract symbol.
- An OptionExpiry row closes the remaining lots at zero.
- Everything is reported in US dollars. A row booked in CAD is converted with the FX
  rate printed on that row; a CAD row with no rate is left out and counted.
- The account number is never read into the result.
"""

from __future__ import annotations

import re
from typing import Any

import numpy as np
import pandas as pd

from scoring_pool import POOL_0DTE, POOL_1DTE

REQUIRED = ["effective_date", "effective_time", "activity_type", "activity_sub_type",
            "description", "symbol", "currency", "quantity", "net_cash_amount"]
TRADE_COLS = ["symbol", "underlying", "cp", "strike", "expiry", "entry", "exit", "qty",
              "cost", "proceeds", "pnl", "ret", "hold_min", "dte", "pool", "premium",
              "expired"]

_OCC = re.compile(r"^\s*([A-Z.]+)\s+(\d{6})([CP])(\d{8})\s*$")
_FX = re.compile(r"FX Rate:\s*([\d.]+)")
_EXPIRED = re.compile(r"Expired\s+(\d+)")

HOLD_BINS = [0, 5, 15, 30, 60, 240, float("inf")]
HOLD_LABELS = ["Under 5 min", "5 to 15 min", "15 to 30 min", "30 to 60 min", "1 to 4 hours",
               "Over 4 hours"]
PREMIUM_BINS = [0, 0.25, 0.60, 1.20, float("inf")]
PREMIUM_LABELS = ["Under $0.25", "$0.25 to $0.60", "$0.60 to $1.20", "Over $1.20"]
TIME_BINS = [0, 10, 11, 12, 14, 15, 24]
TIME_LABELS = ["Before 10:00", "10:00 to 11:00", "11:00 to 12:00", "12:00 to 14:00",
               "14:00 to 15:00", "15:00 to close"]


def parse_occ(symbol: Any) -> dict[str, Any] | None:
    """'AAPL  261009C00340000' -> underlying, expiry, C/P, strike. None if not an option."""
    m = _OCC.match(str(symbol or ""))
    if not m:
        return None
    return {"underlying": m.group(1), "expiry": pd.Timestamp("20" + m.group(2)),
            "cp": m.group(3), "strike": int(m.group(4)) / 1000.0}


def load_activities(path: str) -> pd.DataFrame:
    """The export as a frame, without the account columns. Raises ValueError when the file
    is not an activity export."""
    df = pd.read_csv(path)
    missing = [c for c in REQUIRED if c not in df.columns]
    if missing:
        raise ValueError(f"not an activity export: missing columns {missing}")
    return df.drop(columns=[c for c in ("account_id", "account_type") if c in df.columns])


def _usd(row: pd.Series) -> float | None:
    cash = row.get("net_cash_amount")
    if pd.isna(cash):
        return None
    if str(row.get("currency")) == "USD":
        return float(cash)
    m = _FX.search(str(row.get("description")))
    return float(cash) / float(m.group(1)) if m else None


def closed_trades(activities: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """(closed trades, notes). One row per matched lot. ``notes`` reports what could not
    be used: open lots, sells with no matching buy, and rows with no USD value."""
    notes: dict[str, Any] = {"open_contracts": 0, "unmatched_sells": 0, "skipped_rows": 0}
    if activities is None or activities.empty:
        return pd.DataFrame(columns=TRADE_COLS), notes
    a = activities[activities["activity_type"].isin(["Trade", "OptionExpiry"])].copy()
    a["ts"] = pd.to_datetime(a["effective_date"].astype(str) + " "
                             + a["effective_time"].fillna("16:00:00").astype(str),
                             errors="coerce")
    a = a.sort_values("ts", kind="stable")

    rows: list[dict[str, Any]] = []
    for symbol, g in a.groupby("symbol", sort=False):
        occ = parse_occ(symbol)
        if occ is None:
            continue                                         # shares, not an option
        lots: list[dict[str, Any]] = []

        def close(n: int, price_each: float, when, expired: bool) -> int:
            while n > 0 and lots:
                lot = lots[0]
                k = min(n, lot["q"])
                rows.append({"symbol": " ".join(str(symbol).split()), **occ,
                             "entry": lot["ts"], "exit": when, "qty": k,
                             "cost": lot["each"] * k, "proceeds": price_each * k,
                             "expired": expired})
                lot["q"] -= k
                n -= k
                if lot["q"] == 0:
                    lots.pop(0)
            return n

        for _, r in g.iterrows():
            if r["activity_type"] == "OptionExpiry":
                m = _EXPIRED.search(str(r["description"]))
                close(int(m.group(1)) if m else sum(x["q"] for x in lots), 0.0, r["ts"], True)
                continue
            qty, usd = r["quantity"], _usd(r)
            if pd.isna(qty) or qty == 0 or usd is None or pd.isna(r["ts"]):
                notes["skipped_rows"] += 1
                continue
            if qty > 0:
                lots.append({"ts": r["ts"], "q": int(qty), "each": -usd / qty})
            else:
                notes["unmatched_sells"] += close(int(-qty), usd / -qty, r["ts"], False)
        notes["open_contracts"] += sum(x["q"] for x in lots)

    if not rows:
        return pd.DataFrame(columns=TRADE_COLS), notes
    t = pd.DataFrame(rows)
    t["pnl"] = t["proceeds"] - t["cost"]
    t["ret"] = t["pnl"] / t["cost"].where(t["cost"] > 0)
    t["hold_min"] = (t["exit"] - t["entry"]).dt.total_seconds() / 60.0
    t["dte"] = (t["expiry"].dt.normalize() - t["entry"].dt.normalize()).dt.days
    t["pool"] = np.where(t["dte"] == 0, POOL_0DTE, POOL_1DTE)
    t["premium"] = t["cost"] / t["qty"] / 100.0              # per share, as quoted
    return t[TRADE_COLS].sort_values("entry").reset_index(drop=True), notes


def summary(trades: pd.DataFrame) -> dict[str, Any]:
    """Headline numbers. Empty dict when there are no closed trades."""
    if trades is None or trades.empty:
        return {}
    wins, losses = trades[trades["pnl"] > 0], trades[trades["pnl"] <= 0]
    days = trades.groupby(trades["entry"].dt.date)["pnl"].sum()
    gross_loss = -losses["pnl"].sum()
    return {
        "trades": int(len(trades)), "contracts": int(trades["qty"].sum()),
        "days": int(days.size), "pnl": float(trades["pnl"].sum()),
        "win_rate": float(len(wins) / len(trades)),
        "avg_win": float(wins["pnl"].mean()) if len(wins) else None,
        "avg_loss": float(losses["pnl"].mean()) if len(losses) else None,
        "profit_factor": float(wins["pnl"].sum() / gross_loss) if gross_loss > 0 else None,
        "median_hold_min": float(trades["hold_min"].median()),
        "green_days": int((days > 0).sum()), "red_days": int((days <= 0).sum()),
        "first": trades["entry"].min(), "last": trades["exit"].max(),
    }


def _bucket(trades: pd.DataFrame, by: str) -> pd.Series:
    if by == "pool":
        return pd.Categorical(trades["pool"], [POOL_0DTE, POOL_1DTE], ordered=True)
    if by == "side":
        return pd.Categorical(trades["cp"].map({"C": "Calls", "P": "Puts"}), ["Calls", "Puts"],
                              ordered=True)
    if by == "hold":
        return pd.cut(trades["hold_min"], HOLD_BINS, labels=HOLD_LABELS, right=False)
    if by == "premium":
        return pd.cut(trades["premium"], PREMIUM_BINS, labels=PREMIUM_LABELS, right=False)
    if by == "time":
        hour = trades["entry"].dt.hour + trades["entry"].dt.minute / 60.0
        return pd.cut(hour, TIME_BINS, labels=TIME_LABELS, right=False)
    raise ValueError(f"unknown breakdown {by!r}")


def breakdown(trades: pd.DataFrame, by: str) -> pd.DataFrame:
    """Trades, result, win rate and average win/loss per bucket (empty buckets left out)."""
    cols = ["bucket", "trades", "pnl", "win_rate", "avg_win", "avg_loss"]
    if trades is None or trades.empty:
        return pd.DataFrame(columns=cols)
    key = _bucket(trades, by)
    out = []
    for name, g in trades.groupby(key, observed=True):
        wins, losses = g[g["pnl"] > 0], g[g["pnl"] <= 0]
        out.append({"bucket": str(name), "trades": int(len(g)), "pnl": float(g["pnl"].sum()),
                    "win_rate": float(len(wins) / len(g)),
                    "avg_win": float(wins["pnl"].mean()) if len(wins) else float("nan"),
                    "avg_loss": float(losses["pnl"].mean()) if len(losses) else float("nan")})
    return pd.DataFrame(out, columns=cols)


def daily(trades: pd.DataFrame) -> pd.DataFrame:
    """Result and number of trades per day (by entry date)."""
    if trades is None or trades.empty:
        return pd.DataFrame(columns=["day", "pnl", "trades"])
    g = trades.groupby(trades["entry"].dt.date)
    return pd.DataFrame({"day": list(g.groups), "pnl": g["pnl"].sum().values,
                         "trades": g.size().values})


def worst(trades: pd.DataFrame, n: int = 5) -> pd.DataFrame:
    if trades is None or trades.empty:
        return pd.DataFrame(columns=TRADE_COLS)
    return trades.nsmallest(n, "pnl")
