#!/usr/bin/env python3
"""
telegram_bot.py — Interactive Telegram bot for the Options Scanner.

Run:   python telegram_bot.py
Stop:  Ctrl+C

Conversation flow:
  /start  →  pick a ticker  →  tap a section → that section is sent immediately
          →  Magnets asks for Top N first, then sends
          →  Changes (spot / P/C) is prepended to every section message
          →  no "report done" confirmation messages
"""

import glob
import json
import logging
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from news_service import get_news_sentiment, get_market_news
from best_value import build_best_value_df, resolve_biases_for_ticker

ET       = ZoneInfo("America/New_York")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(BASE_DIR, ".env")
LOG_FILE = os.path.join(BASE_DIR, "telegram_bot.log")

# ── Logging setup ──────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),                     # terminal
        logging.FileHandler(LOG_FILE, encoding="utf-8"),       # file
    ],
)
log = logging.getLogger("tgbot")


def _now_et() -> str:
    return datetime.now(ET).strftime("%Y-%m-%d %H:%M:%S ET")

# ── Config ─────────────────────────────────────────────────────────────────────
def _load_env() -> dict:
    cfg: dict = {}
    try:
        with open(ENV_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return cfg

_ENV  = _load_env()
TOKEN = _ENV.get("TELEGRAM_BOT_TOKEN", "")
# Comma-separated allow-list. Empty → refuse everyone (fail closed).
_ALLOWED_CHAT_IDS = {
    s.strip()
    for s in (_ENV.get("TELEGRAM_CHAT_ID", "") or "").split(",")
    if s.strip()
}

if not TOKEN:
    print("ERROR: TELEGRAM_BOT_TOKEN not found in .env")
    sys.exit(1)

if not _ALLOWED_CHAT_IDS:
    print("ERROR: TELEGRAM_CHAT_ID not found in .env — bot refuses all chats")
    sys.exit(1)

API = f"https://api.telegram.org/bot{TOKEN}"


def _authorized(chat_id: int | str) -> bool:
    return str(chat_id) in _ALLOWED_CHAT_IDS

# ── Raw Telegram API ────────────────────────────────────────────────────────────
def _call(method: str, **kwargs) -> dict:
    """POST to the Telegram Bot API using stdlib only."""
    url  = f"{API}/{method}"
    body = urllib.parse.urlencode(
        {k: json.dumps(v) if isinstance(v, (dict, list)) else v
         for k, v in kwargs.items()}
    ).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read().decode(errors="replace")}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def send_msg(chat_id: int, text: str, markup=None) -> dict:
    kw = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if markup:
        kw["reply_markup"] = markup
    return _call("sendMessage", **kw)


def edit_msg(chat_id: int, msg_id: int, text: str, markup=None) -> dict:
    kw = {"chat_id": chat_id, "message_id": msg_id,
          "text": text, "parse_mode": "HTML"}
    if markup:
        kw["reply_markup"] = markup
    return _call("editMessageText", **kw)


def answer_cb(cb_id: str, toast: str = "") -> None:
    _call("answerCallbackQuery", callback_query_id=cb_id, text=toast)

# ── Archive helpers ─────────────────────────────────────────────────────────────
def _discover_tickers() -> list[str]:
    tickers: set[str] = set()
    for path in glob.glob(os.path.join(BASE_DIR, "archive", "*.json")):
        parts = os.path.basename(path).split("_")
        if len(parts) >= 3:
            tickers.add(parts[0])
    return sorted(tickers)


def _load_latest(ticker: str) -> tuple[dict | None, dict | None]:
    files = sorted(
        glob.glob(os.path.join(BASE_DIR, "archive", f"{ticker}_*.json")),
        reverse=True,
    )
    payload: dict | None = None
    prev:    dict | None = None
    if files:
        try:
            with open(files[0]) as fh:
                payload = json.load(fh)
        except Exception:
            pass
    if len(files) >= 2:
        try:
            with open(files[1]) as fh:
                prev = json.load(fh)
        except Exception:
            pass
    return payload, prev


def _available_expiries(payload: dict) -> list[str]:
    vol     = payload.get("volume") or {}
    exp_set: set[str] = set()
    for c in (vol.get("top_calls") or []) + (vol.get("top_puts") or []):
        if c.get("expiry"):
            exp_set.add(c["expiry"])
    return sorted(exp_set)

# ── Per-chat state ──────────────────────────────────────────────────────────────
# state[chat_id] = {
#   "step"         : "ticker" | "sections" | "expiries"
#   "ticker"       : str
#   "top_n"        : int
#   "include"      : dict[str, bool]
#   "expiries"     : list[str]          # selected for drill-down
#   "avail_exp"    : list[str]          # expiries in the archive
#   "menu_msg_id"  : int                # the interactive message to edit
# }
_state: dict[int, dict] = {}

_SECTIONS = [
    ("session",       "💰 Session"),
    ("mtf",           "📊 Multi-TF"),
    ("magnets",       "🧲 Magnets"),
    ("volume_expiry", "📅 Vol/Expiry"),
    ("orb",           "📍 Breakout"),
    ("deltas",        "🔁 Changes"),
    ("best_value",    "⭐ Best Value"),
    ("catalyst",      "📰 Catalyst"),
    ("market_news",   "📰 Market News"),
]
# Map section key → display label for quick lookup
_SECTION_LABELS = {k: lbl for k, lbl in _SECTIONS}


def _fresh_state() -> dict:
    return {
        "step":        "ticker",
        "ticker":      "",
        "top_n":       5,
        "expiries":    [],
        "avail_exp":   [],
        "menu_msg_id": None,
    }

# ── Keyboard builders ───────────────────────────────────────────────────────────
def _kb_tickers(tickers: list[str]) -> dict:
    rows: list[list] = []
    row:  list       = []
    for t in tickers:
        row.append({"text": t, "callback_data": f"ticker:{t}"})
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return {"inline_keyboard": rows}


def _kb_sections() -> dict:
    """
    Section menu — each button immediately sends that section's report.
    No toggle state. Two buttons per row.
    """
    rows: list[list] = []
    for i in range(0, len(_SECTIONS), 2):
        row = []
        for key, label in _SECTIONS[i : i + 2]:
            row.append({"text": label, "callback_data": f"send_section:{key}"})
        rows.append(row)
    # Bottom nav
    rows.append([
        {"text": "🔍 Expiry drill-down", "callback_data": "goto:expiries"},
        {"text": "◀ Tickers",            "callback_data": "goto:tickers"},
    ])
    return {"inline_keyboard": rows}


def _kb_magnets_n() -> dict:
    """n-selection for Magnets section — shown before sending."""
    return {"inline_keyboard": [
        [
            {"text": "Top 3",  "callback_data": "magnets_n:3"},
            {"text": "Top 5",  "callback_data": "magnets_n:5"},
            {"text": "Top 10", "callback_data": "magnets_n:10"},
            {"text": "Top 20", "callback_data": "magnets_n:20"},
        ],
        [{"text": "◀ Back to sections", "callback_data": "goto:sections"}],
    ]}


def _kb_expiries(st: dict) -> dict:
    selected = set(st.get("expiries") or [])
    rows: list[list] = []
    for exp in st.get("avail_exp") or []:
        tick = "✅" if exp in selected else "☐"
        rows.append([{"text": f"{tick}  {exp}", "callback_data": f"exp:{exp}"}])
    rows.append([
        {"text": "📤 Send drill-down", "callback_data": "send_section:expiry_drill"},
        {"text": "◀ Sections",         "callback_data": "goto:sections"},
    ])
    return {"inline_keyboard": rows}

# ── Message formatter ───────────────────────────────────────────────────────────
def _pc_bias(pc: float) -> str:
    if pc >= 1.5: return "▼ BEARISH"
    if pc >= 1.1: return "▼ MILD BEARISH"
    if pc >= 0.9: return "─ NEUTRAL"
    if pc >= 0.7: return "▲ MILD BULLISH"
    return "▲ BULLISH"


def _esc(text: str) -> str:
    """Escape HTML special chars for Telegram parse_mode=HTML."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _news_bias_icon(bias: str) -> str:
    if bias == "BULLISH":
        return "🟢"
    if bias == "BEARISH":
        return "🔴"
    return "⚪"


def _fmt_ts(ts: int) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(int(ts)).astimezone(ET).strftime("%m-%d %H:%M ET")
    except Exception:
        return "—"


def _fmt_catalyst_lines(ticker: str, news: dict | None = None) -> list[str]:
    """Live catalyst sentiment block for one ticker."""
    info = news if news is not None else get_news_sentiment(ticker)
    bias = info.get("news_bias") or "NEUTRAL"
    score = float(info.get("catalyst_score") or 0.0)
    headlines = info.get("top_headlines") or []
    icon = _news_bias_icon(bias)

    L = [
        f"📰 <b>LIVE CATALYST — {ticker}</b>",
        f"{icon} Bias <b>{bias}</b> · score <b>{score:+.2f}</b>",
    ]
    if not headlines:
        L.append("<i>No recent headlines.</i>")
        L.append("")
        return L

    for h in headlines:
        src  = _esc(h.get("source") or "Unknown")
        text = _esc(h.get("headline") or "")
        url  = (h.get("url") or "").strip()
        when = _fmt_ts(int(h.get("datetime") or 0))
        if url and text:
            L.append(f"• <b>{src}</b>: <a href=\"{url}\">{text}</a> <i>({when})</i>")
        elif text:
            L.append(f"• <b>{src}</b>: {text} <i>({when})</i>")
    L.append("")
    return L


def _fmt_market_news_lines(tickers: list[str], limit: int = 15) -> list[str]:
    """Merged headline timeline across tickers."""
    articles = get_market_news(tickers, limit=limit)
    label = tickers[0] if len(tickers) == 1 else f"{len(tickers)} tickers"
    L = [f"📰 <b>MARKET NEWS — { _esc(label) }</b>"]
    if not articles:
        L.append("<i>No headlines found.</i>")
        L.append("")
        return L

    for a in articles:
        bias = a.get("news_bias") or "NEUTRAL"
        icon = _news_bias_icon(bias)
        tkr  = _esc(a.get("ticker") or "")
        src  = _esc(a.get("source") or "Unknown")
        text = _esc(a.get("headline") or "")
        url  = (a.get("url") or "").strip()
        when = _fmt_ts(int(a.get("datetime") or 0))
        head = f'<a href="{url}">{text}</a>' if url and text else text
        L.append(
            f"{icon} <b>{tkr}</b> · <code>{bias}</code> · "
            f"<i>{src} · {when}</i>\n{head}"
        )
    L.append("")
    return L


def _safe_html_truncate(text: str, limit: int = 4000) -> str:
    """Truncate Telegram HTML without leaving unclosed tags when possible."""
    if len(text) <= limit:
        return text
    cut = text[: limit - 40]
    # Prefer cutting at a newline boundary
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    # Drop a trailing partial tag
    lt = cut.rfind("<")
    gt = cut.rfind(">")
    if lt > gt:
        cut = cut[:lt]
    return cut + "\n\n<i>…truncated</i>"


def _fmt_report(
    payload: dict,
    prev:    dict | None,
    ticker:  str,
    top_n:   int,
    include: dict,
    expiry_drill: list[str],
) -> str:
    L:       list[str] = []
    vol      = payload.get("volume") or {}
    tfs      = payload.get("timeframes") or {}
    mags     = payload.get("signal_magnets") or {}
    session  = payload.get("session") or {}
    or_data  = payload.get("or_data") or {}
    spot     = float(payload.get("spot") or 0)
    direction = payload.get("direction", "—")
    pc_ratio  = float(vol.get("pc_ratio") or 0)
    all_calls = vol.get("top_calls") or []
    all_puts  = vol.get("top_puts")  or []
    dir_icon  = "▲" if "BULL" in direction else ("▼" if "BEAR" in direction else "─")

    try:
        ts_et = (datetime.fromisoformat(payload.get("timestamp", ""))
                 .astimezone(ET).strftime("%Y-%m-%d %H:%M ET"))
    except Exception:
        ts_et = "—"

    L.append(f"<b>📊 {ticker} Options Scanner</b>")
    L.append(f"<i>{ts_et}</i>")
    L.append("")

    # ── Session ──────────────────────────────────────────────────────────────
    if include.get("session"):
        prev_close = session.get("prev_close")
        open_p     = session.get("open")
        cv = int(vol.get("total_call_vol") or 0)
        pv = int(vol.get("total_put_vol")  or 0)
        parts = [f"<b>${spot:.2f}</b>"]
        if prev_close:
            chg  = spot - prev_close
            sign = "+" if chg >= 0 else ""
            pct  = chg / prev_close * 100
            parts.append(f"{sign}${chg:.2f} ({sign}{pct:.2f}%)")
        if open_p:
            parts.append(f"Open ${open_p:.2f}")
        if prev_close:
            parts.append(f"Prev close ${prev_close:.2f}")
        L.append(f"💰 <b>{ticker}</b> · " + " · ".join(parts))
        L.append(f"📈 Direction: <b>{dir_icon} {direction}</b>")
        L.append(f"⚖️ P/C <b>{pc_ratio:.2f}</b> {_pc_bias(pc_ratio)}  "
                 f"Calls {cv:,} · Puts {pv:,}")
        L.append("")

    # ── Multi-Timeframe ───────────────────────────────────────────────────────
    if include.get("mtf") and tfs:
        L.append("📊 <b>MULTI-TIMEFRAME</b>")
        rows = ["<pre>TF    RSI    MACD    Vol×"]
        for tf in ["5M", "10M", "15M", "45M", "1H", "4H", "1D"]:
            d = tfs.get(tf)
            if not d:
                continue
            rsi  = float(d.get("rsi")  or 0)
            hist = float(d.get("hist") or 0)
            vs   = float(d.get("vs")   or 0)
            rows.append(f"{tf:<5} {rsi:>5.1f}  {hist:>+6.2f}  {vs:.1f}x")
        rows.append("</pre>")
        L.extend(rows)

    # ── Magnets ───────────────────────────────────────────────────────────────
    if include.get("magnets"):
        for label, emoji, contracts in [
            (f"TOP {top_n} CALLS", "🟢", all_calls[:top_n]),
            (f"TOP {top_n} PUTS",  "🔴", all_puts[:top_n]),
        ]:
            L.append(f"{emoji} <b>{label}</b>")
            rows = ["<pre>Strike  Expiry   Price    Vol      VOI"]
            for c in contracts:
                strike = float(c.get("strike") or 0)
                price  = float(c.get("lastPrice") or 0)
                exp    = c.get("expiry", "")[5:]   # MM-DD
                v      = int(c.get("volume") or 0)
                oi     = max(int(c.get("openInterest") or 0), 1)
                voi    = v / oi
                flag   = "🔥" if voi >= 5 else ("★" if voi >= 2 else " ")
                rows.append(
                    f"${strike:<6.1f} {exp:<8} ${price:<6.2f} {v:>7,}  {voi:>5.1f}x{flag}"
                )
            rows.append("</pre>")
            L.extend(rows)

    # ── Volume by Expiry ──────────────────────────────────────────────────────
    if include.get("volume_expiry"):
        # Aggregate volume + track top-vol call/put price per expiry
        exp_agg: dict[str, dict] = {}
        for c in all_calls:
            e = c.get("expiry", "?")
            d = exp_agg.setdefault(e, {
                "cv": 0, "pv": 0, "dte": int(c.get("dte", 0)),
                "call_px": None, "call_top_vol": 0,
                "put_px":  None, "put_top_vol":  0,
            })
            v = int(c.get("volume") or 0)
            d["cv"] += v
            if v > d["call_top_vol"]:
                d["call_top_vol"] = v
                d["call_px"] = float(c.get("lastPrice") or 0)
        for c in all_puts:
            e = c.get("expiry", "?")
            d = exp_agg.setdefault(e, {
                "cv": 0, "pv": 0, "dte": int(c.get("dte", 0)),
                "call_px": None, "call_top_vol": 0,
                "put_px":  None, "put_top_vol":  0,
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
                d    = exp_agg[exp]
                cv_  = d["cv"]; pv_ = d["pv"]; dte = d["dte"]
                if cv_ == 0 and pv_ == 0:
                    continue
                pc_  = pv_ / cv_ if cv_ else 0
                bias = _pc_bias(pc_)[:2] if cv_ and pv_ else "—"
                cpx  = f"${d['call_px']:.2f}" if d["call_px"] is not None else "—"
                ppx  = f"${d['put_px']:.2f}"  if d["put_px"]  is not None else "—"
                rows.append(
                    f"{exp[5:]:<7} {dte:>3}d {cv_:>7,} {pv_:>6,} "
                    f"{pc_:.2f}{bias} {cpx:<6} {ppx}"
                )
            rows.append("</pre>")
            L.extend(rows)

    # ── Opening Range Breakout ────────────────────────────────────────────────
    if include.get("orb") and or_data:
        L.append("📍 <b>OPENING RANGE BREAKOUT</b>")
        for tf, d in or_data.items():
            if not isinstance(d, dict):
                continue
            hi  = d.get("high", 0); lo = d.get("low", 0)
            rng = d.get("range", 0); rp = d.get("range_pct", 0)
            bias = d.get("bias", "—")
            L.append(f"<b>{tf} OR:</b> H ${hi:.2f} · L ${lo:.2f} · "
                     f"Range ${rng:.2f} ({rp:.1f}%) → <b>{bias}</b>")
        L.append("")

    # ── Deltas vs previous run ────────────────────────────────────────────────
    if include.get("deltas") and prev:
        prev_vol  = prev.get("volume") or {}
        prev_spot = float(prev.get("spot") or 0)
        prev_pc   = float(prev_vol.get("pc_ratio") or 0)
        prev_dir  = prev.get("direction", "")
        prev_mags = prev.get("signal_magnets") or {}
        try:
            prev_ts = (datetime.fromisoformat(prev.get("timestamp", ""))
                       .astimezone(ET).strftime("%Y-%m-%d %H:%M ET"))
        except Exception:
            prev_ts = "prev run"
        delta_lines = []
        if prev_spot:
            sd    = spot - prev_spot
            arrow = "↑" if sd > 0 else "↓"
            delta_lines.append(f"{arrow} Spot ${prev_spot:.2f} → ${spot:.2f} ({sd:+.2f})")
        pcd   = pc_ratio - prev_pc
        arrow = "↑" if pcd > 0 else "↓"
        delta_lines.append(f"{arrow} P/C {prev_pc:.2f} → {pc_ratio:.2f} ({pcd:+.3f})")
        if prev_dir and prev_dir != direction:
            delta_lines.append(f"🔔 Direction: {prev_dir} → {direction}")
        for side in ("call", "put"):
            cm = mags.get(side) or {}
            pm = prev_mags.get(side) or {}
            if cm and pm and cm.get("strike") != pm.get("strike"):
                lbl = "CALL MAGNET" if side == "call" else "PUT MAGNET"
                delta_lines.append(
                    f"🔄 {lbl}: ${pm.get('strike')} → ${cm.get('strike')} ← STRIKE CHANGE"
                )
        if delta_lines:
            L.append(f"🔁 <b>CHANGES vs {prev_ts}</b>")
            L.extend(delta_lines)
            L.append("")

    # ── Best Value Option (shared engine with dashboard) ──────────────────────
    if include.get("best_value"):
        prev_vol = (prev.get("volume") or {}) if prev else None
        try:
            news_bias = (get_news_sentiment(ticker) or {}).get("news_bias")
        except Exception:
            news_bias = None
        daily_bias, market_state = resolve_biases_for_ticker(
            ticker, payload.get("session") or {}, spot,
        )
        bv_df = build_best_value_df(
            vol, spot, prev_vol,
            min_volume=500,
            daily_bias=daily_bias,
            market_state=market_state,
            news_bias=news_bias,
        )
        if not bv_df.empty and bv_df["Status"].eq("⭐ BEST VALUE").any():
            has_dvol = "dVol" in bv_df.columns
            L.append("⭐ <b>BEST VALUE OPTION</b>")
            notes = []
            if news_bias and news_bias != "NEUTRAL":
                notes.append(
                    f"News {news_bias}: "
                    f"{'CALL ×1.2 · PUT ×0.8' if news_bias == 'BULLISH' else 'CALL ×0.8 · PUT ×1.2'}"
                )
            if daily_bias in ("HEAVY BEARISH", "HEAVY BULLISH"):
                notes.append(f"Daily {daily_bias}")
            if market_state in ("BEARISH DRAG", "BULLISH TAILWIND"):
                notes.append(f"Macro {market_state}")
            if notes:
                L.append("<i>" + " · ".join(notes) + "</i>")
            # Show top 3 by score (filtered rows only)
            ranked = (
                bv_df[bv_df["Value_Score"].notna()]
                .sort_values("Value_Score", ascending=False)
                .head(3)
            )
            hdr = "Side  Strike   Exp    Score  Price  VOI"
            if has_dvol:
                hdr += "    ΔVol"
            rows = [f"<pre>{hdr}"]
            for _, r in ranked.iterrows():
                voi  = r["volume"] / max(int(r["openInterest"]), 1)
                star = " ⭐" if r["Status"] == "⭐ BEST VALUE" else ""
                exp_s = r["expiry"][5:] if len(r["expiry"]) >= 7 else r["expiry"]
                line = (
                    f"{r['side']:<5} ${r['strike']:<7.1f} {exp_s:<6} "
                    f"{r['Value_Score']:.4f} ${r['last']:.2f}  {voi:.1f}x"
                )
                if has_dvol and pd.notna(r.get("dVol")):
                    line += f"  {int(r['dVol']):+,}"
                elif has_dvol:
                    line += "      —"
                line += star
                rows.append(line)
            rows.append("</pre>")
            L.extend(rows)
            # One-liner callout for the winner
            best = bv_df[bv_df["Status"] == "⭐ BEST VALUE"].iloc[0]
            voi_b = best["volume"] / max(int(best["openInterest"]), 1)
            L.append(
                f"→ <b>{best['side']} ${best['strike']:.1f}</b> "
                f"exp {best['expiry']} · score {best['Value_Score']:.4f} · "
                f"${best['last']:.2f} · {voi_b:.1f}×"
            )
            L.append("")

    # ── Live Catalyst Sentiment ───────────────────────────────────────────────
    if include.get("catalyst"):
        L.extend(_fmt_catalyst_lines(ticker))

    # ── Market News timeline (selected ticker only) ───────────────────────────
    if include.get("market_news"):
        L.extend(_fmt_market_news_lines([ticker], limit=15))

    # ── Expiry drill-down ─────────────────────────────────────────────────────
    for exp in (expiry_drill or []):
        exp_calls = [c for c in all_calls if c.get("expiry") == exp]
        exp_puts  = [c for c in all_puts  if c.get("expiry") == exp]
        if not exp_calls and not exp_puts:
            continue
        dte    = (exp_calls or exp_puts)[0].get("dte", "?")
        cv_exp = sum(int(c.get("volume") or 0) for c in exp_calls)
        pv_exp = sum(int(c.get("volume") or 0) for c in exp_puts)
        pc_exp = pv_exp / cv_exp if cv_exp else 0
        L.append(f"🔍 <b>EXPIRY DRILL-DOWN: {exp} ({dte}d) · P/C {pc_exp:.2f}</b>")
        rows = ["<pre>Side   Strike   Vol      VOI"]
        for side_lbl, contracts in [("CALL", exp_calls[:5]), ("PUT", exp_puts[:5])]:
            for c in contracts:
                strike = float(c.get("strike") or 0)
                v      = int(c.get("volume") or 0)
                oi     = max(int(c.get("openInterest") or 0), 1)
                voi    = v / oi
                flag   = "🔥" if voi >= 5 else ("★" if voi >= 2 else " ")
                rows.append(f"{side_lbl:<6} ${strike:<6.1f} {v:>7,} {voi:>6.1f}x{flag}")
        rows.append("</pre>")
        L.extend(rows)

    return "\n".join(L)

# ── Event handlers ──────────────────────────────────────────────────────────────
def _handle_start(chat_id: int) -> None:
    if not _authorized(chat_id):
        log.warning(f"[{chat_id}] unauthorized /start rejected")
        send_msg(chat_id, "⛔ Unauthorized.")
        return
    tickers = _discover_tickers()
    log.info(f"[{chat_id}] /start — tickers available: {tickers}")
    if not tickers:
        send_msg(chat_id,
                 "⚠️ No scanned tickers found.\n"
                 "Run the daily scanner first, then try again.")
        return
    _state[chat_id] = _fresh_state()
    r = send_msg(
        chat_id,
        "📊 <b>Options Scanner Bot</b>\n\nSelect a ticker:",
        markup=_kb_tickers(tickers),
    )
    if r.get("ok"):
        _state[chat_id]["menu_msg_id"] = r["result"]["message_id"]
        log.info(f"[{chat_id}] Ticker menu sent (msg_id={r['result']['message_id']})")
    else:
        log.error(f"[{chat_id}] Failed to send ticker menu: {r.get('error')}")


def _sections_prompt(ticker: str, spot, ts: str = "") -> str:
    line = f"📊 <b>{ticker}</b>  ·  ${spot}"
    if ts:
        line += f"  ·  {ts}"
    return line + "\n\nSelect a section to send:"


def _send_section_report(
    chat_id: int,
    ticker: str,
    section: str,
    top_n: int = 5,
    expiries: list[str] | None = None,
) -> None:
    """
    Load archive and send ONE section (+ Changes always prepended when available).
    No confirmation / 'report sent' messages.
    """
    payload, prev = _load_latest(ticker)
    if not payload:
        send_msg(chat_id, f"⚠️ No archive for <b>{ticker}</b>.")
        return

    include = {k: False for k, _ in _SECTIONS}
    if section == "expiry_drill":
        # drill-down only — still include deltas
        include["deltas"] = True
    else:
        include[section] = True
        include["deltas"] = True   # Changes on every message for this ticker

    report = _fmt_report(
        payload, prev, ticker, top_n,
        include, expiries or [],
    )
    report = _safe_html_truncate(report, 4000)
    r = send_msg(chat_id, report)
    if r.get("ok"):
        log.info(f"[{chat_id}] Sent section={section} ticker={ticker} "
                 f"(msg_id={r['result']['message_id']})")
    else:
        log.error(f"[{chat_id}] Section send failed: {r.get('error')}")


def _handle_callback(chat_id: int, msg_id: int, cb_id: str, data: str) -> None:
    if not _authorized(chat_id):
        answer_cb(cb_id, "Unauthorized")
        log.warning(f"[{chat_id}] unauthorized callback rejected: {data!r}")
        return
    answer_cb(cb_id)
    st = _state.setdefault(chat_id, _fresh_state())

    # ── Pick ticker ───────────────────────────────────────────────────────────
    if data.startswith("ticker:"):
        ticker = data.split(":", 1)[1]
        log.info(f"[{chat_id}] Ticker selected: {ticker}")
        payload, _ = _load_latest(ticker)
        if not payload:
            log.warning(f"[{chat_id}] No archive for {ticker}")
            edit_msg(chat_id, msg_id, f"⚠️ No archive found for <b>{ticker}</b>.")
            return
        avail = _available_expiries(payload)
        st.update({
            "step": "sections", "ticker": ticker,
            "avail_exp": avail, "expiries": [],
        })
        try:
            ts = (datetime.fromisoformat(payload["timestamp"])
                  .astimezone(ET).strftime("%Y-%m-%d %H:%M ET"))
        except Exception:
            ts = "—"
        spot = payload.get("spot", "—")
        log.info(f"[{chat_id}] Sections menu for {ticker} (spot=${spot})")
        edit_msg(
            chat_id, msg_id,
            _sections_prompt(ticker, spot, ts),
            markup=_kb_sections(),
        )

    # ── Magnets: ask for n first ──────────────────────────────────────────────
    elif data == "send_section:magnets":
        ticker = st.get("ticker", "—")
        log.info(f"[{chat_id}] Magnets n-picker for {ticker}")
        edit_msg(
            chat_id, msg_id,
            f"🧲 <b>{ticker}</b> — Magnets\n\nHow many top contracts?",
            markup=_kb_magnets_n(),
        )

    # ── Magnets n chosen → send immediately ───────────────────────────────────
    elif data.startswith("magnets_n:"):
        n = int(data.split(":", 1)[1])
        st["top_n"] = n
        ticker = st.get("ticker")
        if not ticker:
            return
        log.info(f"[{chat_id}] Sending Magnets top_n={n} for {ticker}")
        _send_section_report(chat_id, ticker, "magnets", top_n=n)
        # Restore sections menu (edit stays as menu, report is a new message)
        payload, _ = _load_latest(ticker)
        spot = payload.get("spot", "—") if payload else "—"
        edit_msg(
            chat_id, msg_id,
            _sections_prompt(ticker, spot),
            markup=_kb_sections(),
        )

    # ── Any other section → send immediately ──────────────────────────────────
    elif data.startswith("send_section:"):
        section = data.split(":", 1)[1]
        ticker  = st.get("ticker")
        if not ticker:
            return

        if section == "expiry_drill":
            expiries = st.get("expiries") or []
            if not expiries:
                edit_msg(
                    chat_id, msg_id,
                    f"📌 Select expiries for <b>{ticker}</b>\n"
                    "(tap to toggle, then press Send drill-down):",
                    markup=_kb_expiries(st),
                )
                return
            log.info(f"[{chat_id}] Sending expiry drill-down {expiries} for {ticker}")
            _send_section_report(chat_id, ticker, "expiry_drill",
                                 top_n=st.get("top_n", 5), expiries=expiries)
        else:
            log.info(f"[{chat_id}] Sending section={section} for {ticker}")
            _send_section_report(chat_id, ticker, section, top_n=st.get("top_n", 5))

        # Keep the sections menu in place — do not send a "done" message
        payload, _ = _load_latest(ticker)
        spot = payload.get("spot", "—") if payload else "—"
        edit_msg(
            chat_id, msg_id,
            _sections_prompt(ticker, spot),
            markup=_kb_sections(),
        )

    # ── Navigate to expiry picker ─────────────────────────────────────────────
    elif data == "goto:expiries":
        st["step"] = "expiries"
        ticker = st.get("ticker", "—")
        log.info(f"[{chat_id}] Opening expiry picker for {ticker}")
        edit_msg(
            chat_id, msg_id,
            f"📌 Select expiries for <b>{ticker}</b>\n"
            "(tap to toggle, then press Send drill-down):",
            markup=_kb_expiries(st),
        )

    # ── Toggle one expiry ─────────────────────────────────────────────────────
    elif data.startswith("exp:"):
        exp      = data.split(":", 1)[1]
        selected = set(st.get("expiries") or [])
        if exp in selected:
            selected.discard(exp)
        else:
            selected.add(exp)
        st["expiries"] = sorted(selected)
        ticker = st.get("ticker", "—")
        edit_msg(
            chat_id, msg_id,
            f"📌 Select expiries for <b>{ticker}</b>\n"
            "(tap to toggle, then press Send drill-down):",
            markup=_kb_expiries(st),
        )

    # ── Back to sections ──────────────────────────────────────────────────────
    elif data == "goto:sections":
        st["step"] = "sections"
        ticker = st.get("ticker", "—")
        payload, _ = _load_latest(ticker)
        spot = payload.get("spot", "—") if payload else "—"
        edit_msg(
            chat_id, msg_id,
            _sections_prompt(ticker, spot),
            markup=_kb_sections(),
        )

    # ── Back to ticker list ───────────────────────────────────────────────────
    elif data == "goto:tickers":
        log.info(f"[{chat_id}] Back to ticker list")
        st.update(_fresh_state())
        tickers = _discover_tickers()
        edit_msg(
            chat_id, msg_id,
            "📊 Select a ticker:",
            markup=_kb_tickers(tickers),
        )

# ── Main polling loop ───────────────────────────────────────────────────────────
def main() -> None:
    me = _call("getMe")
    if not me.get("ok"):
        log.error(f"Could not connect to Telegram: {me.get('error')}")
        sys.exit(1)
    username = me["result"].get("username", "unknown")
    log.info(f"Bot started as @{username}")
    log.info(f"Open Telegram → @{username} → press Start")
    log.info(f"Log file: {LOG_FILE}")
    print(f"\n✅  Bot running as @{username}  (Ctrl+C to stop)")
    print(f"    Logs → {LOG_FILE}\n")

    offset = 0
    while True:
        try:
            resp = _call(
                "getUpdates",
                offset=offset,
                timeout=25,
                allowed_updates=["message", "callback_query"],
            )
            if not resp.get("ok"):
                log.error(f"getUpdates failed: {resp.get('error')}")
                time.sleep(5)
                continue

            for update in resp.get("result", []):
                offset = update["update_id"] + 1

                if "message" in update:
                    msg      = update["message"]
                    chat_id  = msg["chat"]["id"]
                    username_ = msg["from"].get("username") or msg["from"].get("first_name", "?")
                    text     = msg.get("text", "")
                    log.info(f"[{chat_id}] ← @{username_}: {text!r}")

                    if text.startswith("/start") or text.startswith("/scan"):
                        _handle_start(chat_id)
                    elif text.startswith("/help"):
                        if not _authorized(chat_id):
                            send_msg(chat_id, "⛔ Unauthorized.")
                            log.warning(f"[{chat_id}] unauthorized /help rejected")
                        else:
                            send_msg(
                                chat_id,
                                "<b>Options Scanner Bot — commands</b>\n\n"
                                "/start — pick a ticker, then tap a section to send it\n"
                                "/scan  — same as /start\n"
                                "/help  — show this message\n\n"
                                "Each section sends immediately. "
                                "Changes (spot / P/C) is included on every message.",
                            )
                            log.info(f"[{chat_id}] → help message sent")
                    else:
                        log.info(f"[{chat_id}] Unrecognised command, ignored")

                elif "callback_query" in update:
                    cq       = update["callback_query"]
                    chat_id  = cq["message"]["chat"]["id"]
                    msg_id   = cq["message"]["message_id"]
                    data     = cq.get("data", "")
                    username_ = cq["from"].get("username") or cq["from"].get("first_name", "?")
                    log.info(f"[{chat_id}] ← @{username_} button: {data!r}")
                    _handle_callback(chat_id, msg_id, cq["id"], data)

        except KeyboardInterrupt:
            log.info("Bot stopped by user (Ctrl+C)")
            print("\n🛑  Bot stopped.")
            break
        except Exception as exc:
            log.exception(f"Unexpected error: {exc}")
            time.sleep(5)


if __name__ == "__main__":
    main()
