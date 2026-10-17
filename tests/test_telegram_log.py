"""Telegram send record (telegram_log.py) and Settings' three tabs."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import scheduler
import telegram_log as tl

ET = ZoneInfo("America/New_York")
TOKEN = "8123458869:AAsecretPART"


def _resp(status=200):
    class R:
        def __init__(self): self.status = status
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return R()


def _http(code, description):
    body = json.dumps({"ok": False, "error_code": code, "description": description})
    return urllib.error.HTTPError("u", code, "x", {}, io.BytesIO(body.encode()))


# ── the record ──────────────────────────────────────────────────────────────

def test_record_never_stores_the_token_and_masks_the_chat():
    tl.record(ticker="spy", kind="scan", ok=True, token=TOKEN, chat_id="115681240",
              text="<b>SPY</b> report", now=datetime(2026, 10, 16, 15, 0, tzinfo=ET))
    raw = open(tl.PATH).read()
    assert "secret" not in raw and "AAsecretPART" not in raw
    e = tl.recent()[0]
    assert e["ticker"] == "SPY" and e["bot_id"] == "8123458869" and e["chat"] == "…1240"
    assert e["preview"] == "SPY report" and e["ts"].startswith("2026-10-16T15:00")


def test_recent_is_newest_first_and_skips_bad_lines():
    for i in range(3):
        tl.record(ticker="AAPL", kind="scan", ok=True, token=TOKEN, chat_id=1, text=f"m{i}")
    with open(tl.PATH, "a") as fh:
        fh.write("not json\n")
    assert [e["preview"] for e in tl.recent(2)] == ["m2", "m1"]


def test_file_is_trimmed(monkeypatch):
    monkeypatch.setattr(tl, "TRIM_AT", 10)
    monkeypatch.setattr(tl, "KEEP", 4)
    for i in range(12):
        tl.record(ticker="AAPL", kind="scan", ok=True, token=TOKEN, chat_id=1, text=f"m{i}")
    assert len(open(tl.PATH).readlines()) < 10
    assert tl.recent(1)[0]["preview"] == "m11"


def test_unwritable_record_does_not_raise(monkeypatch, tmp_path):
    monkeypatch.setattr(tl, "PATH", str(tmp_path / "file" / "x.jsonl"))
    (tmp_path / "file").write_text("a file, not a folder")
    tl.record(ticker="AAPL", kind="scan", ok=True, token=TOKEN, chat_id=1, text="m")


def test_helpers():
    assert tl.bot_id("abc") == "" and tl.bot_id(None) == ""
    assert tl.telegram_reason('{"ok":false,"description":"Bad Request: chat not found"}') \
        == "Bad Request: chat not found"
    assert tl.telegram_reason("<html>gateway</html>") == "<html>gateway</html>"
    entries = [{"ticker": "SPY", "ok": True}, {"ticker": "AAPL", "ok": False, "error": "x"},
               {"ticker": "SPY", "ok": False}]
    last = tl.last_per_ticker(entries)
    assert last["SPY"]["ok"] is True and tl.status_text(last["AAPL"]) == "❌ Failed: x"
    assert tl.status_text({"ok": True, "plain_fallback": True}) == "✅ Sent as plain text"


# ── the scheduler records every send ───────────────────────────────────────

def test_scheduler_records_a_sent_message(monkeypatch):
    monkeypatch.setattr(scheduler.urllib.request, "urlopen", lambda req, timeout=15: _resp())
    assert scheduler._send_telegram(TOKEN, "115681240", "<b>x</b>", ticker="SPY") is True
    e = tl.recent()[0]
    assert (e["ticker"], e["kind"], e["ok"], e["error"]) == ("SPY", "scan", True, "")


def test_scheduler_records_telegrams_reason_on_failure(monkeypatch):
    def boom(req, timeout=15):
        raise _http(403, "Forbidden: bot was blocked by the user")
    monkeypatch.setattr(scheduler.urllib.request, "urlopen", boom)
    assert scheduler._send_telegram(TOKEN, "1", "x", ticker="SPY", kind="gamma_shift") is False
    e = tl.recent()[0]
    assert e["ok"] is False and e["kind"] == "gamma_shift"
    assert e["error"] == "HTTP 403: Forbidden: bot was blocked by the user"


def test_scheduler_records_the_plain_text_resend(monkeypatch):
    calls = []

    def fake(req, timeout=15):
        calls.append(req.data.decode())
        if "parse_mode" in calls[-1]:
            raise _http(400, "Bad Request: can't parse entities")
        return _resp()
    monkeypatch.setattr(scheduler.urllib.request, "urlopen", fake)
    assert scheduler._send_telegram(TOKEN, "1", "<b>x", ticker="AAPL") is True
    e = tl.recent()[0]
    assert e["ok"] and e["plain_fallback"] and tl.status_text(e) == "✅ Sent as plain text"


def test_scheduler_records_when_the_resend_fails_too(monkeypatch):
    def fake(req, timeout=15):
        raise _http(400, "Bad Request: chat not found")
    monkeypatch.setattr(scheduler.urllib.request, "urlopen", fake)
    assert scheduler._send_telegram(TOKEN, "1", "x", ticker="AAPL") is False
    e = tl.recent()[0]
    assert not e["ok"] and e["error"].count("chat not found") == 2


def test_app_push_is_recorded(monkeypatch):
    import ui.telegram_push as tp
    monkeypatch.setattr(tp.urllib.request, "urlopen", lambda req, timeout=15: _resp())
    assert tp._send_telegram(TOKEN, "42", "hello", ticker="AAPL") == (True, "")
    e = tl.recent()[0]
    assert (e["kind"], e["source"], e["ticker"]) == ("manual", "app", "AAPL")


def test_bot_username(monkeypatch):
    import ui.telegram_push as tp

    class R:
        def __enter__(self): return io.BytesIO(b'{"ok":true,"result":{"username":"spyoptionscaner"}}')
        def __exit__(self, *a): return False
    monkeypatch.setattr(tp.urllib.request, "urlopen", lambda url, timeout=10: R())
    assert tp.bot_username(TOKEN) == (True, "@spyoptionscaner")
    assert tp.bot_username(None) == (False, "no token in .env")

    def bad(url, timeout=10):
        raise _http(401, "Unauthorized")
    monkeypatch.setattr(tp.urllib.request, "urlopen", bad)
    assert tp.bot_username(TOKEN) == (False, "HTTP 401: Unauthorized")


# ── Settings in three tabs ─────────────────────────────────────────────────

def test_settings_has_three_tabs_and_shows_recent_sends(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest
    import ui.settings_store as settings_store
    import ui.telegram_push as tg

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(tg, "_ENV_FILE", str(tmp_path / "absent.env"))
    monkeypatch.setattr(settings_store, "PATH", str(tmp_path / "ui_settings.json"))
    tl.record(ticker="SPY", kind="gamma_shift", ok=False, token=TOKEN, chat_id="115681240",
              text="SPY gamma shift", error="HTTP 400: Bad Request: chat not found")
    cfg = {"ticker": "AAPL", "top_n": 5, "min_dte": 1, "sort_by": "Volume",
           "latest_archive": None}
    at = AppTest.from_string(
        "import telegram_log, sys\n"
        f"telegram_log.PATH = {tl.PATH!r}\n"
        "from ui.pages import settings\n"
        f"settings.render({cfg!r})\n", default_timeout=60).run()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == ["⚙️ Settings", "📨 Telegram", "⏱️ Scheduler"]
    assert any("Check which bot" in b.label for b in at.button)
    assert any("SPY gamma shift" in c.value for c in at.code)
    assert not any("Scheduler & background jobs" in e.label for e in at.expander)
