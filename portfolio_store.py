"""
portfolio_store.py — Persist My Open Positions across Streamlit sessions.

Open ledger:   data/portfolio.json
Closed ledger: data/portfolio_closed.json  (exit price + realized PnL)
Daily journal: data/journal/YYYY-MM-DD.json  (append-only buy/sell events per day)
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

ET = ZoneInfo("America/New_York")

_BASE = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_PATH = os.path.join(_BASE, "data", "portfolio.json")
CLOSED_PATH = os.path.join(_BASE, "data", "portfolio_closed.json")
JOURNAL_DIR = os.path.join(_BASE, "data", "journal")

LEDGER_COLS = [
    "Ticker", "Side", "Strike", "Expiry", "Quantity", "Entry_Price",
    "Mark_Price", "Mark_Updated_At", "Entry_At",
]
EDITOR_COLS = ["Ticker", "Side", "Strike", "Expiry", "Quantity", "Entry_Price"]
CLOSED_COLS = [
    "Ticker", "Side", "Strike", "Expiry", "Quantity",
    "Entry_Price", "Exit_Price", "Entry_At", "Exit_At",
    "PnL_Pct", "PnL_Dollars",
]


def _now_et() -> datetime:
    return datetime.now(ET)


def _now_iso() -> str:
    return _now_et().isoformat(timespec="seconds")


def _today_et() -> str:
    return _now_et().date().isoformat()


def today_et() -> str:
    """Public: current America/New_York calendar day as YYYY-MM-DD."""
    return _today_et()


def _date_from_iso(ts: str | None) -> str:
    """Extract YYYY-MM-DD (ET calendar day) from an ISO timestamp."""
    if not ts:
        return _today_et()
    s = str(ts).strip()
    try:
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            try:
                dt = datetime.fromisoformat(s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=ET)
                return dt.astimezone(ET).date().isoformat()
            except ValueError:
                return s[:10]
    except Exception:
        pass
    return _today_et()


def empty_portfolio() -> pd.DataFrame:
    return pd.DataFrame(columns=LEDGER_COLS)


def empty_closed() -> pd.DataFrame:
    return pd.DataFrame(columns=CLOSED_COLS)


def load_portfolio() -> pd.DataFrame:
    if not os.path.exists(PORTFOLIO_PATH):
        return empty_portfolio()
    try:
        with open(PORTFOLIO_PATH) as fh:
            rows = json.load(fh)
        if not isinstance(rows, list):
            return empty_portfolio()
        df = pd.DataFrame(rows)
        for col in LEDGER_COLS:
            if col not in df.columns:
                df[col] = pd.NA
        return df[LEDGER_COLS].copy()
    except Exception:
        return empty_portfolio()


def load_closed() -> pd.DataFrame:
    if not os.path.exists(CLOSED_PATH):
        return empty_closed()
    try:
        with open(CLOSED_PATH) as fh:
            rows = json.load(fh)
        if not isinstance(rows, list):
            return empty_closed()
        df = pd.DataFrame(rows)
        for col in CLOSED_COLS:
            if col not in df.columns:
                df[col] = pd.NA
        return df[CLOSED_COLS].copy()
    except Exception:
        return empty_closed()


def save_portfolio(df: pd.DataFrame) -> None:
    os.makedirs(os.path.dirname(PORTFOLIO_PATH), exist_ok=True)
    out = empty_portfolio() if df is None or df.empty else df.copy()
    for col in LEDGER_COLS:
        if col not in out.columns:
            out[col] = pd.NA
    out = out[LEDGER_COLS]
    records: list[dict[str, Any]] = []
    for _, r in out.iterrows():
        ticker = str(r.get("Ticker") or "").strip().upper()
        if not ticker or ticker == "NAN":
            continue
        rec = {
            "Ticker": ticker,
            "Side": str(r.get("Side") or "").strip().upper(),
            "Strike": _num_or_none(r.get("Strike")),
            "Expiry": str(r.get("Expiry") or "").strip(),
            "Quantity": _num_or_none(r.get("Quantity")),
            "Entry_Price": _num_or_none(r.get("Entry_Price")),
            "Mark_Price": _num_or_none(r.get("Mark_Price")),
            "Mark_Updated_At": (
                None if pd.isna(r.get("Mark_Updated_At"))
                else str(r.get("Mark_Updated_At"))
            ),
            "Entry_At": (
                None if pd.isna(r.get("Entry_At")) else str(r.get("Entry_At"))
            ),
        }
        records.append(rec)
    with open(PORTFOLIO_PATH, "w") as fh:
        json.dump(records, fh, indent=2)


def save_closed(df: pd.DataFrame) -> None:
    os.makedirs(os.path.dirname(CLOSED_PATH), exist_ok=True)
    out = empty_closed() if df is None or df.empty else df.copy()
    for col in CLOSED_COLS:
        if col not in out.columns:
            out[col] = pd.NA
    records: list[dict[str, Any]] = []
    for _, r in out.iterrows():
        ticker = str(r.get("Ticker") or "").strip().upper()
        if not ticker or ticker == "NAN":
            continue
        records.append({
            "Ticker": ticker,
            "Side": str(r.get("Side") or "").strip().upper(),
            "Strike": _num_or_none(r.get("Strike")),
            "Expiry": str(r.get("Expiry") or "").strip(),
            "Quantity": _num_or_none(r.get("Quantity")),
            "Entry_Price": _num_or_none(r.get("Entry_Price")),
            "Exit_Price": _num_or_none(r.get("Exit_Price")),
            "Entry_At": (
                None if pd.isna(r.get("Entry_At")) else str(r.get("Entry_At"))
            ),
            "Exit_At": (
                None if pd.isna(r.get("Exit_At")) else str(r.get("Exit_At"))
            ),
            "PnL_Pct": _num_or_none(r.get("PnL_Pct")),
            "PnL_Dollars": _num_or_none(r.get("PnL_Dollars")),
        })
    with open(CLOSED_PATH, "w") as fh:
        json.dump(records, fh, indent=2)


def _num_or_none(v) -> float | None:
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)) or pd.isna(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ── Daily journal files (data/journal/YYYY-MM-DD.json) ─────────────────────────


def journal_path_for_day(day: str | date | None = None) -> str:
    if day is None:
        day_s = _today_et()
    elif isinstance(day, date):
        day_s = day.isoformat()
    else:
        day_s = str(day).strip()[:10]
    return os.path.join(JOURNAL_DIR, f"{day_s}.json")


def _load_day_events(day: str) -> list[dict[str, Any]]:
    path = journal_path_for_day(day)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as fh:
            rows = json.load(fh)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _save_day_events(day: str, events: list[dict[str, Any]]) -> None:
    os.makedirs(JOURNAL_DIR, exist_ok=True)
    path = journal_path_for_day(day)
    with open(path, "w") as fh:
        json.dump(events, fh, indent=2)


def _event_fingerprint(ev: dict[str, Any]) -> str:
    """Dedupe key for backfill / accidental double-append."""
    return "|".join([
        str(ev.get("Action") or ""),
        str(ev.get("Ticker") or ""),
        str(ev.get("Side") or ""),
        str(ev.get("Strike") or ""),
        str(ev.get("Expiry") or ""),
        str(ev.get("Quantity") or ""),
        str(ev.get("Price") or ""),
        str(ev.get("At") or ""),
        str(ev.get("PnL_Dollars") if ev.get("PnL_Dollars") is not None else ""),
    ])


def append_journal_event(event: dict[str, Any], *, day: str | None = None) -> str:
    """
    Append one BUY/SELL event to data/journal/YYYY-MM-DD.json.
    Returns the day string written.
    """
    day_s = day or _date_from_iso(event.get("At"))
    events = _load_day_events(day_s)
    fp = _event_fingerprint(event)
    if any(_event_fingerprint(e) == fp for e in events):
        return day_s
    events.append(event)
    _save_day_events(day_s, events)
    return day_s


def list_journal_days() -> list[str]:
    """Sorted YYYY-MM-DD days that have a journal file (newest first)."""
    if not os.path.isdir(JOURNAL_DIR):
        return []
    days: list[str] = []
    for name in os.listdir(JOURNAL_DIR):
        if name.endswith(".json") and len(name) == 15:  # YYYY-MM-DD.json
            days.append(name[:10])
    return sorted(days, reverse=True)


def load_journal_day(day: str) -> pd.DataFrame:
    """Load one day's events as a DataFrame."""
    events = _load_day_events(day)
    cols = [
        "Action", "Ticker", "Side", "Strike", "Expiry", "Quantity",
        "Price", "At", "Entry_Price", "PnL_Pct", "PnL_Dollars", "Day",
    ]
    if not events:
        return pd.DataFrame(columns=cols)
    rows = []
    for e in events:
        rows.append({
            "Action": e.get("Action"),
            "Ticker": e.get("Ticker"),
            "Side": e.get("Side"),
            "Strike": e.get("Strike"),
            "Expiry": e.get("Expiry"),
            "Quantity": e.get("Quantity"),
            "Price": e.get("Price"),
            "At": e.get("At"),
            "Entry_Price": e.get("Entry_Price"),
            "PnL_Pct": e.get("PnL_Pct"),
            "PnL_Dollars": e.get("PnL_Dollars"),
            "Day": day,
        })
    return pd.DataFrame(rows)


def load_all_journal_events() -> pd.DataFrame:
    """Concatenate all daily journal files (newest days first)."""
    frames = [load_journal_day(d) for d in list_journal_days()]
    frames = [f for f in frames if not f.empty]
    if not frames:
        return load_journal_day(_today_et())
    return pd.concat(frames, ignore_index=True)


def backfill_daily_journal_from_ledgers() -> int:
    """
    One-shot: write missing BUY/SELL events from open + closed ledgers
    into daily journal files. Returns number of events newly written.
    """
    written = 0
    open_df = load_portfolio()
    if open_df is not None and not open_df.empty:
        for _, r in open_df.iterrows():
            at = None if pd.isna(r.get("Entry_At")) else str(r.get("Entry_At"))
            ev = {
                "Action": "BUY",
                "Ticker": str(r.get("Ticker") or "").upper().strip(),
                "Side": str(r.get("Side") or "").upper().strip(),
                "Strike": _num_or_none(r.get("Strike")),
                "Expiry": str(r.get("Expiry") or "").strip(),
                "Quantity": _num_or_none(r.get("Quantity")),
                "Price": _num_or_none(r.get("Entry_Price")),
                "At": at or _now_iso(),
                "Entry_Price": _num_or_none(r.get("Entry_Price")),
                "PnL_Pct": None,
                "PnL_Dollars": None,
            }
            day = _date_from_iso(ev["At"])
            before = len(_load_day_events(day))
            append_journal_event(ev, day=day)
            written += max(0, len(_load_day_events(day)) - before)

    closed_df = load_closed()
    if closed_df is not None and not closed_df.empty:
        for _, r in closed_df.iterrows():
            bought_at = None if pd.isna(r.get("Entry_At")) else str(r.get("Entry_At"))
            sold_at = None if pd.isna(r.get("Exit_At")) else str(r.get("Exit_At"))
            if bought_at:
                buy_ev = {
                    "Action": "BUY",
                    "Ticker": str(r.get("Ticker") or "").upper().strip(),
                    "Side": str(r.get("Side") or "").upper().strip(),
                    "Strike": _num_or_none(r.get("Strike")),
                    "Expiry": str(r.get("Expiry") or "").strip(),
                    "Quantity": _num_or_none(r.get("Quantity")),
                    "Price": _num_or_none(r.get("Entry_Price")),
                    "At": bought_at,
                    "Entry_Price": _num_or_none(r.get("Entry_Price")),
                    "PnL_Pct": None,
                    "PnL_Dollars": None,
                }
                day = _date_from_iso(bought_at)
                before = len(_load_day_events(day))
                append_journal_event(buy_ev, day=day)
                written += max(0, len(_load_day_events(day)) - before)

            sell_ev = {
                "Action": "SELL",
                "Ticker": str(r.get("Ticker") or "").upper().strip(),
                "Side": str(r.get("Side") or "").upper().strip(),
                "Strike": _num_or_none(r.get("Strike")),
                "Expiry": str(r.get("Expiry") or "").strip(),
                "Quantity": _num_or_none(r.get("Quantity")),
                "Price": _num_or_none(r.get("Exit_Price")),
                "At": sold_at or _now_iso(),
                "Entry_Price": _num_or_none(r.get("Entry_Price")),
                "PnL_Pct": _num_or_none(r.get("PnL_Pct")),
                "PnL_Dollars": _num_or_none(r.get("PnL_Dollars")),
            }
            day = _date_from_iso(sell_ev["At"])
            before = len(_load_day_events(day))
            append_journal_event(sell_ev, day=day)
            written += max(0, len(_load_day_events(day)) - before)

    return written


def append_position(
    *,
    ticker: str,
    side: str,
    strike: float,
    expiry: str,
    quantity: int | float,
    entry_price: float,
    mark_price: float | None = None,
) -> pd.DataFrame:
    """Append one contract and persist. Returns the new ledger."""
    df = load_portfolio()
    mark = float(mark_price) if mark_price is not None else float(entry_price)
    now = _now_iso()
    row = {
        "Ticker": str(ticker).upper().strip(),
        "Side": str(side).upper().strip(),
        "Strike": float(strike),
        "Expiry": str(expiry).strip(),
        "Quantity": float(quantity),
        "Entry_Price": float(entry_price),
        "Mark_Price": mark,
        "Mark_Updated_At": now,
        "Entry_At": now,
    }
    if df.empty:
        df = pd.DataFrame([row], columns=LEDGER_COLS)
    else:
        df = pd.concat([df, pd.DataFrame([row])], ignore_index=True)
    save_portfolio(df)
    append_journal_event({
        "Action": "BUY",
        "Ticker": row["Ticker"],
        "Side": row["Side"],
        "Strike": row["Strike"],
        "Expiry": row["Expiry"],
        "Quantity": row["Quantity"],
        "Price": row["Entry_Price"],
        "At": now,
        "Entry_Price": row["Entry_Price"],
        "PnL_Pct": None,
        "PnL_Dollars": None,
    })
    return df


def close_position(
    row_index: int,
    exit_price: float,
    *,
    portfolio_df: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """
    Remove an open position by positional index and append to the closed ledger
    with Exit_Price / realized PnL. Returns (updated_open_df, closed_record).
    """
    df = (
        portfolio_df.copy()
        if isinstance(portfolio_df, pd.DataFrame)
        else load_portfolio()
    )
    df = df.reset_index(drop=True)
    if row_index < 0 or row_index >= len(df):
        raise IndexError(f"position index out of range: {row_index}")

    r = df.iloc[row_index]
    entry = float(r.get("Entry_Price") or 0)
    qty = float(r.get("Quantity") or 0)
    exit_px = float(exit_price)
    pnl_pct = ((exit_px - entry) / entry) if entry else float("nan")
    pnl_dollars = (exit_px - entry) * qty * 100.0 if entry else float("nan")
    now = _now_iso()

    closed_rec = {
        "Ticker": str(r.get("Ticker") or "").upper().strip(),
        "Side": str(r.get("Side") or "").upper().strip(),
        "Strike": _num_or_none(r.get("Strike")),
        "Expiry": str(r.get("Expiry") or "").strip(),
        "Quantity": qty,
        "Entry_Price": entry,
        "Exit_Price": exit_px,
        "Entry_At": (
            None if pd.isna(r.get("Entry_At")) else str(r.get("Entry_At"))
        ),
        "Exit_At": now,
        "PnL_Pct": None if pd.isna(pnl_pct) else round(pnl_pct, 6),
        "PnL_Dollars": None if pd.isna(pnl_dollars) else round(pnl_dollars, 2),
    }

    closed = load_closed()
    if closed.empty:
        closed = pd.DataFrame([closed_rec], columns=CLOSED_COLS)
    else:
        closed = pd.concat(
            [closed, pd.DataFrame([closed_rec])], ignore_index=True
        )
    save_closed(closed)

    append_journal_event({
        "Action": "SELL",
        "Ticker": closed_rec["Ticker"],
        "Side": closed_rec["Side"],
        "Strike": closed_rec["Strike"],
        "Expiry": closed_rec["Expiry"],
        "Quantity": closed_rec["Quantity"],
        "Price": closed_rec["Exit_Price"],
        "At": now,
        "Entry_Price": closed_rec["Entry_Price"],
        "PnL_Pct": closed_rec["PnL_Pct"],
        "PnL_Dollars": closed_rec["PnL_Dollars"],
    })

    df = df.drop(index=row_index).reset_index(drop=True)
    save_portfolio(df)
    return df, closed_rec


def apply_live_marks(
    portfolio_df: pd.DataFrame,
    live_scanner_df: pd.DataFrame,
    *,
    force_eod: bool = False,
) -> pd.DataFrame:
    """
    Update Mark_Price from live scanner quotes when a contract matches.
    Always updates when a fresh Current_Price is available; force_eod is
    reserved for end-of-day snapshot semantics (same write today).
    """
    if portfolio_df is None or portfolio_df.empty:
        return empty_portfolio()

    df = portfolio_df.copy()
    for col in LEDGER_COLS:
        if col not in df.columns:
            df[col] = pd.NA

    if live_scanner_df is None or live_scanner_df.empty:
        return df[LEDGER_COLS]

    live = live_scanner_df.copy()
    rename = {}
    cols_lower = {c.lower(): c for c in live.columns}
    if "current_price" not in cols_lower and "last" in cols_lower:
        rename[cols_lower["last"]] = "Current_Price"
    for want, aliases in [
        ("Ticker", ("ticker",)),
        ("Side", ("side",)),
        ("Strike", ("strike",)),
        ("Expiry", ("expiry",)),
        ("Current_Price", ("current_price", "last")),
    ]:
        if want not in live.columns:
            for a in aliases:
                if a in cols_lower:
                    rename[cols_lower[a]] = want
                    break
    live = live.rename(columns=rename)
    if "Current_Price" not in live.columns:
        return df[LEDGER_COLS]

    live["Ticker"] = live["Ticker"].astype(str).str.upper().str.strip()
    live["Side"] = live["Side"].astype(str).str.upper().str.strip()
    live["Strike"] = pd.to_numeric(live["Strike"], errors="coerce")
    live["Expiry"] = live["Expiry"].astype(str).str.strip()
    live["Current_Price"] = pd.to_numeric(live["Current_Price"], errors="coerce")

    changed = False
    now = _now_iso()
    for i, row in df.iterrows():
        mask = (
            (live["Ticker"] == str(row.get("Ticker") or "").upper())
            & (live["Side"] == str(row.get("Side") or "").upper())
            & (live["Strike"] == pd.to_numeric(row.get("Strike"), errors="coerce"))
            & (live["Expiry"] == str(row.get("Expiry") or "").strip())
        )
        hits = live.loc[mask, "Current_Price"].dropna()
        if hits.empty:
            continue
        px = float(hits.iloc[0])
        if px <= 0:
            continue
        prev = row.get("Mark_Price")
        if force_eod or pd.isna(prev) or abs(float(prev) - px) > 1e-9:
            df.at[i, "Mark_Price"] = px
            df.at[i, "Mark_Updated_At"] = now
            changed = True

    if changed:
        save_portfolio(df)
    return df[LEDGER_COLS]


JOURNAL_COLS = [
    "Status", "Ticker", "Side", "Strike", "Expiry", "Quantity",
    "Bought_At", "Bought_Price", "Sold_At", "Sold_Price",
    "PnL_Pct", "PnL_Dollars", "Unrealized_Pct", "Unrealized_Dollars",
]


def journal_dataframe(
    open_df: pd.DataFrame | None = None,
    closed_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Unified buy/sell journal: open (bought) + closed (bought & sold)."""
    open_df = load_portfolio() if open_df is None else open_df
    closed_df = load_closed() if closed_df is None else closed_df
    rows: list[dict[str, Any]] = []

    if closed_df is not None and not closed_df.empty:
        for _, r in closed_df.iterrows():
            rows.append({
                "Status": "CLOSED",
                "Ticker": str(r.get("Ticker") or "").upper().strip(),
                "Side": str(r.get("Side") or "").upper().strip(),
                "Strike": _num_or_none(r.get("Strike")),
                "Expiry": str(r.get("Expiry") or "").strip(),
                "Quantity": _num_or_none(r.get("Quantity")),
                "Bought_At": (
                    None if pd.isna(r.get("Entry_At")) else str(r.get("Entry_At"))
                ),
                "Bought_Price": _num_or_none(r.get("Entry_Price")),
                "Sold_At": (
                    None if pd.isna(r.get("Exit_At")) else str(r.get("Exit_At"))
                ),
                "Sold_Price": _num_or_none(r.get("Exit_Price")),
                "PnL_Pct": _num_or_none(r.get("PnL_Pct")),
                "PnL_Dollars": _num_or_none(r.get("PnL_Dollars")),
                "Unrealized_Pct": None,
                "Unrealized_Dollars": None,
            })

    if open_df is not None and not open_df.empty:
        for _, r in open_df.iterrows():
            entry = _num_or_none(r.get("Entry_Price"))
            mark = _num_or_none(r.get("Mark_Price"))
            qty = _num_or_none(r.get("Quantity")) or 0.0
            u_pct = u_dol = None
            if entry and entry > 0 and mark is not None:
                u_pct = (mark - entry) / entry
                u_dol = (mark - entry) * 100.0 * float(qty)
            rows.append({
                "Status": "OPEN",
                "Ticker": str(r.get("Ticker") or "").upper().strip(),
                "Side": str(r.get("Side") or "").upper().strip(),
                "Strike": _num_or_none(r.get("Strike")),
                "Expiry": str(r.get("Expiry") or "").strip(),
                "Quantity": qty if qty else _num_or_none(r.get("Quantity")),
                "Bought_At": (
                    None if pd.isna(r.get("Entry_At")) else str(r.get("Entry_At"))
                ),
                "Bought_Price": entry,
                "Sold_At": None,
                "Sold_Price": None,
                "PnL_Pct": None,
                "PnL_Dollars": None,
                "Unrealized_Pct": None if u_pct is None else round(u_pct, 6),
                "Unrealized_Dollars": (
                    None if u_dol is None else round(u_dol, 2)
                ),
            })

    if not rows:
        return pd.DataFrame(columns=JOURNAL_COLS)
    out = pd.DataFrame(rows)[JOURNAL_COLS]
    sort_key = out["Sold_At"].fillna(out["Bought_At"]).fillna("")
    return out.assign(_sk=sort_key).sort_values("_sk", ascending=False).drop(
        columns=["_sk"]
    ).reset_index(drop=True)


def journal_performance(journal_df: pd.DataFrame | None = None) -> dict[str, Any]:
    """Aggregate performance stats from the journal."""
    jf = journal_dataframe() if journal_df is None else journal_df
    closed = jf[jf["Status"] == "CLOSED"] if not jf.empty else jf
    open_ = jf[jf["Status"] == "OPEN"] if not jf.empty else jf

    n_closed = int(len(closed))
    n_open = int(len(open_))
    wins = 0
    losses = 0
    total_pnl = 0.0
    pcts: list[float] = []
    if n_closed:
        for _, r in closed.iterrows():
            d = _num_or_none(r.get("PnL_Dollars"))
            p = _num_or_none(r.get("PnL_Pct"))
            if d is not None:
                total_pnl += float(d)
                if d > 0:
                    wins += 1
                elif d < 0:
                    losses += 1
            if p is not None:
                pcts.append(float(p))

    unrealized = 0.0
    if n_open:
        for _, r in open_.iterrows():
            u = _num_or_none(r.get("Unrealized_Dollars"))
            if u is not None:
                unrealized += float(u)

    return {
        "n_closed": n_closed,
        "n_open": n_open,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / n_closed) if n_closed else None,
        "total_realized_pnl": round(total_pnl, 2),
        "avg_pnl_pct": round(sum(pcts) / len(pcts), 6) if pcts else None,
        "unrealized_pnl": round(unrealized, 2),
    }


def day_performance(day: str) -> dict[str, Any]:
    """Realized PnL stats for SELL events on one calendar day."""
    df = load_journal_day(day)
    sells = df[df["Action"] == "SELL"] if not df.empty else df
    buys = df[df["Action"] == "BUY"] if not df.empty else df
    total = 0.0
    wins = losses = 0
    pcts: list[float] = []
    for _, r in sells.iterrows():
        d = _num_or_none(r.get("PnL_Dollars"))
        p = _num_or_none(r.get("PnL_Pct"))
        if d is not None:
            total += float(d)
            if d > 0:
                wins += 1
            elif d < 0:
                losses += 1
        if p is not None:
            pcts.append(float(p))
    n = int(len(sells))
    return {
        "day": day,
        "n_buys": int(len(buys)),
        "n_sells": n,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / n) if n else None,
        "realized_pnl": round(total, 2),
        "avg_pnl_pct": round(sum(pcts) / len(pcts), 6) if pcts else None,
        "file": journal_path_for_day(day),
    }
