"""Telegram rejects a raw < in HTML mode: on 2026-10-14 scan messages were lost."""

from __future__ import annotations

import scheduler


# ── Telegram rejects '<' in HTML: 2026-10-14 messages were lost ─────────────
def test_not_ranked_reason_is_escaped():
    import telegram_bot
    from tests.test_plan_report import GOLDEN
    import copy
    p = copy.deepcopy(GOLDEN)
    p["best_value"] = {"rows": [], "flow_dispersion": 0.0}
    text = telegram_bot._fmt_report(p, None, "AAPL", 5, include={"best_value": True},
                                    expiry_drill=[])
    assert "<0.05" not in text
    import re
    for tag in re.findall(r"<[^>]*>", text):
        assert re.fullmatch(r"</?(b|i|u|s|code|pre)>", tag), tag


def test_plain_text_fallback_strips_tags():
    assert scheduler.plain_text("⭐ <b>1DTE+</b> <i>(low:0.0&lt;0.05)</i>") == \
        "⭐ 1DTE+ (low:0.0<0.05)"


def test_a_400_is_resent_as_plain_text(monkeypatch):
    import io
    import urllib.error
    sent = []

    def fake_open(req, timeout=15):
        sent.append(req.data.decode())
        if "parse_mode" in sent[-1]:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO())
        class R:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()

    monkeypatch.setattr(scheduler.urllib.request, "urlopen", fake_open)
    assert scheduler._send_telegram("t", "c", "<b>x</b> <i>a&lt;b</i>") is True
    assert len(sent) == 2 and "parse_mode" not in sent[1]
