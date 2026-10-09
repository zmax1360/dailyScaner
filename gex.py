"""gex — net gamma exposure (GEX) per strike and expiry from open interest.

    shares:       GEX = gamma x open_interest x 100            (shares per $1 move)
    per $1 move:  GEX = gamma x open_interest x 100 x spot     (dollars per $1 move)
    per 1% move:  GEX = gamma x open_interest x 100 x spot^2 x 0.01

Calls count positive, puts negative. That sign is the standard dealer-positioning
ASSUMPTION (dealers long calls, short puts); it is not observed.

Pure calculation: no network, no Streamlit, no writes. Display/analysis only — not a
scoring input. Missing inputs (open interest, IV, spot) are excluded and counted,
never defaulted to 0. A strike/expiry with no usable contract is NaN, not 0.

Input is the frame from ``volume_history.latest_scan`` (contracts that have traded today,
so a strike with open interest but no volume yet is absent until it trades).
"""

from __future__ import annotations

import math
from datetime import date, datetime, time as dtime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

from config import SCORING
from greeks import implied_vol
from zero_dte_gex import bs_gamma

ET = ZoneInfo("America/New_York")
SESSION_CLOSE = dtime(16, 0)
MIN_T_MINUTES = 15.0          # gamma is unbounded at expiry; same floor as zero_dte_gex
CONTRACT_MULTIPLIER = 100
MOVE_PCT = 0.01               # exposure is quoted per 1% move in spot

UNIT_DOLLAR = "dollar"        # $ of delta hedging per $1 move in spot
UNIT_PCT = "pct"              # $ of delta hedging per 1% move in spot
UNIT_SHARES = "shares"        # shares of delta hedging per $1 move in spot
UNITS = (UNIT_DOLLAR, UNIT_PCT, UNIT_SHARES)

IV_VENDOR = "vendor"          # IV as reported by the data source
IV_QUOTE = "quote"            # IV solved from the bid/ask mid (vendor IV as fallback)

TABLE_COLS = ["side", "strike", "expiry", "dte", "open_interest", "iv", "iv_from_quote",
              "iv_from_pair", "gamma", "gex"]


def _f(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def years_to_expiry(expiry: str, as_of: datetime) -> float | None:
    """Calendar time from ``as_of`` to 16:00 ET on the expiry date, in years.

    Includes the hours left today (unlike a whole-day DTE). Floored at
    MIN_T_MINUTES before the close. None when the expiry is unparseable or the
    contract has already expired at ``as_of`` (16:00 ET on the expiry date).
    """
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    now = as_of.astimezone(ET)
    try:
        exp_d = date.fromisoformat(str(expiry)[:10])
    except ValueError:
        return None
    close = datetime.combine(exp_d, SESSION_CLOSE, tzinfo=ET)
    if close <= now:
        return None                       # expired at the time of the snapshot
    minutes = max((close - now).total_seconds() / 60.0, MIN_T_MINUTES)
    return minutes / (365.0 * 24.0 * 60.0)


PARITY_NEAR_PCT = 0.015         # strikes this close to spot can set the quote price
PARITY_MAX_SPREAD = 0.10        # ...if both quotes are this tight (share of the mid)
PARITY_MIN_SPREAD_ABS = 0.05    # ...or within this many dollars
PARITY_MAX_SHIFT_PCT = 0.01     # a quote price further than this from spot is not believed


def _usable_mid(bid: Any, ask: Any) -> float | None:
    b, a = _f(bid), _f(ask)
    if b is None or a is None or b <= 0 or a < b:
        return None
    return 0.5 * (b + a)


def quote_price(records: list[dict], spot: float, t_years: float, r: float) -> float | None:
    """The stock price the option quotes of one expiry were made at, from put-call parity:
    call − put + strike × e^(−rT), at up to three tight-quoted strikes nearest spot
    (the middle value). None when no strike near spot has two tight quotes, or when the
    answer is further from spot than is believable.

    Option quotes often lag the stock by several minutes. Solving implied volatility
    with a newer stock price than the quotes were made at makes calls look cheap and
    puts look dear (or the reverse), which on expiry day is enough to flip a strike's
    sign. Solving at the quotes' own price removes that."""
    by_strike: dict[float, dict[str, tuple[float, float]]] = {}
    for rec in records:
        side, k = str(rec.get("side") or "").upper(), _f(rec.get("strike"))
        mid = _usable_mid(rec.get("bid"), rec.get("ask"))
        if side not in ("CALL", "PUT") or k is None or mid is None:
            continue
        if abs(k - spot) > spot * PARITY_NEAR_PCT:
            continue
        spread = _f(rec.get("ask")) - _f(rec.get("bid"))
        if spread > max(PARITY_MIN_SPREAD_ABS, PARITY_MAX_SPREAD * mid):
            continue
        by_strike.setdefault(k, {})[side] = (mid, spread)
    pairs = sorted((k for k, v in by_strike.items() if len(v) == 2),
                   key=lambda k: abs(k - spot))[:3]
    if not pairs:
        return None
    est = sorted(by_strike[k]["CALL"][0] - by_strike[k]["PUT"][0] + k * math.exp(-r * t_years)
                 for k in pairs)
    price = est[len(est) // 2] if len(est) % 2 else 0.5 * (est[0] + est[1]) if len(est) == 2 \
        else est[len(est) // 2]
    if abs(price - spot) > spot * PARITY_MAX_SHIFT_PCT:
        return None
    return float(price)


def gex_table(
    latest: pd.DataFrame,
    *,
    spot: float | None,
    as_of: datetime,
    unit: str = UNIT_DOLLAR,
    today: date | None = None,
    iv_source: str = IV_QUOTE,
) -> pd.DataFrame:
    """One row per contract with its gamma and signed dollar GEX.

    ``gex`` is NaN when open interest, IV or gamma is unusable. Contracts already
    expired at ``as_of`` are dropped, and so are expiries before ``today`` (so a
    snapshot viewed on a later day does not show dead expiries).

    ``iv_source`` "quote" solves IV from each contract's bid/ask mid with the exact
    time to expiry, at the stock price the quotes themselves imply (``quote_price``;
    spot when that cannot be told). A call and a put at one strike then share ONE implied
    volatility whenever the out-of-the-money contract's solves: the in-the-money contract
    uses it too (flagged ``iv_from_pair``). With one IV per strike the sign of a strike is
    simply calls against puts in open interest; separate IVs let it flip with quote
    noise. An out-of-the-money contract never borrows from its in-the-money pair, whose
    quotes are wide. Without any solved IV the vendor IV is kept. IV is never
    borrowed from a different strike: penny quotes in the far wings solve to inflated
    IVs, and spreading those to neighbours overstated wing gamma several-fold.

    Gamma is always evaluated at ``spot``. ``table.attrs["quote_price"]`` maps each expiry
    to the price its IVs were solved at (absent in vendor mode).
    """
    if unit not in UNITS:
        raise ValueError(f"unknown unit {unit!r}")
    if iv_source not in (IV_VENDOR, IV_QUOTE):
        raise ValueError(f"unknown iv_source {iv_source!r}")
    s = _f(spot)
    if latest is None or latest.empty or s is None or s <= 0:
        return pd.DataFrame(columns=TABLE_COLS)
    r = float(SCORING.get("risk_free_rate", 0.045))
    min_iv = float(SCORING.get("min_iv_usable", 0.01))
    snap_day = as_of.astimezone(ET).date()
    scale = {UNIT_DOLLAR: s, UNIT_PCT: s * s * MOVE_PCT, UNIT_SHARES: 1.0}[unit]

    # pass 1: parse; find the price each expiry's quotes were made at
    records = []
    for rec in latest.to_dict(orient="records"):
        side = str(rec.get("side") or "").upper()
        strike = _f(rec.get("strike"))
        expiry = str(rec.get("expiry") or "")[:10]
        t = years_to_expiry(expiry, as_of)
        if side not in ("CALL", "PUT") or strike is None or strike <= 0 or t is None:
            continue
        if today is not None and date.fromisoformat(expiry) < today:
            continue
        records.append({**rec, "side": side, "strike": strike, "expiry": expiry, "t": t})

    priced_at: dict[str, float] = {}
    if iv_source == IV_QUOTE:
        by_expiry: dict[str, list[dict]] = {}
        for rec in records:
            by_expiry.setdefault(rec["expiry"], []).append(rec)
        for expiry, recs in by_expiry.items():
            q = quote_price(recs, s, recs[0]["t"], r)
            priced_at[expiry] = s if q is None else q

    # pass 2: (quote mode) solve IV from each contract's own mid at that price
    parsed = []
    for rec in records:
        side, strike, expiry, t = rec["side"], rec["strike"], rec["expiry"], rec["t"]
        solved = None
        if iv_source == IV_QUOTE:
            mid = _usable_mid(rec.get("bid"), rec.get("ask"))
            if mid is not None:
                v = implied_vol(side, priced_at[expiry], strike, t * 365.0, mid, r=r)
                if v is not None and v >= min_iv:
                    solved = v
        parsed.append({"side": side, "strike": strike, "expiry": expiry, "t": t,
                       "oi": _f(rec.get("open_interest")), "vendor_iv": _f(rec.get("iv")),
                       "solved": solved})

    # One IV per strike and expiry whenever the out-of-the-money contract solved: both the
    # call and the put use it. Out-of-the-money is judged at the quotes' own price. It is
    # never the other way round: in-the-money quotes are wide and solve to inflated IVs.
    def _is_otm(p: dict) -> bool:
        return (p["side"] == "CALL") == (p["strike"] > priced_at.get(p["expiry"], s))

    shared: dict[tuple[str, float], tuple[float, str]] = {}
    for p in parsed:
        if p["solved"] is not None and _is_otm(p):
            shared[(p["expiry"], p["strike"])] = (p["solved"], p["side"])

    rows = []
    for p in parsed:
        side, strike, expiry, t, oi = p["side"], p["strike"], p["expiry"], p["t"], p["oi"]
        iv, from_quote, from_pair = p["vendor_iv"], False, False
        pick = shared.get((expiry, strike))
        if pick is not None:
            iv, from_quote, from_pair = pick[0], True, pick[1] != side
        elif p["solved"] is not None:
            iv, from_quote = p["solved"], True          # no out-of-the-money IV to share
        gamma = None
        if iv is not None and iv >= min_iv:
            gamma = bs_gamma(s, strike, iv, t_years=t, r=r)
        gex = float("nan")
        if gamma is not None and oi is not None and oi >= 0:
            sign = 1.0 if side == "CALL" else -1.0
            gex = sign * gamma * oi * CONTRACT_MULTIPLIER * scale
        rows.append({
            "side": side, "strike": strike, "expiry": expiry,
            "dte": (date.fromisoformat(expiry) - snap_day).days,
            "open_interest": float("nan") if oi is None else oi,
            "iv": float("nan") if iv is None else iv,
            "iv_from_quote": from_quote,
            "iv_from_pair": from_pair,
            "gamma": float("nan") if gamma is None else gamma,
            "gex": gex,
        })
    out = pd.DataFrame(rows, columns=TABLE_COLS)
    if priced_at:
        out.attrs["quote_price"] = priced_at
    return out


def coverage(table: pd.DataFrame) -> dict[str, int]:
    """How many contracts actually contributed — the map is only as good as this."""
    n = int(len(table))
    used = int(table["gex"].notna().sum()) if n else 0
    from_quote = int(table["iv_from_quote"].sum()) if n else 0
    from_pair = int(table["iv_from_pair"].sum()) if n else 0
    return {"contracts": n, "used": used, "excluded": n - used,
            "iv_from_quote": from_quote, "iv_from_pair": from_pair}


def available_expiries(table: pd.DataFrame) -> list[str]:
    """Expiries that have at least one usable contract, nearest first."""
    if table is None or table.empty:
        return []
    return sorted(table.loc[table["gex"].notna(), "expiry"].unique())


def current_week(expiries: list[str]) -> list[str]:
    """Expiries in the same Mon-Sun week as the nearest one."""
    if not expiries:
        return []
    first = date.fromisoformat(expiries[0])
    week = first.isocalendar()[:2]
    return [e for e in expiries if date.fromisoformat(e).isocalendar()[:2] == week]


def gex_matrix(
    table: pd.DataFrame,
    *,
    spot: float,
    expiries: list[str] | None = None,
    n_strikes: int | None = 16,
) -> pd.DataFrame:
    """Net GEX: strikes (descending) x expiries (nearest first).

    ``expiries`` None = every expiry with usable contracts. ``n_strikes`` keeps the
    N strikes closest to spot (None = all). A cell is NaN when no usable contract
    exists there (never 0).
    """
    if table is None or table.empty:
        return pd.DataFrame()
    t = table[table["gex"].notna()]
    cols = available_expiries(table)
    if expiries is not None:
        cols = [e for e in cols if e in set(expiries)]
    t = t[t["expiry"].isin(cols)]
    if t.empty:
        return pd.DataFrame()
    if n_strikes is not None:
        strikes = sorted(t["strike"].unique(), key=lambda k: (abs(k - spot), k))
        t = t[t["strike"].isin(strikes[: max(int(n_strikes), 1)])]
    m = t.pivot_table(index="strike", columns="expiry", values="gex",
                      aggfunc=lambda x: x.sum(min_count=1))
    return m.reindex(columns=cols).sort_index(ascending=False)


def walls(matrix: pd.DataFrame) -> dict[str, dict[str, float | None]]:
    """Per expiry: largest positive strike (call wall), most negative (put wall), net."""
    out: dict[str, dict[str, float | None]] = {}
    for exp in matrix.columns:
        col = matrix[exp].dropna()
        if col.empty:
            continue
        pos, neg = col[col > 0], col[col < 0]
        out[str(exp)] = {
            "call_wall": float(pos.idxmax()) if not pos.empty else None,
            "put_wall": float(neg.idxmin()) if not neg.empty else None,
            "net": float(col.sum()),
        }
    return out


def support_resistance(matrix: pd.DataFrame, spot: float) -> dict[str, dict[str, float | None]]:
    """Per expiry: the largest positive strike at or below spot (support) and the largest
    positive strike above spot (resistance). The call wall is whichever of the two is
    bigger; splitting it by side of spot says how it is likely to act right now."""
    out: dict[str, dict[str, float | None]] = {}
    for exp in matrix.columns:
        pos = matrix[exp].dropna()
        pos = pos[pos > 0]
        below, above = pos[pos.index <= spot], pos[pos.index > spot]
        out[str(exp)] = {
            "support": None if below.empty else float(below.idxmax()),
            "resistance": None if above.empty else float(above.idxmax()),
        }
    return out


def summary(latest: pd.DataFrame, *, spot: float | None, as_of: datetime,
            today: date | None = None, n_strikes: int = 16, top: int = 3) -> dict | None:
    """Headline gamma read for the nearest live expiry, in dollars per $1 move.

    Net, the call and put walls, the largest positive strikes, and the highest
    negative strike below spot. None when nothing usable is in the snapshot.
    """
    table = gex_table(latest, spot=spot, as_of=as_of, today=today)
    expiries = available_expiries(table)
    if not expiries:
        return None
    exp = expiries[0]
    matrix = gex_matrix(table, spot=float(spot), expiries=[exp], n_strikes=n_strikes)
    if matrix.empty:
        return None
    col = matrix[exp].dropna()
    w = walls(matrix)[exp]
    below = col[(col.index < float(spot)) & (col < 0)]
    sr = support_resistance(matrix, float(spot))[exp]
    return {
        "expiry": exp,
        "spot": float(spot),
        "net": w["net"],
        "call_wall": w["call_wall"],
        "call_wall_gex": None if w["call_wall"] is None else float(col[w["call_wall"]]),
        "put_wall": w["put_wall"],
        "put_wall_gex": None if w["put_wall"] is None else float(col[w["put_wall"]]),
        "support": sr["support"],
        "support_gex": None if sr["support"] is None else float(col[sr["support"]]),
        "resistance": sr["resistance"],
        "resistance_gex": None if sr["resistance"] is None else float(col[sr["resistance"]]),
        "top": [(float(k), float(v))
                for k, v in col[col > 0].sort_values(ascending=False).head(top).items()],
        "first_negative_below_spot": None if below.empty else float(below.index.max()),
        "as_of": as_of,
        "coverage": coverage(table),
    }


def nearest_strike(matrix: pd.DataFrame, spot: float) -> float | None:
    if matrix is None or matrix.empty:
        return None
    return float(min(matrix.index, key=lambda k: abs(float(k) - float(spot))))


def fmt_money(v: Any) -> str:
    """25.40M / -399,216 / 8,356 — blank for missing, never "-0"."""
    x = _f(v)
    if x is None:
        return ""
    a = abs(x)
    if a >= 1e9:
        return f"{x / 1e9:.2f}B"
    if a >= 1e6:
        return f"{x / 1e6:.2f}M"
    if a < 0.5:
        return "0"
    return f"{x:,.0f}"
