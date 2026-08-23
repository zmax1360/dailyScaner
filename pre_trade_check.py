"""
pre_trade_check.py — Pre-trade R:R calculator for the journal.

Converts chart levels into option outcomes, scores reward-to-risk against
fixed gates, and persists each check for later comparison with actuals.
Pure compute lives here; Streamlit render is in app.py.
"""

from __future__ import annotations

import json
import math
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

_BASE = os.path.dirname(os.path.abspath(__file__))
CHECKS_PATH = os.path.join(_BASE, "data", "journal", "pre_trade_checks.json")
PREFS_PATH = os.path.join(_BASE, "data", "journal", "pre_trade_prefs.json")
SCANS_PATH = os.path.join(_BASE, "data", "journal", "pretrade_scans.json")

STALE_QUOTE_MINUTES = 10
MAX_STORED_SCANS = 50

# Chart half — never prefilled from the scanner (or ATR, or a measured move).
CHART_FIELDS = frozenset({
    "underlying",
    "target_distance",
    "invalidation_distance",
    "hold_hours",
    "entry_window",
})

PREFILL_CONTRACT_FIELDS = (
    "symbol",
    "direction",
    "strike",
    "dte",
    "bid",
    "ask",
    "delta",
    "theta",
    "theta_units",
    "open_interest",
)

PREFILL_WIDGET_KEYS = {
    "symbol": "ptc_symbol",
    "direction": "ptc_direction",
    "strike": "ptc_strike",
    "dte": "ptc_dte",
    "bid": "ptc_bid",
    "ask": "ptc_ask",
    "delta": "ptc_delta",
    "theta": "ptc_theta",
    "theta_units": "ptc_theta_units",
    "open_interest": "ptc_oi",
}

TRADING_HOURS_PER_SESSION = 6.5
SLIPPAGE = 0.02
LOSS_HOLD_FRACTION = 0.6
TIME_STOP_FRACTION = 0.65
ACCOUNT_RISK_FRAC = 0.01
CAPITAL_OUTLAY_FLAG = 0.10

DELTA_MIN = 0.35
DELTA_MAX = 0.50
SPREAD_PCT_MAX = 0.03
OI_MIN = 500
RATIO_MIN = 2.0
RATIO_AMBER = 1.5

ENTRY_WINDOWS = (
    "09:45–11:30",
    "11:30–14:00",
    "14:00–15:30",
    "other",
)
ALLOWED_WINDOWS = frozenset({"09:45–11:30", "14:00–15:30"})
THETA_UNITS = ("per day", "per hour")
EXIT_REASONS = ("target", "price stop", "time stop", "other")

# One session = 6.5 trading hours. T7 treats hold ≥ 6.5h as “≥ 1 day”.
HOURS_PER_DAY = TRADING_HOURS_PER_SESSION
DISTANCE_ERROR = "Enter a positive distance in dollars."
NO_CONTRACT_REASON = "no contract data."
LOSS_CLAMP_NOTE = (
    "stop distance exceeds premium — this is a total loss, not a partial one."
)
ACCOUNT_SIZE_MSG = "enter account size."


@dataclass
class PreTradeInputs:
    symbol: str = ""
    direction: str = "CALL"
    underlying: float | None = None
    target_distance: float | None = None
    invalidation_distance: float | None = None
    hold_hours: float | None = None
    entry_window: str = "09:45–11:30"
    strike: float | None = None
    dte: int | None = None
    bid: float | None = None
    ask: float | None = None
    delta: float | None = None
    theta: float | None = None
    theta_units: str = "per day"
    open_interest: float | None = None
    account_size: float | None = None


def _now_et() -> datetime:
    return datetime.now(ET)


def _now_iso() -> str:
    return _now_et().isoformat(timespec="seconds")


def _f(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return v


def _i(x) -> int | None:
    v = _f(x)
    if v is None:
        return None
    return int(v)


def _abs(x) -> float | None:
    v = _f(x)
    if v is None:
        return None
    return abs(v)


def _div(n: float | None, d: float | None) -> float | None:
    if n is None or d is None or d == 0:
        return None
    out = n / d
    return out if math.isfinite(out) else None


def _finite(x) -> float | None:
    v = _f(x)
    if v is None or not math.isfinite(v):
        return None
    return v


def _norm_window(raw: str) -> str:
    s = str(raw or "").strip()
    return s.replace("-", "–") if s else s


def _positive_distance(x) -> tuple[float | None, str | None]:
    """Return (value, error). Zero/negative/missing → error."""
    v = _f(x)
    if v is None or v <= 0:
        return None, DISTANCE_ERROR
    return v, None


def implied_level_prices(
    direction: str,
    underlying: float | None,
    target_distance: float | None,
    invalidation_distance: float | None,
) -> tuple[float | None, float | None]:
    """Chart prices implied by dollar distances. CALL up-target; PUT down-target."""
    if underlying is None:
        return None, None
    tgt = None
    stop = None
    if target_distance is not None and target_distance > 0:
        tgt = (
            underlying + target_distance
            if direction == "CALL"
            else underlying - target_distance
        )
    if invalidation_distance is not None and invalidation_distance > 0:
        stop = (
            underlying - invalidation_distance
            if direction == "CALL"
            else underlying + invalidation_distance
        )
    return tgt, stop


def min_dte_for_hold(hold_hours: float) -> int:
    """Minimum DTE the thesis duration is allowed to sit on."""
    if hold_hours < 0.5:
        return 0
    if hold_hours < 3.0:
        return 1
    if hold_hours < HOURS_PER_DAY:
        return 2
    return 5


def ratio_color(ratio: float | None) -> str:
    if ratio is None:
        return "#9e9e9e"
    if ratio >= RATIO_MIN:
        return "#00c853"
    if ratio >= RATIO_AMBER:
        return "#ff6d00"
    return "#d50000"


def format_money(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "—"
    if signed:
        return f"${x:+.2f}"
    return f"${x:.2f}"


def format_loss(x: float | None) -> str:
    """Loss figures always render with a minus sign (never a leading +)."""
    if x is None:
        return "—"
    return f"−${abs(float(x)):.2f}"


def format_pct(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}%"


def format_ratio(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:.1f} : 1"


def format_num(x: float | None, dp: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:.{dp}f}"


def compute_pre_trade(inp: PreTradeInputs) -> dict[str, Any]:
    """
    Derive every intermediate, then score gates. Never invent a price.

    Returns a JSON-ready dict: inputs, derived, gates, verdict, plan.
    Loss figures are stored signed-negative. Percentages used in the ratio
    are magnitudes (positive).
    """
    direction = str(inp.direction or "CALL").strip().upper()
    if direction not in ("CALL", "PUT"):
        direction = "CALL"
    window = _norm_window(inp.entry_window)
    units = str(inp.theta_units or "per day").strip().lower()
    if units not in ("per day", "per hour"):
        units = "per day"

    underlying = _finite(inp.underlying)
    target_dist, target_err = _positive_distance(inp.target_distance)
    inval_dist, inval_err = _positive_distance(inp.invalidation_distance)
    field_errors: dict[str, str] = {}
    if target_err:
        field_errors["target_distance"] = target_err
    if inval_err:
        field_errors["invalidation_distance"] = inval_err

    hold_hours = _finite(inp.hold_hours)
    strike = _finite(inp.strike)
    dte = _i(inp.dte)
    bid = _finite(inp.bid)
    ask = _finite(inp.ask)
    abs_delta = _abs(inp.delta)
    abs_theta = _abs(inp.theta)
    oi = _finite(inp.open_interest)
    account = _finite(inp.account_size)

    implied_target, implied_stop = implied_level_prices(
        direction, underlying, target_dist, inval_dist,
    )

    mid = None
    if bid is not None and ask is not None:
        mid = _finite((bid + ask) / 2.0)
    no_contract = mid is None or mid <= 0

    def _blank_derived() -> dict[str, Any]:
        return {
            "mid": None,
            "spread": None,
            "spread_pct": None,
            "intrinsic": None,
            "extrinsic": None,
            "extrinsic_pct": None,
            "fully_extrinsic": False,
            "theta_hr": None,
            "move_to_target": None,
            "move_to_stop": None,
            "theta_hold": None,
            "holding_cost_win": None,
            "delta_gain": None,
            "gain_per_contract": None,
            "theta_hold_loss": None,
            "slippage": SLIPPAGE,
            "loss_hold_fraction": LOSS_HOLD_FRACTION,
            "holding_cost_loss": None,
            "delta_loss": None,
            "loss_per_contract": None,
            "loss_clamped": False,
            "loss_clamp_note": None,
            "value_at_target": None,
            "value_at_stop": None,
            "gain_pct": None,
            "loss_pct": None,
            "ratio": None,
            "breakeven_move": None,
            "breakeven_win_rate": None,
            "time_stop_minutes": None,
            "risk_per_contract": None,
            "max_risk": None,
            "contracts": None,
            "capital_deployed": None,
            "pct_of_account": None,
            "large_capital": False,
            "need_dte": None,
            "abs_delta": abs_delta,
            "abs_theta": abs_theta,
            "implied_target_price": implied_target,
            "implied_stop_price": implied_stop,
        }

    if no_contract:
        derived = _blank_derived()
    else:
        spread = _finite(ask - bid) if bid is not None and ask is not None else None
        spread_pct = _div(spread, mid)

        intrinsic = None
        if underlying is not None and strike is not None:
            if direction == "PUT":
                intrinsic = max(strike - underlying, 0.0)
            else:
                intrinsic = max(underlying - strike, 0.0)

        extrinsic = None
        if intrinsic is not None:
            extrinsic = _finite(mid - intrinsic)
        extrinsic_pct = _div(extrinsic, mid)
        fully_extrinsic = (
            extrinsic_pct is not None and round(extrinsic_pct * 100, 1) == 100.0
        )

        theta_hr = None
        if abs_theta is not None:
            theta_hr = (
                abs_theta / TRADING_HOURS_PER_SESSION
                if units == "per day"
                else abs_theta
            )
            theta_hr = _finite(theta_hr)

        # Distances are the moves. Missing/invalid → None (not a silent 0).
        move_to_target = abs(target_dist) if target_dist is not None else None
        move_to_stop = abs(inval_dist) if inval_dist is not None else None

        hold_for_cost = 0.0 if hold_hours is None else hold_hours
        theta_for_cost = 0.0 if theta_hr is None else theta_hr
        theta_hold = _finite(theta_for_cost * hold_for_cost)

        holding_cost_win = None
        if theta_hold is not None and spread is not None:
            holding_cost_win = _finite(theta_hold + spread)

        delta_gain = None
        if move_to_target is not None and abs_delta is not None:
            delta_gain = _finite(move_to_target * abs_delta)

        gain_per_contract = None
        if delta_gain is not None and holding_cost_win is not None:
            gain_per_contract = _finite(delta_gain - holding_cost_win)

        theta_hold_loss = None
        if theta_hold is not None:
            theta_hold_loss = _finite(theta_hold * LOSS_HOLD_FRACTION)

        holding_cost_loss = None
        if theta_hold_loss is not None and spread is not None:
            holding_cost_loss = _finite(theta_hold_loss + spread + SLIPPAGE)

        delta_loss = None
        if move_to_stop is not None and abs_delta is not None:
            delta_loss = _finite(move_to_stop * abs_delta)

        loss_mag = None
        loss_clamped = False
        if delta_loss is not None and holding_cost_loss is not None:
            raw_loss = delta_loss + holding_cost_loss
            if raw_loss > mid:
                loss_mag = mid
                loss_clamped = True
            else:
                loss_mag = _finite(raw_loss)

        # Stored signed-negative. Magnitudes used for pct / ratio / sizing.
        loss_per_contract = None if loss_mag is None else -abs(loss_mag)
        value_at_target = None
        if gain_per_contract is not None:
            value_at_target = _finite(mid + gain_per_contract)
        value_at_stop = None
        if loss_mag is not None:
            value_at_stop = max(_finite(mid - abs(loss_mag)) or 0.0, 0.0)

        gain_pct = _div(gain_per_contract, mid)
        loss_pct = _div(loss_mag, mid)  # positive magnitude
        ratio = _div(gain_pct, loss_pct)

        breakeven_move = None
        if spread is not None and theta_hold is not None and abs_delta:
            breakeven_move = _div(spread + theta_hold, abs_delta)

        breakeven_win_rate = None
        if gain_pct is not None and loss_pct is not None:
            den = gain_pct + loss_pct
            if den != 0:
                breakeven_win_rate = _finite(loss_pct / den)

        time_stop_minutes = None
        if hold_hours is not None:
            time_stop_minutes = int(round(hold_hours * 60.0 * TIME_STOP_FRACTION))

        risk_per_contract = None if loss_mag is None else -abs(loss_mag) * 100.0

        max_risk = None
        if account is not None and account > 0:
            max_risk = account * ACCOUNT_RISK_FRAC

        contracts = None
        if (
            max_risk is not None
            and loss_mag is not None
            and loss_mag > 0
            and max_risk > 0
        ):
            risk_abs = abs(loss_mag) * 100.0
            contracts = int(math.floor(max_risk / risk_abs))
            if contracts < 0:
                contracts = 0

        capital_deployed = None
        if contracts is not None:
            capital_deployed = _finite(contracts * mid * 100.0)
        pct_of_account = _div(capital_deployed, account if account and account > 0 else None)
        large_capital = bool(
            pct_of_account is not None and pct_of_account > CAPITAL_OUTLAY_FLAG
        )

        derived = {
            "mid": mid,
            "spread": spread,
            "spread_pct": spread_pct,
            "intrinsic": intrinsic,
            "extrinsic": extrinsic,
            "extrinsic_pct": extrinsic_pct,
            "fully_extrinsic": fully_extrinsic,
            "theta_hr": theta_hr,
            "move_to_target": move_to_target,
            "move_to_stop": move_to_stop,
            "theta_hold": theta_hold,
            "holding_cost_win": holding_cost_win,
            "delta_gain": delta_gain,
            "gain_per_contract": gain_per_contract,
            "theta_hold_loss": theta_hold_loss,
            "slippage": SLIPPAGE,
            "loss_hold_fraction": LOSS_HOLD_FRACTION,
            "holding_cost_loss": holding_cost_loss,
            "delta_loss": delta_loss,
            "loss_per_contract": loss_per_contract,
            "loss_clamped": loss_clamped,
            "loss_clamp_note": LOSS_CLAMP_NOTE if loss_clamped else None,
            "value_at_target": value_at_target,
            "value_at_stop": value_at_stop,
            "gain_pct": gain_pct,
            "loss_pct": loss_pct,
            "ratio": ratio,
            "breakeven_move": breakeven_move,
            "breakeven_win_rate": breakeven_win_rate,
            "time_stop_minutes": time_stop_minutes,
            "risk_per_contract": risk_per_contract,
            "max_risk": max_risk,
            "contracts": contracts,
            "capital_deployed": capital_deployed,
            "pct_of_account": pct_of_account,
            "large_capital": large_capital,
            "need_dte": None,
            "abs_delta": abs_delta,
            "abs_theta": abs_theta,
            "implied_target_price": implied_target,
            "implied_stop_price": implied_stop,
        }

    # Never leak NaN / Inf into the result.
    for key, val in list(derived.items()):
        if isinstance(val, float) and not math.isfinite(val):
            derived[key] = None

    need_dte = min_dte_for_hold(hold_hours) if hold_hours is not None else None
    derived["need_dte"] = need_dte
    dte_ok = (
        dte is not None and need_dte is not None and dte >= need_dte
        if hold_hours is not None
        else False
    )
    dte_fail_msg = None
    if hold_hours is not None and not dte_ok:
        shown = need_dte if need_dte is not None else "?"
        dte_fail_msg = (
            f"Thesis needs {hold_hours:g} hours; this contract's deadline "
            f"is too short. Consider DTE ≥ {shown}."
        )

    gates: list[dict[str, Any]] = []

    def _gate(
        name: str,
        passed: bool | None,
        value: str,
        fail_msg: str | None = None,
    ) -> None:
        if passed is True:
            badge = "PASS"
        elif passed is False:
            badge = "FAIL"
        else:
            badge = "—"
        gates.append({
            "name": name,
            "passed": passed,
            "badge": badge,
            "value": value,
            "fail_msg": None if passed is True else fail_msg,
        })

    d = derived
    _gate(
        "Delta in range",
        abs_delta is not None and DELTA_MIN <= abs_delta <= DELTA_MAX,
        f"|δ|={format_num(abs_delta, 2)}" if abs_delta is not None else "—",
        f"Need {DELTA_MIN:.2f} ≤ |delta| ≤ {DELTA_MAX:.2f}",
    )
    spread_ok = (
        d.get("spread") is not None
        and d["spread"] >= 0
        and d.get("spread_pct") is not None
        and d["spread_pct"] <= SPREAD_PCT_MAX
    )
    _gate(
        "Spread acceptable",
        False if no_contract else spread_ok,
        format_pct(d.get("spread_pct")),
        f"spread_pct must be ≤ {format_pct(SPREAD_PCT_MAX)}"
        + (
            " (bid/ask inverted)"
            if d.get("spread") is not None and d["spread"] < 0
            else ""
        ),
    )
    _gate(
        "Liquidity",
        oi is not None and oi >= OI_MIN,
        f"{int(oi):,}" if oi is not None else "—",
        f"open interest must be ≥ {OI_MIN:,}",
    )
    _gate(
        "Reward:risk",
        (d.get("ratio") is not None and d["ratio"] >= RATIO_MIN)
        if not no_contract else False,
        format_ratio(d.get("ratio")),
        f"ratio must be ≥ {RATIO_MIN:.1f} : 1",
    )
    _gate(
        "DTE matches hold",
        bool(dte_ok),
        f"DTE {dte if dte is not None else '—'} · need ≥ {need_dte if need_dte is not None else '—'}",
        dte_fail_msg or "enter hold hours and DTE",
    )
    _gate(
        "Trading window",
        window in ALLOWED_WINDOWS,
        window or "—",
        "entry window must be 09:45–11:30 or 14:00–15:30",
    )
    if account is None or account <= 0:
        _gate("Position size", None, "—", ACCOUNT_SIZE_MSG)
    else:
        n_contracts = d.get("contracts")
        _gate(
            "Position size",
            n_contracts is not None and n_contracts >= 1,
            (
                f"{int(n_contracts)} contract{'s' if n_contracts != 1 else ''}"
                if n_contracts is not None else "—"
            ),
            "1% risk cannot fund a single contract",
        )

    failed = [g for g in gates if g["passed"] is False]
    all_pass = all(g["passed"] is True for g in gates)
    verdict = "TAKE" if all_pass else "SKIP"

    skip_reason = None
    if no_contract:
        skip_reason = NO_CONTRACT_REASON
    elif field_errors:
        skip_reason = DISTANCE_ERROR
    elif verdict == "SKIP" and not failed:
        skip_reason = ACCOUNT_SIZE_MSG

    plan = None
    if verdict == "TAKE":
        n_contracts = int(d.get("contracts") or 0)
        risk_total = None
        if d.get("risk_per_contract") is not None:
            risk_total = n_contracts * abs(d["risk_per_contract"])
        plan = (
            f"{str(inp.symbol or '').strip().upper() or 'SYMBOL'}  "
            f"{direction}  {format_num(strike, 1)}  DTE {dte if dte is not None else '—'}\n"
            f"Entry           {format_money(underlying)}\n"
            f"Target          {format_money(implied_target)}"
            f"       →  option ~{format_money(d.get('value_at_target'))}\n"
            f"Invalidation    {format_money(implied_stop)}"
            f" →  option ~{format_money(d.get('value_at_stop'))}\n"
            f"Time stop       {d.get('time_stop_minutes') if d.get('time_stop_minutes') is not None else '—'} min\n"
            f"Contracts       {n_contracts}    risk {format_money(risk_total)}\n"
            f"Ratio           {format_ratio(d.get('ratio'))}"
        )

    inputs_out = asdict(inp)
    inputs_out["direction"] = direction
    inputs_out["theta_units"] = units
    inputs_out["entry_window"] = window
    inputs_out["symbol"] = str(inp.symbol or "").strip().upper()

    return {
        "inputs": inputs_out,
        "derived": derived,
        "gates": gates,
        "failed": [g["name"] for g in failed],
        "field_errors": field_errors,
        "skip_reason": skip_reason,
        "verdict": verdict,
        "plan": plan,
    }


def _read_json(path: str, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as fh:
            data = json.load(fh)
        return data
    except Exception:
        return default


def _write_json(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


# ── Scanner → Pre-Trade bridge (read-only UI; no scoring changes) ─────────────

def parse_scan_ts(ts) -> datetime | None:
    """Parse a scan timestamp into aware ET. Accepts ISO, archive, or scan_id."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        dt = ts
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ET)
        return dt.astimezone(ET)
    s = str(ts).strip()
    if not s:
        return None
    s = s.replace(" ET", "").replace("ET", "").strip()
    dt = None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        dt = None
    if dt is None:
        for fmt, n in (
            ("%Y-%m-%d %H:%M:%S", 19),
            ("%Y-%m-%d %H:%M", 16),
            ("%Y%m%dT%H%M%S", 15),
        ):
            try:
                dt = datetime.strptime(s[:n], fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    return dt.astimezone(ET)


def make_scan_id(run_timestamp: str | datetime | None = None) -> str:
    """Compact scan id with no colons: YYYYMMDDTHHMMSS in ET."""
    dt = parse_scan_ts(run_timestamp) or _now_et()
    return dt.astimezone(ET).strftime("%Y%m%dT%H%M%S")


def candidate_ref(scan_id: str, contract_id: str) -> str:
    return f"{scan_id}:{contract_id}"


def parse_candidate_ref(raw: str | None) -> tuple[str, str] | None:
    """Split `scan_id:contract_id` on the first colon."""
    if not raw or not isinstance(raw, str):
        return None
    scan_id, sep, contract_id = raw.partition(":")
    scan_id, contract_id = scan_id.strip(), contract_id.strip()
    if not sep or not scan_id or not contract_id:
        return None
    return scan_id, contract_id


def _row_get(row: Any, *keys, default=None):
    for key in keys:
        if hasattr(row, "get"):
            try:
                if key in row:
                    return row.get(key)
            except TypeError:
                pass
            val = row.get(key)
            if val is not None:
                return val
        try:
            if key in row.index:
                return row[key]
        except Exception:
            continue
    return default


def contract_prefill_from_row(row: Any, *, ticker: str) -> dict[str, Any]:
    """
    Contract-half fields only. Chart fields are never populated — not from
    spot, ATR, or anything else on the row.
    """
    side = str(_row_get(row, "side", "Side", default="CALL") or "CALL").upper()
    if side not in ("CALL", "PUT"):
        side = "CALL"
    dte = _i(_row_get(row, "dte", "DTE"))
    theta = _f(_row_get(row, "theta", "Theta"))
    units = str(_row_get(row, "theta_units", default="per day") or "per day")
    if units not in THETA_UNITS:
        units = "per day"
    prefill = {
        "symbol": str(ticker or _row_get(row, "symbol", "Ticker", default="") or "").upper(),
        "direction": side,
        "strike": _f(_row_get(row, "strike", "Strike")),
        "dte": dte,
        "bid": _f(_row_get(row, "bid", "Bid")),
        "ask": _f(_row_get(row, "ask", "Ask")),
        "delta": _f(_row_get(row, "delta", "Delta")),
        "theta": theta,
        "theta_units": units,
        "open_interest": _f(_row_get(row, "openInterest", "open_interest", "OI")),
    }
    for chart_key in CHART_FIELDS:
        prefill.pop(chart_key, None)
    return prefill


def _lookup_scored_row(scored_df, side: str, strike, expiry):
    if scored_df is None or getattr(scored_df, "empty", True):
        return None
    side_u = str(side).upper()
    try:
        strike_k = round(float(strike), 4)
    except (TypeError, ValueError):
        return None
    expiry_s = str(expiry or "")
    for _, r in scored_df.iterrows():
        try:
            if (
                str(r.get("side") or "").upper() == side_u
                and round(float(r.get("strike")), 4) == strike_k
                and str(r.get("expiry") or "") == expiry_s
            ):
                return r
        except (TypeError, ValueError):
            continue
    return None


def build_scan_snapshot(
    ticker: str,
    scored_df,
    ranked_df,
    run_timestamp: str | datetime | None,
) -> dict[str, Any]:
    """Ranked candidate snapshot for Check this. Does not change scores."""
    from best_value_ui import contract_key_from_row

    dt = parse_scan_ts(run_timestamp) or _now_et()
    scan_id = make_scan_id(dt)
    scan_ts = dt.astimezone(ET).isoformat(timespec="seconds")
    contracts: dict[str, Any] = {}
    if ranked_df is None or getattr(ranked_df, "empty", True):
        return {
            "scan_id": scan_id,
            "scan_ts": scan_ts,
            "ticker": str(ticker or "").upper(),
            "contracts": contracts,
        }
    ranked = ranked_df.reset_index(drop=True)
    for rank, (_, row) in enumerate(ranked.iterrows(), start=1):
        cid = contract_key_from_row(row)
        full = _lookup_scored_row(
            scored_df, row.get("side"), row.get("strike"), row.get("expiry"),
        )
        source = full if full is not None else row
        score = _f(source.get("Value_Score") if hasattr(source, "get") else None)
        if score is None:
            score = _f(row.get("Value_Score"))
        contracts[cid] = {
            "rank": rank,
            "score": score,
            "prefill": contract_prefill_from_row(source, ticker=ticker),
        }
    return {
        "scan_id": scan_id,
        "scan_ts": scan_ts,
        "ticker": str(ticker or "").upper(),
        "contracts": contracts,
    }


def store_scan_snapshot(snapshot: dict[str, Any]) -> None:
    scan_id = str((snapshot or {}).get("scan_id") or "").strip()
    if not scan_id:
        return
    scans = _read_json(SCANS_PATH, {})
    if not isinstance(scans, dict):
        scans = {}
    if scan_id in scans:
        del scans[scan_id]
    scans[scan_id] = snapshot
    while len(scans) > MAX_STORED_SCANS:
        oldest = next(iter(scans))
        del scans[oldest]
    _write_json(SCANS_PATH, scans)


def load_scan_contract(scan_id: str, contract_id: str) -> dict[str, Any] | None:
    scans = _read_json(SCANS_PATH, {})
    if not isinstance(scans, dict):
        return None
    snap = scans.get(str(scan_id))
    if not isinstance(snap, dict):
        return None
    contracts = snap.get("contracts") or {}
    entry = contracts.get(str(contract_id))
    if not isinstance(entry, dict):
        return None
    prefill = dict(entry.get("prefill") or {})
    for chart_key in CHART_FIELDS:
        prefill.pop(chart_key, None)
    return {
        "scan_id": snap.get("scan_id") or scan_id,
        "contract_id": contract_id,
        "scan_ts": snap.get("scan_ts"),
        "ticker": snap.get("ticker"),
        "rank": entry.get("rank"),
        "score": entry.get("score"),
        "prefill": prefill,
    }


def _same_prefill_value(original, current) -> bool:
    if original is None and current in (None, "", 0, 0.0):
        return True
    if current is None and original in (None, "", 0, 0.0):
        return True
    fo, fc = _f(original), _f(current)
    orig_is_plain_str = isinstance(original, str) and fo is None
    cur_is_plain_str = isinstance(current, str) and fc is None
    if orig_is_plain_str or cur_is_plain_str or (fo is None and fc is None):
        return (
            str(original or "").strip().upper()
            == str(current or "").strip().upper()
        )
    if fo is None or fc is None:
        return False
    return abs(fo - fc) < 1e-9


def prefill_overrides(original: dict | None, current: dict | None) -> dict[str, Any]:
    """Fields the user changed off the scanner prefill, and to what."""
    original = original or {}
    current = current or {}
    out: dict[str, Any] = {}
    for field in PREFILL_CONTRACT_FIELDS:
        if field not in original:
            continue
        old, new = original.get(field), current.get(field)
        if not _same_prefill_value(old, new):
            out[field] = {"from": old, "to": new}
    return out


def current_prefill_values(
    *,
    symbol: str,
    direction: str,
    strike,
    dte,
    bid,
    ask,
    delta,
    theta,
    theta_units: str,
    open_interest,
) -> dict[str, Any]:
    return {
        "symbol": str(symbol or "").upper(),
        "direction": str(direction or "").upper(),
        "strike": _f(strike),
        "dte": _i(dte),
        "bid": _f(bid),
        "ask": _f(ask),
        "delta": _f(delta),
        "theta": _f(theta),
        "theta_units": theta_units,
        "open_interest": _f(open_interest),
    }


def scan_age_minutes(scan_ts, now: datetime | None = None) -> float | None:
    dt = parse_scan_ts(scan_ts)
    if dt is None:
        return None
    now = now or _now_et()
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    return (now.astimezone(ET) - dt).total_seconds() / 60.0


def is_stale(
    scan_ts,
    now: datetime | None = None,
    *,
    limit_minutes: float = STALE_QUOTE_MINUTES,
) -> bool:
    age = scan_age_minutes(scan_ts, now)
    return age is not None and age > limit_minutes


def staleness_warning(
    scan_ts,
    now: datetime | None = None,
    *,
    limit_minutes: float = STALE_QUOTE_MINUTES,
) -> str | None:
    age = scan_age_minutes(scan_ts, now)
    if age is None or age <= limit_minutes:
        return None
    n = max(1, int(round(age)))
    return f"quotes are {n} minutes old — refresh bid/ask before trusting the ratio."


def format_prefill_banner(scan_ts, rank, score) -> str:
    dt = parse_scan_ts(scan_ts)
    when = dt.strftime("%Y-%m-%d %H:%M") if dt is not None else (str(scan_ts or "—"))
    try:
        rank_s = f"{int(rank):02d}"
    except (TypeError, ValueError):
        rank_s = "—"
    sc = _f(score)
    score_s = f"{sc:.2f}" if sc is not None else "—"
    return f"From scan {when} · rank {rank_s} · score {score_s}"


def apply_candidate_to_session(st, payload: dict[str, Any]) -> None:
    """Fill contract widgets; leave chart widgets blank. Never fill target/inval."""
    prefill = dict((payload or {}).get("prefill") or {})
    for chart_key in CHART_FIELDS:
        prefill.pop(chart_key, None)

    st.session_state["ptc_underlying"] = 0.0
    st.session_state["ptc_target"] = 0.0
    st.session_state["ptc_invalidation"] = 0.0
    st.session_state["ptc_hold"] = 0.0
    st.session_state["ptc_window"] = ENTRY_WINDOWS[0]

    st.session_state["ptc_symbol"] = str(prefill.get("symbol") or "").upper()
    direction = str(prefill.get("direction") or "CALL").upper()
    st.session_state["ptc_direction"] = (
        direction if direction in ("CALL", "PUT") else "CALL"
    )

    def _set_num(field: str, widget: str, default: float = 0.0) -> None:
        v = _f(prefill.get(field))
        st.session_state[widget] = float(v) if v is not None else default

    _set_num("strike", "ptc_strike")
    dte = _i(prefill.get("dte"))
    st.session_state["ptc_dte"] = int(dte) if dte is not None else 0
    _set_num("bid", "ptc_bid")
    _set_num("ask", "ptc_ask")
    _set_num("delta", "ptc_delta")
    _set_num("theta", "ptc_theta")
    units = prefill.get("theta_units") or "per day"
    st.session_state["ptc_theta_units"] = (
        units if units in THETA_UNITS else THETA_UNITS[0]
    )
    _set_num("open_interest", "ptc_oi")

    st.session_state["ptc_prefill_original"] = {
        k: prefill.get(k) for k in PREFILL_CONTRACT_FIELDS
    }
    st.session_state["ptc_scan_meta"] = {
        "scan_id": payload.get("scan_id"),
        "contract_id": payload.get("contract_id"),
        "scanner_rank": payload.get("rank"),
        "scanner_score": payload.get("score"),
        "scan_ts": payload.get("scan_ts"),
    }


def read_candidate_query(st) -> str | None:
    try:
        val = st.query_params.get("candidate")
        if isinstance(val, list):
            val = val[0] if val else None
        return str(val).strip() if val else None
    except Exception:
        try:
            vals = (st.experimental_get_query_params() or {}).get("candidate") or []
            return str(vals[0]).strip() if vals else None
        except Exception:
            return None


def set_candidate_query(st, ref: str) -> None:
    try:
        st.query_params["candidate"] = ref
    except Exception:
        st.experimental_set_query_params(candidate=ref)


def load_prefs() -> dict[str, Any]:
    data = _read_json(PREFS_PATH, {})
    if not isinstance(data, dict):
        return {}
    return data


def save_prefs(*, account_size: float | None) -> None:
    prefs = load_prefs()
    if account_size is not None and math.isfinite(float(account_size)):
        prefs["account_size"] = float(account_size)
    _write_json(PREFS_PATH, prefs)


def load_checks() -> list[dict[str, Any]]:
    data = _read_json(CHECKS_PATH, [])
    if not isinstance(data, list):
        return []
    return data


def save_check(
    result: dict[str, Any],
    *,
    taken: bool = False,
    scan_id: str | None = None,
    contract_id: str | None = None,
    scanner_rank: int | None = None,
    scanner_score: float | None = None,
    prefill_overrides: dict | None = None,
) -> dict[str, Any]:
    """Append one snapshot. Does not overwrite prior checks."""
    rank = _i(scanner_rank)
    score = _f(scanner_score)
    row = {
        "check_id": str(uuid.uuid4()),
        "ts_et": _now_iso(),
        "inputs": result.get("inputs") or {},
        "derived": result.get("derived") or {},
        "gates": result.get("gates") or [],
        "failed": result.get("failed") or [],
        "skip_reason": result.get("skip_reason"),
        "verdict": result.get("verdict"),
        "plan": result.get("plan"),
        "taken": bool(taken),
        "actual_exit_price": None,
        "exit_reason": None,
        "scan_id": str(scan_id).strip() if scan_id else None,
        "contract_id": str(contract_id).strip() if contract_id else None,
        "scanner_rank": rank,
        "scanner_score": score,
        "prefill_overrides": dict(prefill_overrides or {}),
    }
    rows = load_checks()
    rows.insert(0, row)
    _write_json(CHECKS_PATH, rows)
    return row


def update_check(
    check_id: str,
    *,
    taken: bool | None = None,
    actual_exit_price: float | None | object = ...,
    exit_reason: str | None | object = ...,
) -> bool:
    """Patch a saved check. Ellipsis means 'leave unchanged'."""
    rows = load_checks()
    found = False
    for row in rows:
        if str(row.get("check_id")) != str(check_id):
            continue
        found = True
        if taken is not None:
            row["taken"] = bool(taken)
        if actual_exit_price is not ...:
            row["actual_exit_price"] = (
                None if actual_exit_price is None else _f(actual_exit_price)
            )
        if exit_reason is not ...:
            reason = None if exit_reason in (None, "") else str(exit_reason)
            if reason is not None and reason not in EXIT_REASONS:
                reason = "other"
            row["exit_reason"] = reason
        break
    if not found:
        return False
    _write_json(CHECKS_PATH, rows)
    return True


# ── Streamlit page ────────────────────────────────────────────────────────────

def _still_prefilled(st, field: str) -> bool:
    original = st.session_state.get("ptc_prefill_original") or {}
    if field not in original:
        return False
    orig = original.get(field)
    if orig is None or orig == "":
        return False
    widget = PREFILL_WIDGET_KEYS.get(field)
    if not widget:
        return False
    return _same_prefill_value(original.get(field), st.session_state.get(widget))


def _marked_kwargs(st, field: str, label: str) -> dict:
    """Left-border + 'from scanner' while the value still matches prefill."""
    if not _still_prefilled(st, field):
        return {}
    st.markdown(
        f'<div style="border-left:3px solid #26a69a;padding:0.05rem 0 0.1rem 0.55rem;'
        f'margin:0.2rem 0 0.05rem">'
        f'<span style="font-size:0.88rem">{label}</span>'
        f' <span style="color:#26a69a;font-size:0.72rem">from scanner</span></div>',
        unsafe_allow_html=True,
    )
    return {"label_visibility": "collapsed"}


def _ptc_line(
    st, label: str, value: str, *,
    note: str | None = None, strong: bool = False, color: str = "#eee",
) -> None:
    note_html = (
        f' <span style="color:#888;font-size:0.78rem">{note}</span>' if note else ""
    )
    weight = "700" if strong else "400"
    st.markdown(
        f'<div style="display:flex;justify-content:space-between;align-items:baseline;'
        f'gap:0.6rem;font-family:ui-monospace,Menlo,monospace;font-size:0.84rem;'
        f'padding:0.14rem 0;border-bottom:1px solid rgba(255,255,255,0.05)">'
        f'<span style="color:#9e9e9e">{label}</span>'
        f'<span style="font-weight:{weight};color:{color};text-align:right">'
        f'{value}{note_html}</span></div>',
        unsafe_allow_html=True,
    )


def _ptc_section(st, title: str) -> None:
    st.markdown(
        f'<div style="color:#9e9e9e;font-size:0.72rem;letter-spacing:0.08em;'
        f'text-transform:uppercase;margin:0.85rem 0 0.35rem">{title}</div>',
        unsafe_allow_html=True,
    )


def render_pre_trade_page(
    *,
    default_ticker: str = "",
    default_spot: float | None = None,
) -> None:
    """Single-screen calculator. Recalculates on every input change."""
    import pandas as pd
    import streamlit as st

    prefs = load_prefs()
    cand_ref = read_candidate_query(st)
    cand_payload = None
    cand_missing = False
    parsed = parse_candidate_ref(cand_ref) if cand_ref else None
    if parsed:
        cand_payload = load_scan_contract(*parsed)
        if cand_payload is None:
            cand_missing = True
        elif st.session_state.get("ptc_applied_candidate") != cand_ref:
            apply_candidate_to_session(st, cand_payload)
            st.session_state["ptc_applied_candidate"] = cand_ref

    if "ptc_symbol" not in st.session_state:
        st.session_state["ptc_symbol"] = str(default_ticker or "").upper()
    if "ptc_account" not in st.session_state:
        saved_acct = _f(prefs.get("account_size"))
        st.session_state["ptc_account"] = float(saved_acct) if saved_acct else 0.0
    # Chart field. Never seed from scanner/spot when a candidate is loaded.
    if (
        "ptc_underlying" not in st.session_state
        and default_spot
        and cand_payload is None
    ):
        st.session_state["ptc_underlying"] = float(default_spot)

    st.markdown("### Pre-Trade Check")
    st.caption(
        "Fill this in **before** entering. Chart levels → option outcomes → "
        "TAKE or SKIP. No override. Save a snapshot to compare with the actual later."
    )

    meta = st.session_state.get("ptc_scan_meta") or {}
    if cand_payload or (cand_ref and meta):
        src = cand_payload or {}
        banner = format_prefill_banner(
            src.get("scan_ts") or meta.get("scan_ts"),
            src.get("rank") if src.get("rank") is not None else meta.get("scanner_rank"),
            src.get("score") if src.get("score") is not None else meta.get("scanner_score"),
        )
        st.info(
            f"{banner}  \n"
            "Contract fields prefilled. Chart fields are yours to enter."
        )
        warn = staleness_warning(src.get("scan_ts") or meta.get("scan_ts"))
        if warn:
            st.warning(warn)
    elif cand_missing:
        st.warning(
            "Candidate not found. The scan snapshot may have expired — "
            "enter the contract by hand."
        )

    c_in, c_der, c_ver = st.columns(3)

    with c_in:
        st.markdown("#### INPUTS")
        _ptc_section(st, "Chart")
        _ms = _marked_kwargs(st, "symbol", "Symbol")
        symbol = st.text_input("Symbol", key="ptc_symbol", **_ms).strip().upper()
        _md = _marked_kwargs(st, "direction", "Direction")
        direction = st.selectbox(
            "Direction", ["CALL", "PUT"], key="ptc_direction", **_md,
        )
        underlying = st.number_input(
            "Underlying now", min_value=0.0, step=0.01, format="%.2f",
            key="ptc_underlying",
        )
        target_distance = st.number_input(
            "Target distance ($)", step=0.01, format="%.2f", key="ptc_target",
            help="Dollar distance from entry, always positive",
        )
        t_dist, t_err = _positive_distance(target_distance)
        if t_err:
            st.markdown(
                f'<div style="color:#ff8a80;font-size:0.8rem;margin:-0.35rem 0 0.4rem">'
                f"{t_err}</div>",
                unsafe_allow_html=True,
            )
        else:
            impl_t, _ = implied_level_prices(
                direction, _finite(underlying), t_dist, None,
            )
            if impl_t is not None:
                st.caption(f"→ target price {impl_t:.2f}")
        invalidation_distance = st.number_input(
            "Invalidation distance ($)", step=0.01, format="%.2f",
            key="ptc_invalidation",
            help="Dollar distance from entry, always positive",
        )
        i_dist, i_err = _positive_distance(invalidation_distance)
        if i_err:
            st.markdown(
                f'<div style="color:#ff8a80;font-size:0.8rem;margin:-0.35rem 0 0.4rem">'
                f"{i_err}</div>",
                unsafe_allow_html=True,
            )
        else:
            _, impl_s = implied_level_prices(
                direction, _finite(underlying), None, i_dist,
            )
            if impl_s is not None:
                st.caption(f"→ stop price {impl_s:.2f}")
        hold_hours = st.number_input(
            "Expected hold (hours)", min_value=0.0, step=0.5, format="%.2f",
            key="ptc_hold", help="e.g. 0.5, 1, 2",
        )
        entry_window = st.selectbox(
            "Entry window", list(ENTRY_WINDOWS), key="ptc_window",
        )

        _ptc_section(st, "Contract")
        _mk = _marked_kwargs(st, "strike", "Strike")
        strike = st.number_input(
            "Strike", min_value=0.0, step=0.5, format="%.2f",
            key="ptc_strike", **_mk,
        )
        _mk = _marked_kwargs(st, "dte", "DTE")
        dte = st.number_input("DTE", min_value=0, step=1, key="ptc_dte", **_mk)
        _mk = _marked_kwargs(st, "bid", "Bid")
        bid = st.number_input(
            "Bid", min_value=0.0, step=0.01, format="%.2f", key="ptc_bid", **_mk,
        )
        _mk = _marked_kwargs(st, "ask", "Ask")
        ask = st.number_input(
            "Ask", min_value=0.0, step=0.01, format="%.2f", key="ptc_ask", **_mk,
        )
        _mk = _marked_kwargs(st, "delta", "Delta")
        delta = st.number_input(
            "Delta", step=0.01, format="%.3f", key="ptc_delta",
            help="Signed or unsigned — absolute value is used",
            **_mk,
        )
        th1, th2 = st.columns([2, 1])
        with th1:
            _mk = _marked_kwargs(st, "theta", "Theta")
            theta = st.number_input(
                "Theta", step=0.01, format="%.4f", key="ptc_theta",
                help="Pasted negative is fine — absolute value is used",
                **_mk,
            )
        with th2:
            _mk = _marked_kwargs(st, "theta_units", "Theta units")
            theta_units = st.selectbox(
                "Theta units", list(THETA_UNITS),
                key="ptc_theta_units", **_mk,
            )
        _mk = _marked_kwargs(st, "open_interest", "Open interest")
        oi = st.number_input(
            "Open interest", min_value=0.0, step=1.0, format="%.0f",
            key="ptc_oi", **_mk,
        )
        account = st.number_input(
            "Account size", min_value=0.0, step=100.0, format="%.0f",
            key="ptc_account",
            help="Persists between sessions",
        )
        save_prefs(account_size=account)

    inp = PreTradeInputs(
        symbol=symbol,
        direction=direction,
        underlying=underlying,
        target_distance=target_distance,
        invalidation_distance=invalidation_distance,
        hold_hours=hold_hours,
        entry_window=entry_window,
        strike=strike,
        dte=int(dte),
        bid=bid,
        ask=ask,
        delta=delta,
        theta=theta,
        theta_units=theta_units,
        open_interest=oi,
        account_size=account,
    )
    result = compute_pre_trade(inp)
    d = result["derived"]

    with c_der:
        st.markdown("#### DERIVED")
        _ptc_section(st, "Contract anatomy")
        _ptc_line(st, "mid = (bid + ask) / 2", format_money(d["mid"]))
        _ptc_line(st, "spread = ask − bid", format_money(d["spread"]))
        _ptc_line(st, "spread_pct = spread / mid", format_pct(d["spread_pct"]))
        _ptc_line(st, "intrinsic", format_money(d["intrinsic"]))
        _ptc_line(st, "extrinsic = mid − intrinsic", format_money(d["extrinsic"]))
        st.markdown(
            f'<div style="margin:0.4rem 0 0.15rem;font-size:0.72rem;color:#9e9e9e;'
            f'letter-spacing:0.06em;text-transform:uppercase">extrinsic_pct</div>'
            f'<div style="font-size:1.7rem;font-weight:800;color:#eee;line-height:1.1">'
            f'{format_pct(d["extrinsic_pct"])}</div>',
            unsafe_allow_html=True,
        )
        if d.get("fully_extrinsic"):
            st.markdown(
                '<div style="color:#ff6d00;font-size:0.85rem;margin-bottom:0.4rem">'
                "fully extrinsic — scheduled to zero at expiry</div>",
                unsafe_allow_html=True,
            )

        _ptc_section(st, "Theta normalisation")
        _ptc_line(
            st, "theta_hr",
            format_num(d["theta_hr"], 4),
            note="|θ|/6.5 if per day",
        )

        _ptc_section(st, "Step 1 — moves from the chart")
        _ptc_line(st, "move_to_target", format_num(d["move_to_target"]))
        _ptc_line(st, "move_to_stop", format_num(d["move_to_stop"]))

        _ptc_section(st, "Step 2 — option terms")
        _ptc_line(
            st, "θ_hr × hold",
            format_money(d["theta_hold"]),
            note="time decay (win path)",
        )
        _ptc_line(st, "+ spread", format_money(d["spread"]))
        _ptc_line(
            st, "holding_cost_win", format_money(d["holding_cost_win"]),
            strong=True,
        )
        _ptc_line(st, "move_to_target × |δ|", format_money(d["delta_gain"]))
        _ptc_line(st, "− holding_cost_win", format_money(d["holding_cost_win"]))
        _ptc_line(
            st, "gain_per_contract", format_money(d["gain_per_contract"], signed=True),
            strong=True,
        )

        _ptc_line(
            st, "θ_hr × hold × 0.6",
            format_money(d["theta_hold_loss"]),
            note="early-stop assumption",
        )
        _ptc_line(st, "+ spread", format_money(d["spread"]))
        _ptc_line(st, "+ slippage", format_money(d["slippage"]), note="fixed 0.02")
        _ptc_line(
            st, "holding_cost_loss", format_money(d["holding_cost_loss"]),
            strong=True,
        )
        _ptc_line(st, "move_to_stop × |δ|", format_money(d["delta_loss"]))
        _ptc_line(st, "+ holding_cost_loss", format_money(d["holding_cost_loss"]))
        _ptc_line(
            st, "loss_per_contract", format_loss(d["loss_per_contract"]),
            strong=True, color="#d50000",
        )
        if d.get("loss_clamped"):
            st.markdown(
                f'<div style="color:#ff6d00;font-size:0.85rem;margin:0.25rem 0 0.4rem">'
                f'{d.get("loss_clamp_note") or LOSS_CLAMP_NOTE}</div>',
                unsafe_allow_html=True,
            )
        _ptc_line(st, "value_at_target", format_money(d["value_at_target"]))
        _ptc_line(st, "value_at_stop", format_money(d["value_at_stop"]))

        _ptc_section(st, "Step 3 — the ratio")
        _ptc_line(st, "gain_pct", format_pct(d["gain_pct"]))
        _ptc_line(st, "loss_pct", format_pct(d["loss_pct"]))
        color = ratio_color(d["ratio"])
        st.markdown(
            f'<div style="margin:0.45rem 0 0.2rem;font-size:2.35rem;font-weight:900;'
            f'color:{color};line-height:1">{format_ratio(d["ratio"])}</div>',
            unsafe_allow_html=True,
        )

        _ptc_section(st, "Step 4 — supporting numbers")
        _ptc_line(
            st, "breakeven_move",
            format_num(d["breakeven_move"]),
            note="underlying must move this far just to break even",
        )
        _ptc_line(st, "breakeven_win_rate", format_pct(d["breakeven_win_rate"]))
        _ptc_line(
            st, "time_stop_minutes",
            "—" if d["time_stop_minutes"] is None else f"{d['time_stop_minutes']} min",
        )

        _ptc_section(st, "Position sizing")
        _ptc_line(
            st, "risk_per_contract", format_loss(d["risk_per_contract"]),
            color="#d50000",
        )
        _ptc_line(st, "max_risk (1%)", format_money(d["max_risk"]))
        _ptc_line(
            st, "contracts",
            "—" if d["contracts"] is None else str(int(d["contracts"])),
        )
        _ptc_line(st, "capital_deployed", format_money(d["capital_deployed"]))
        _ptc_line(st, "pct_of_account", format_pct(d["pct_of_account"]))
        if d.get("large_capital"):
            st.markdown(
                '<div style="color:#ff6d00;font-size:0.85rem;margin-top:0.35rem">'
                "large capital outlay to risk 1%</div>",
                unsafe_allow_html=True,
            )

    with c_ver:
        st.markdown("#### VERDICT")
        verdict = result["verdict"]
        vcolor = "#00c853" if verdict == "TAKE" else "#d50000"
        skip_reason = result.get("skip_reason")
        extra_reason = (
            f'<div style="color:#ff8a80;font-size:0.85rem;margin-top:0.25rem">'
            f"{skip_reason}</div>"
            if verdict == "SKIP" and skip_reason else ""
        )
        st.markdown(
            f'<div style="background:rgba(255,255,255,0.03);border:1px solid {vcolor};'
            f'border-radius:10px;padding:0.7rem 0.9rem;margin-bottom:0.7rem">'
            f'<div style="font-size:2rem;font-weight:900;color:{vcolor};letter-spacing:0.04em">'
            f'{verdict}</div>{extra_reason}</div>',
            unsafe_allow_html=True,
        )

        for g in result["gates"]:
            passed = g["passed"]
            badge = g.get("badge") or (
                "PASS" if passed is True else ("FAIL" if passed is False else "—")
            )
            bcolor = (
                "#00c853" if passed is True
                else ("#d50000" if passed is False else "#9e9e9e")
            )
            fail = g.get("fail_msg") or ""
            extra = (
                f'<div style="color:#ff8a80;font-size:0.75rem;margin-top:0.15rem">{fail}</div>'
                if (passed is not True and fail) else ""
            )
            st.markdown(
                f'<div style="display:flex;justify-content:space-between;gap:0.5rem;'
                f'padding:0.35rem 0;border-bottom:1px solid rgba(255,255,255,0.06)">'
                f'<div><div style="font-weight:600">{g["name"]}</div>'
                f'<div style="color:#9e9e9e;font-size:0.8rem">{g["value"]}</div>'
                f'{extra}</div>'
                f'<div style="font-weight:800;color:{bcolor}">{badge}</div></div>',
                unsafe_allow_html=True,
            )

        if verdict == "SKIP" and result["failed"]:
            st.markdown("**Failed gates**")
            for name in result["failed"]:
                st.markdown(f"- {name}")

        if verdict == "TAKE" and result.get("plan"):
            _ptc_section(st, "Plan summary — copy into the log")
            st.code(result["plan"])

        if st.button("Save this check", type="primary", use_container_width=True, key="ptc_save"):
            meta = st.session_state.get("ptc_scan_meta") or {}
            overrides = prefill_overrides(
                st.session_state.get("ptc_prefill_original") or {},
                current_prefill_values(
                    symbol=symbol,
                    direction=direction,
                    strike=strike,
                    dte=dte,
                    bid=bid,
                    ask=ask,
                    delta=delta,
                    theta=theta,
                    theta_units=theta_units,
                    open_interest=oi,
                ),
            )
            row = save_check(
                result,
                scan_id=meta.get("scan_id"),
                contract_id=meta.get("contract_id"),
                scanner_rank=meta.get("scanner_rank"),
                scanner_score=meta.get("scanner_score"),
                prefill_overrides=overrides,
            )
            st.success(
                f"Saved {row['verdict']} {symbol or '—'} at {row['ts_et'][:16].replace('T', ' ')} ET"
            )

    st.markdown("---")
    st.markdown("#### Check history")
    st.caption(
        "Mark **Taken** after the fact. Fill exit price and reason to see whether "
        "the projection was close — including when you skipped."
    )
    saved = load_checks()
    if not saved:
        st.info("No saved checks yet.")
        return

    hist_rows = []
    for row in saved:
        inp_r = row.get("inputs") or {}
        der = row.get("derived") or {}
        ts = str(row.get("ts_et") or "")
        day = ts[:10]
        exit_px = row.get("actual_exit_price")
        reason = row.get("exit_reason") or ""
        if row.get("taken") and exit_px is not None:
            actual = f"{format_money(_f(exit_px))}" + (f" · {reason}" if reason else "")
        elif row.get("taken"):
            actual = "taken · no exit yet"
        else:
            actual = "skipped"
        hist_rows.append({
            "check_id": row.get("check_id"),
            "Date": day,
            "Symbol": inp_r.get("symbol") or "",
            "Verdict": row.get("verdict") or "",
            "Ratio": format_ratio(der.get("ratio")),
            "Taken": bool(row.get("taken")),
            "Exit $": float(exit_px) if exit_px is not None else None,
            "Reason": reason,
            "Actual": actual,
        })
    hdf = pd.DataFrame(hist_rows)
    display = hdf.drop(columns=["check_id"])
    edited = st.data_editor(
        display,
        column_config={
            "Date": st.column_config.TextColumn(disabled=True),
            "Symbol": st.column_config.TextColumn(disabled=True),
            "Verdict": st.column_config.TextColumn(disabled=True),
            "Ratio": st.column_config.TextColumn(disabled=True),
            "Taken": st.column_config.CheckboxColumn(),
            "Exit $": st.column_config.NumberColumn(format="%.2f", min_value=0.0),
            "Reason": st.column_config.SelectboxColumn(
                options=[""] + list(EXIT_REASONS),
            ),
            "Actual": st.column_config.TextColumn(disabled=True),
        },
        disabled=["Date", "Symbol", "Verdict", "Ratio", "Actual"],
        hide_index=True,
        use_container_width=True,
        key="ptc_history_editor",
        height=min(360, 48 + 36 * max(len(display), 1)),
    )

    def _norm_exit(x):
        v = _f(x)
        return None if v is None else v

    changed = False
    for orig, new in zip(hist_rows, edited.to_dict("records")):
        new_taken = bool(new.get("Taken"))
        new_exit = _norm_exit(new.get("Exit $"))
        orig_exit = _norm_exit(orig.get("Exit $"))
        new_reason = new.get("Reason") or ""
        orig_reason = orig.get("Reason") or ""
        exit_changed = (
            (orig_exit is None) != (new_exit is None)
            or (
                orig_exit is not None
                and new_exit is not None
                and abs(orig_exit - new_exit) > 1e-9
            )
        )
        if (
            new_taken != orig["Taken"]
            or exit_changed
            or new_reason != orig_reason
        ):
            update_check(
                orig["check_id"],
                taken=new_taken,
                actual_exit_price=new_exit,
                exit_reason=new_reason or None,
            )
            changed = True
    if changed:
        st.rerun()
