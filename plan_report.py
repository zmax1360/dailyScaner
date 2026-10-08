"""plan_report — the Game Plan and gamma read for one scan, as message lines.

Shared by the scheduler's automatic Telegram message, the interactive bot and the app's
manual push, so all three say the same thing. No Streamlit and no network: it reads the
scan archive it is given and the chain snapshot the scanner already recorded.
Display only; nothing here feeds scoring.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd

import ema_stack
import game_plan
import gex
import volume_history as vh

ET = ZoneInfo("America/New_York")
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                             "ui_settings.json")


def gamma_summary_for(ticker: str, spot: Any, today=None) -> dict | None:
    """``gex.summary`` for the nearest live expiry of ``ticker``; None if unavailable.
    ``today`` (a date) defaults to the current date in New York."""
    try:
        spot_f = float(spot)
    except (TypeError, ValueError):
        return None
    latest = vh.latest_scan(str(ticker or "").upper())
    if latest.empty or spot_f <= 0:
        return None
    as_of = datetime.fromisoformat(str(latest["ts_et"].iloc[0]))
    return gex.summary(latest, spot=spot_f, as_of=as_of,
                       today=today or datetime.now(ET).date())


def gamma_lines(summary: dict | None) -> list[str]:
    """Telegram (HTML) lines for the gamma read. ``summary`` comes from ``gex.summary``."""
    if not summary:
        return ["🧱 <b>GAMMA</b> — no chain snapshot with usable open interest yet"]
    d = datetime.strptime(summary["expiry"], "%Y-%m-%d")
    net = float(summary["net"])
    regime = ("positive: dealer hedging tends to dampen moves" if net >= 0
              else "negative: dealer hedging tends to amplify moves")
    lines = [
        f"🧱 <b>GAMMA · {d:%b} {d.day}</b>",
        f"Net <b>{'+' if net >= 0 else ''}{gex.fmt_money(net)}</b> — {regime}",
    ]
    walls = []
    if summary.get("call_wall") is not None:
        walls.append(f"Call wall <b>${summary['call_wall']:g}</b> "
                     f"({gex.fmt_money(summary['call_wall_gex'])})")
    if summary.get("put_wall") is not None:
        walls.append(f"Put wall <b>${summary['put_wall']:g}</b> "
                     f"({gex.fmt_money(summary['put_wall_gex'])})")
    if walls:
        lines.append(" · ".join(walls))
    levels = []
    if summary.get("support") is not None:
        levels.append(f"Support <b>${summary['support']:g}</b> "
                      f"({gex.fmt_money(summary['support_gex'])})")
    if summary.get("resistance") is not None:
        levels.append(f"Resistance <b>${summary['resistance']:g}</b> "
                      f"({gex.fmt_money(summary['resistance_gex'])})")
    if levels:
        lines.append(" · ".join(levels))
    if summary.get("top"):
        lines.append("Largest: " + " · ".join(
            f"${k:g} {gex.fmt_money(v)}" for k, v in summary["top"]))
    if summary.get("first_negative_below_spot") is not None:
        lines.append(f"First negative strike below spot: "
                     f"<b>${summary['first_negative_below_spot']:g}</b>")
    as_of = summary["as_of"].astimezone(ET)
    lines.append(f"<i>dollars of hedging per $1 move · snapshot {as_of:%a %b %d %H:%M ET}</i>")
    return lines


def saved_plan_settings(path: str | None = None) -> dict[str, float]:
    """Stop buffer and minimum reward to risk as saved in the app's Settings, falling back
    to the Game Plan defaults when the file is missing, unreadable or out of range."""
    out = {"stop_buffer_pct": game_plan.STOP_BUFFER_PCT,
           "min_reward_to_risk": game_plan.MIN_REWARD_TO_RISK}
    try:
        with open(path or SETTINGS_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return out
    if not isinstance(raw, dict):
        return out
    for key, (lo, hi) in (("stop_buffer_pct", (0.0, 2.0)), ("min_reward_to_risk", (0.5, 5.0))):
        v = raw.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi:
            out[key] = float(v)
    return out


def picks_from_archive(payload: dict) -> pd.DataFrame:
    """The ranked rows the scan stored. Never re-scored here."""
    rows = ((payload or {}).get("best_value") or {}).get("rows") or []
    return pd.DataFrame(rows)


def plan_from_archive(payload: dict, ticker: str, *, gamma: dict | None = None,
                      now: datetime | None = None,
                      settings: dict[str, float] | None = None) -> game_plan.Plan:
    """The Game Plan for one scan archive. VWAP is not stored in the archive, so it is
    left out; candidates come from the ranking the scan itself saved."""
    from strategy_engine import ticker_expected_range

    spot = float(payload.get("spot") or 0)
    session = payload.get("session") or {}
    s = settings or saved_plan_settings()
    return game_plan.build_plan(
        ticker=ticker, spot=spot,
        trend=ema_stack.banner_for_archive(payload, now=now or datetime.now(ET)),
        gamma=gamma, vwap=None,
        expected=ticker_expected_range(spot, payload.get("volume") or {}),
        picks=picks_from_archive(payload),
        session_high=session.get("day_high"), session_low=session.get("day_low"),
        stop_buffer_pct=s["stop_buffer_pct"], min_reward_to_risk=s["min_reward_to_risk"],
        emas=((payload.get("timeframes") or {}).get(ema_stack.TIMEFRAME) or {}),
        price_note="Price is the one recorded by the scan.",
    )


def report_lines(payload: dict, ticker: str, *, plan: bool = True, gamma: bool = True,
                 now: datetime | None = None,
                 compact: bool = False) -> tuple[list[str], list[str]]:
    """(game plan lines, gamma lines) for a scan message. Either list is empty when it was
    not asked for or could not be built; the reason is not hidden: the caller logs it."""
    summary = (gamma_summary_for(ticker, payload.get("spot"), today=now.date() if now else None)
               if (plan or gamma) else None)
    plan_out = (game_plan.plan_lines(plan_from_archive(payload, ticker, gamma=summary, now=now),
                                     compact=compact)
                if plan else [])
    return plan_out, (gamma_lines(summary) if gamma else [])
