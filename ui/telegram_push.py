"""Manual Telegram push from the Settings page: message formatting, config and send."""

from __future__ import annotations

from datetime import date, datetime
import os
import urllib.request

from ui.market import _build_best_value_df
from ui.services import _SCANNER_DIR
import pandas as pd
import ema_stack
import game_plan
from ui.common import ET
from ui.gamma_data import gamma_summary_for  # noqa: F401  (re-exported for the Settings page)

_ENV_FILE    = os.path.join(_SCANNER_DIR, ".env")


def _load_telegram_config() -> tuple[str | None, str | None]:
    """
    Read TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID from the .env file.
    Returns (token, chat_id) — either may be None if not configured.
    """
    token: str | None = None
    chat_id: str | None = None
    try:
        with open(_ENV_FILE) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                if key.strip() == "TELEGRAM_BOT_TOKEN":
                    token = val or None
                elif key.strip() == "TELEGRAM_CHAT_ID":
                    chat_id = val or None
    except FileNotFoundError:
        pass
    return token, chat_id


def _send_telegram(token: str, chat_id: str, text: str) -> tuple[bool, str]:
    """
    POST a message to Telegram via the Bot API.
    Uses only stdlib urllib — no extra dependencies.
    Returns (success, error_message).
    """
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "HTML",
    }).encode()
    try:
        req = urllib.request.Request(url, data=body, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200, ""
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        return False, f"HTTP {e.code}: {detail}"
    except Exception as exc:
        return False, str(exc)


def _tg_pc_bias(pc: float) -> str:
    if pc >= 1.5:  return "▼ BEARISH"
    if pc >= 1.1:  return "▼ MILD BEARISH"
    if pc >= 0.9:  return "─ NEUTRAL"
    if pc >= 0.7:  return "▲ MILD BULLISH"
    return "▲ BULLISH"


def _format_scan_message(
    payload: dict,
    prev_payload: dict | None,
    ticker: str,
    top_n: int,
    include: dict,
    expiry_drill: list[str] | None = None,
    gamma: dict | None = None,
    plan=None,
) -> str:
    """
    Build a Telegram HTML message from an archive payload.

    include keys:
      session, mtf, magnets, volume_expiry, orb, deltas

    expiry_drill: list of expiry strings (YYYY-MM-DD) to show drill-down details for.
    """
    L: list[str] = []
    vol       = payload.get("volume") or {}
    tfs       = payload.get("timeframes") or {}
    mags      = payload.get("signal_magnets") or {}
    session   = payload.get("session") or {}
    or_data   = payload.get("or_data") or {}
    spot      = float(payload.get("spot") or 0)
    direction = payload.get("direction", "—")
    pc_ratio  = float(vol.get("pc_ratio") or 0)
    all_calls = vol.get("top_calls") or []
    all_puts  = vol.get("top_puts")  or []

    try:
        ts_et = datetime.fromisoformat(payload.get("timestamp", "")).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
    except Exception:
        ts_et = "—"

    dir_icon = "▲" if "BULL" in direction else ("▼" if "BEAR" in direction else "─")

    # ── Header ────────────────────────────────────────────────────────────────
    L.append(f"<b>📊 {ticker} Options Scanner</b>")
    L.append(f"<i>{ts_et}</i>")
    L.append("")

    # ── Session summary ───────────────────────────────────────────────────────
    # ── Game plan (first, because it is the summary) ─────────────────────────
    if include.get("game_plan") and plan is not None:
        L.extend(game_plan.plan_lines(plan))
        L.append("")

    if include.get("session", True):
        prev_close = session.get("prev_close")
        open_p     = session.get("open")
        cv = int(vol.get("total_call_vol") or 0)
        pv = int(vol.get("total_put_vol")  or 0)
        pc_bias = _tg_pc_bias(pc_ratio)

        if prev_close:
            chg  = spot - prev_close
            sign = "+" if chg >= 0 else ""
            pct  = chg / prev_close * 100
            delta_str = f"{sign}${chg:.2f} ({sign}{pct:.2f}%)"
        else:
            delta_str = ""

        parts = [f"<b>${spot:.2f}</b>"]
        if delta_str:
            parts.append(delta_str)
        if open_p:
            parts.append(f"Open ${open_p:.2f}")
        if prev_close:
            parts.append(f"Prev close ${prev_close:.2f}")
        L.append(f"💰 <b>{ticker}</b> · " + " · ".join(parts))
        L.append(f"📈 Direction: <b>{dir_icon} {direction}</b>")
        L.append(f"⚖️ P/C <b>{pc_ratio:.2f}</b> {pc_bias} · Calls {cv:,} · Puts {pv:,}")
        L.append("")

    # ── Multi-Timeframe Detail ────────────────────────────────────────────────
    if include.get("mtf", True) and tfs:
        L.append("📊 <b>MULTI-TIMEFRAME</b>")
        rows = ["<pre>TF    RSI    MACD       Vol×"]
        tf_order = ["5M", "10M", "15M", "45M", "1H", "4H", "1D"]
        for tf in tf_order:
            d = tfs.get(tf)
            if not d:
                continue
            rsi  = float(d.get("rsi") or 0)
            hist = float(d.get("hist") or 0)
            vs   = float(d.get("vs") or 0)
            macd_s = f"{hist:+.2f}"
            rows.append(f"{tf:<5} {rsi:>5.1f}  {macd_s:>8}   {vs:.1f}x")
        rows.append("</pre>")
        L.extend(rows)

    # ── The Magnets ───────────────────────────────────────────────────────────
    if include.get("magnets", True):
        for label, emoji, contracts in [
            (f"TOP {top_n} CALLS", "🟢", all_calls[:top_n]),
            (f"TOP {top_n} PUTS",  "🔴", all_puts[:top_n]),
        ]:
            L.append(f"{emoji} <b>{label}</b>")
            rows = ["<pre>Strike  Expiry   Price    Vol      VOI"]
            for c in contracts:
                strike = float(c.get("strike") or 0)
                price  = float(c.get("lastPrice") or 0)
                exp    = c.get("expiry", "")
                exp_s  = exp[5:] if len(exp) >= 7 else exp  # MM-DD
                v      = int(c.get("volume") or 0)
                oi     = max(int(c.get("openInterest") or 0), 1)
                voi    = v / oi
                flag   = "🔥" if voi >= 5 else ("★" if voi >= 2 else " ")
                rows.append(
                    f"${strike:<6.1f} {exp_s:<8} ${price:<6.2f} {v:>7,}  {voi:>5.1f}x{flag}"
                )
            rows.append("</pre>")
            L.extend(rows)

    # ── Volume by Expiry ──────────────────────────────────────────────────────
    if include.get("volume_expiry", True):
        exp_agg: dict[str, dict] = {}
        for c in all_calls:
            exp = c.get("expiry", "?")
            d = exp_agg.setdefault(exp, {
                "cv": 0, "pv": 0, "dte": int(c.get("dte", 0)),
                "call_px": None, "call_top_vol": 0,
                "put_px": None, "put_top_vol": 0,
            })
            v = int(c.get("volume") or 0)
            d["cv"] += v
            if v > d["call_top_vol"]:
                d["call_top_vol"] = v
                d["call_px"] = float(c.get("lastPrice") or 0)
        for c in all_puts:
            exp = c.get("expiry", "?")
            d = exp_agg.setdefault(exp, {
                "cv": 0, "pv": 0, "dte": int(c.get("dte", 0)),
                "call_px": None, "call_top_vol": 0,
                "put_px": None, "put_top_vol": 0,
            })
            v = int(c.get("volume") or 0)
            d["pv"] += v
            if v > d["put_top_vol"]:
                d["put_top_vol"] = v
                d["put_px"] = float(c.get("lastPrice") or 0)

        if exp_agg:
            L.append("📅 <b>VOLUME BY EXPIRY</b>")
            rows = ["<pre>Expiry  DTE CallVol PutVol  P/C  C$    P$"]
            for exp in sorted(exp_agg):
                d   = exp_agg[exp]
                cv_ = d["cv"]; pv_ = d["pv"]; dte = d["dte"]
                if cv_ == 0 and pv_ == 0:
                    continue
                pc_ = pv_ / cv_ if cv_ else 0
                bias_s = _tg_pc_bias(pc_) if cv_ and pv_ else "n/a"
                exp_s  = exp[5:] if len(exp) >= 7 else exp
                cpx = f"${d['call_px']:.2f}" if d["call_px"] is not None else "—"
                ppx = f"${d['put_px']:.2f}"  if d["put_px"]  is not None else "—"
                rows.append(
                    f"{exp_s:<7} {dte:>3}d {cv_:>7,} {pv_:>6,} "
                    f"{pc_:.2f}{bias_s[:2]} {cpx:<6} {ppx}"
                )
            rows.append("</pre>")
            L.extend(rows)

    # ── Opening Range Breakout ────────────────────────────────────────────────
    if include.get("orb", True) and or_data:
        L.append("📍 <b>OPENING RANGE BREAKOUT</b>")
        for tf, d in or_data.items():
            if not isinstance(d, dict):
                continue
            bias   = d.get("bias", "—")
            hi     = d.get("high", 0)
            lo     = d.get("low", 0)
            rng    = d.get("range", 0)
            rng_p  = d.get("range_pct", 0)
            L.append(f"<b>{tf} OR:</b> H ${hi:.2f} · L ${lo:.2f} · Range ${rng:.2f} ({rng_p:.1f}%) → <b>{bias}</b>")

    # ── Deltas vs previous run ────────────────────────────────────────────────
    if include.get("deltas", True) and prev_payload:
        prev_vol  = prev_payload.get("volume") or {}
        prev_spot = float(prev_payload.get("spot") or 0)
        prev_pc   = float(prev_vol.get("pc_ratio") or 0)
        prev_dir  = prev_payload.get("direction", "")
        prev_mags = prev_payload.get("signal_magnets") or {}

        try:
            prev_ts = datetime.fromisoformat(prev_payload.get("timestamp","")).astimezone(ET).strftime("%Y-%m-%d %H:%M ET")
        except Exception:
            prev_ts = "prev run"

        delta_lines = []
        # Spot
        if prev_spot:
            sd = spot - prev_spot
            arrow = "↑" if sd > 0 else "↓"
            delta_lines.append(f"{arrow} Spot ${prev_spot:.2f} → ${spot:.2f} ({sd:+.2f})")
        # P/C
        pcd = pc_ratio - prev_pc
        arrow = "↑" if pcd > 0 else "↓"
        delta_lines.append(f"{arrow} P/C {prev_pc:.2f} → {pc_ratio:.2f} ({pcd:+.3f})")
        # Direction
        if prev_dir and prev_dir != direction:
            delta_lines.append(f"🔔 Direction: {prev_dir} → {direction}")
        # Magnet shifts
        for side in ("call", "put"):
            cm = mags.get(side) or {}
            pm = prev_mags.get(side) or {}
            if cm and pm and cm.get("strike") != pm.get("strike"):
                label = "CALL MAGNET" if side == "call" else "PUT MAGNET"
                delta_lines.append(f"🔄 {label}: ${pm.get('strike')} → ${cm.get('strike')} ← STRIKE CHANGE")

        if delta_lines:
            L.append(f"🔁 <b>CHANGES vs {prev_ts}</b>")
            L.extend(delta_lines)
            L.append("")

    # ── Best Value Option ─────────────────────────────────────────────────────
    if include.get("best_value"):
        prev_vol_bv = (prev_payload.get("volume") or {}) if prev_payload else None
        bv_df = _build_best_value_df(vol, spot, prev_vol_bv, min_volume=500)
        if not bv_df.empty and bv_df["Status"].astype(str).str.contains("BEST VALUE", na=False).any():
            from scoring_pool import POOL_0DTE, POOL_1DTE
            L.append("⭐ <b>BEST VALUE</b> <i>(per DTE pool)</i>")
            for pool_name in (POOL_1DTE, POOL_0DTE):
                best_p = bv_df[
                    bv_df["Status"].astype(str).str.contains("BEST VALUE", na=False)
                    & (bv_df.get("pool") == pool_name)
                ]
                if best_p.empty:
                    L.append(f"⭐ <b>{pool_name}</b> — not ranked")
                    continue
                best = best_p.iloc[0]
                voi_b = best["volume"] / max(int(best["openInterest"]), 1)
                dte_s = (
                    f"{int(best['dte'])}d"
                    if "dte" in best.index and pd.notna(best.get("dte"))
                    else "?"
                )
                L.append(
                    f"⭐ <b>{pool_name}</b>  "
                    f"{best['side']} ${best['strike']:.1f}  "
                    f"{best['expiry']} ({dte_s})  "
                    f"score {best['Value_Score']:.2f}  "
                    f"${best['last']:.2f}  vol/OI {voi_b:.1f}x"
                )
            L.append("")

    # ── Gamma exposure ────────────────────────────────────────────────────────
    if include.get("gamma"):
        L.extend(format_gamma_lines(gamma))
        L.append("")

    # ── Expiry drill-down ─────────────────────────────────────────────────────
    if expiry_drill:
        for exp in expiry_drill:
            exp_calls = [c for c in all_calls if c.get("expiry") == exp]
            exp_puts  = [c for c in all_puts  if c.get("expiry") == exp]
            if not exp_calls and not exp_puts:
                continue
            dte = (exp_calls or exp_puts)[0].get("dte", "?")
            pc_calls_vol = sum(int(c.get("volume") or 0) for c in exp_calls)
            pc_puts_vol  = sum(int(c.get("volume") or 0) for c in exp_puts)
            exp_pc = pc_puts_vol / pc_calls_vol if pc_calls_vol else 0
            L.append(f"🔍 <b>EXPIRY DRILL-DOWN: {exp} ({dte}d) · P/C {exp_pc:.2f}</b>")
            rows = ["<pre>Side   Strike   Vol      VOI"]
            for side_label, contracts in [("CALL", exp_calls), ("PUT", exp_puts)]:
                for c in contracts[:5]:
                    strike = float(c.get("strike") or 0)
                    v      = int(c.get("volume") or 0)
                    oi     = max(int(c.get("openInterest") or 0), 1)
                    voi    = v / oi
                    flag   = "🔥" if voi >= 5 else ("★" if voi >= 2 else " ")
                    rows.append(f"{side_label:<6} ${strike:<7.1f} {v:>7,}  {voi:.1f}x{flag}")
            rows.append("</pre>")
            L.extend(rows)

    return "\n".join(L)


def format_gamma_lines(summary: dict | None) -> list[str]:
    """Telegram (HTML) lines for the gamma read. ``summary`` comes from ``gex.summary``."""
    import gex

    if not summary:
        return ["🧱 <b>GAMMA</b> — no chain snapshot with usable open interest yet"]
    d = date.fromisoformat(summary["expiry"])
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


def plan_for_message(payload: dict, prev_payload: dict | None, ticker: str,
                     gamma: dict | None = None,
                     stop_buffer_pct: float = game_plan.STOP_BUFFER_PCT,
                     min_reward_to_risk: float = game_plan.MIN_REWARD_TO_RISK):
    """Game plan from one archive, for the Telegram push. VWAP is not in the archive, so
    it is left out; the candidate uses the same basic ranking as the message's Best Value
    section."""
    from strategy_engine import ticker_expected_range

    spot = float(payload.get("spot") or 0)
    vol = payload.get("volume") or {}
    prev_vol = (prev_payload.get("volume") or {}) if prev_payload else None
    return game_plan.build_plan(
        ticker=ticker, spot=spot,
        trend=ema_stack.banner_for_archive(payload, now=datetime.now(ET)),
        gamma=gamma, vwap=None, expected=ticker_expected_range(spot, vol),
        picks=_build_best_value_df(vol, spot, prev_vol, min_volume=500),
        session_high=(payload.get("session") or {}).get("day_high"),
        session_low=(payload.get("session") or {}).get("day_low"),
        stop_buffer_pct=stop_buffer_pct,
        min_reward_to_risk=min_reward_to_risk,
    )
