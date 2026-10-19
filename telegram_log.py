"""telegram_log — one line per Telegram send, for Settings → Telegram.

Every send (the scheduler's scan message, gamma-shift alert and refusal alert, and the
app's manual push) appends one JSON line to ``data/telegram_sent.jsonl``: when, ticker,
what kind of message, sent or failed, Telegram's reason on failure, which bot and chat
(masked), and the start of the text.

Never stored: the bot token. The bot is identified by its numeric id (the part before
the ':', which Telegram shows publicly); the chat by its last 4 digits. Writing never
raises, so a full disk cannot stop a message. No network, no Streamlit.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data",
                    "telegram_sent.jsonl")
KEEP = 500            # lines kept when the file is trimmed
TRIM_AT = 1000        # trim once the file has this many lines
PREVIEW = 600         # characters of message text kept

KIND_LABELS = {"scan": "Scan message", "gamma_shift": "Gamma shift alert",
               "refusal": "Scanner stopped alert", "manual": "Sent from the app",
               "setup_1100": "11:00 setup alert"}


def bot_id(token: str | None) -> str:
    """The bot's public numeric id from its token ('123456:ABC…' → '123456'), '' if none."""
    head = str(token or "").split(":", 1)[0].strip()
    return head if head.isdigit() else ""


def mask_chat(chat_id: Any) -> str:
    s = str(chat_id or "").strip()
    return f"…{s[-4:]}" if len(s) > 4 else s


def telegram_reason(body: str) -> str:
    """Telegram's own reason from an error response body ('Bad Request: chat not
    found'); the raw text, shortened, when it is not Telegram's JSON."""
    try:
        data = json.loads(body)
        if isinstance(data, dict) and data.get("description"):
            return str(data["description"])
    except (TypeError, ValueError):
        pass
    return str(body or "").strip()[:200]


def _plain(text: str) -> str:
    return re.sub(r"<[^>]+>", "", str(text or ""))


def record(*, ticker: str, kind: str, ok: bool, token: str | None, chat_id: Any,
           text: str, error: str = "", plain_fallback: bool = False,
           source: str = "scheduler", path: str | None = None,
           now: datetime | None = None) -> None:
    entry = {
        "ts": (now or datetime.now(ET)).astimezone(ET).isoformat(timespec="seconds"),
        "ticker": str(ticker or "").upper(), "kind": kind, "ok": bool(ok),
        "error": str(error or ""), "plain_fallback": bool(plain_fallback),
        "bot_id": bot_id(token), "chat": mask_chat(chat_id), "source": source,
        "chars": len(text or ""), "preview": _plain(text)[:PREVIEW],
    }
    target = path or PATH
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _trim(target)
    except OSError:
        pass


def _trim(target: str) -> None:
    with open(target, encoding="utf-8") as fh:
        lines = fh.readlines()
    if len(lines) < TRIM_AT:
        return
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.writelines(lines[-KEEP:])
    os.replace(tmp, target)


def recent(n: int = 50, path: str | None = None) -> list[dict]:
    """The last ``n`` sends, newest first. Unreadable lines are skipped."""
    try:
        with open(path or PATH, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            out.append(e)
        if len(out) >= n:
            break
    return out


def last_per_ticker(entries: list[dict]) -> dict[str, dict]:
    """Newest entry per ticker from ``recent`` (which is newest first)."""
    out: dict[str, dict] = {}
    for e in entries:
        out.setdefault(e.get("ticker") or "", e)
    return out


def status_text(e: dict) -> str:
    if e.get("ok"):
        return "✅ Sent as plain text" if e.get("plain_fallback") else "✅ Sent"
    return f"❌ Failed: {e.get('error') or 'unknown'}"
