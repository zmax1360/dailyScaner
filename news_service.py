#!/usr/bin/env python3
"""
news_service.py — Standalone news & catalyst sentiment module.

Decoupled from the Streamlit dashboard and daily scanner. Prefers Finnhub
company news; falls back to yfinance when the key is missing/invalid or
Finnhub returns no headlines.

Usage:
    from news_service import get_news_sentiment
    result = get_news_sentiment("AAPL")

    python3 news_service.py          # self-test for AAPL
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any

# ── Optional .env loader (no python-dotenv dependency) ─────────────────────────
_ENV_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _load_dotenv() -> None:
    """Inject KEY=VAL pairs from .env into os.environ if not already set."""
    try:
        with open(_ENV_FILE) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass


_load_dotenv()

# ── Keyword lexicons ───────────────────────────────────────────────────────────
_BULLISH_KEYWORDS = (
    "upgrade", "upgraded", "raises target", "price target raised",
    "record revenue", "record profit", "record sales",
    "approval", "fda approval", "cleared", "authorized",
    "beat", "beats", "beats estimates", "tops estimates", "exceeds",
    "buyback", "share repurchase", "stock split",
    "bullish", "outperform", "overweight", "strong buy",
    "partnership", "deal", "acquisition", "acquires", "merger",
    "guidance raise", "raises guidance", "raised guidance",
    "surge", "soars", "breakout", "all-time high", "ath",
)

_BEARISH_KEYWORDS = (
    "downgrade", "downgraded", "cuts target", "price target cut",
    "investigation", "probe", "subpoena",
    "lawsuit", "sued", "litigation", "class action",
    "delay", "delayed", "halt", "halted", "suspend", "suspended",
    "ban", "banned", "restriction",
    "tariff", "tariffs", "sanctions",
    "miss", "misses", "missed estimates", "falls short",
    "bearish", "underperform", "underweight", "sell rating",
    "guidance cut", "cuts guidance", "lowered guidance", "warns",
    "layoff", "layoffs", "restructuring", "bankruptcy",
    "plunge", "plunges", "crash", "selloff", "fraud",
)


def _neutral_payload(ticker: str) -> dict[str, Any]:
    """Safe fallback when every source fails."""
    return {
        "ticker": ticker.upper(),
        "catalyst_score": 0.0,
        "news_bias": "NEUTRAL",
        "headline_count": 0,
        "top_headlines": [],
    }


def calculate_catalyst_score(headlines: list[str]) -> tuple[float, str]:
    """
    Rule-based catalyst scorer.

    Returns (catalyst_score in [-1.0, +1.0], news_bias).
    Each headline contributes +1 per bullish hit and -1 per bearish hit;
    the net is normalised by headline count and clipped to [-1, 1].
    """
    if not headlines:
        return 0.0, "NEUTRAL"

    total = 0.0
    for raw in headlines:
        text = (raw or "").lower()
        bull = sum(1 for kw in _BULLISH_KEYWORDS if kw in text)
        bear = sum(1 for kw in _BEARISH_KEYWORDS if kw in text)
        total += max(-2.0, min(2.0, float(bull - bear)))

    avg = total / max(len(headlines), 1)
    score = max(-1.0, min(1.0, avg / 2.0))
    score = round(score, 4)

    if score >= 0.15:
        bias = "BULLISH"
    elif score <= -0.15:
        bias = "BEARISH"
    else:
        bias = "NEUTRAL"
    return score, bias


def score_headline(text: str) -> str:
    """Per-article bias label for UI badges."""
    _, bias = calculate_catalyst_score([text or ""])
    return bias


def _parse_iso_ts(value: Any) -> int:
    """Parse ISO / epoch-ish timestamps to unix seconds (0 on failure)."""
    if value is None or value == "":
        return 0
    if isinstance(value, (int, float)):
        ts = int(value)
        # ms → s
        return ts // 1000 if ts > 10_000_000_000 else ts
    try:
        s = str(value).strip().replace("Z", "+00:00")
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0


def _normalize_article(
    *,
    headline: str,
    source: str,
    url: str,
    dt: int,
    summary: str = "",
) -> dict[str, Any] | None:
    headline = (headline or "").strip()
    if not headline:
        return None
    return {
        "headline": headline,
        "source":   (source or "").strip(),
        "url":      (url or "").strip(),
        "datetime": int(dt or 0),
        "summary":  (summary or "").strip(),
    }


def _fetch_finnhub_articles(ticker: str, lookback_hours: int) -> list[dict[str, Any]]:
    """Return normalised articles from Finnhub, or [] on any failure."""
    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if not api_key:
        return []

    try:
        import finnhub
    except ImportError:
        return []

    try:
        client = finnhub.Client(api_key=api_key)
        now_utc = datetime.now(timezone.utc)
        start   = now_utc - timedelta(hours=max(1, int(lookback_hours)))
        articles = client.company_news(
            ticker,
            _from=start.strftime("%Y-%m-%d"),
            to=now_utc.strftime("%Y-%m-%d"),
        )
        if not isinstance(articles, list) or not articles:
            return []

        cutoff_ts = int(start.timestamp())
        recent = [a for a in articles if int(a.get("datetime") or 0) >= cutoff_ts]
        pool   = recent if recent else articles
        pool   = sorted(pool, key=lambda a: int(a.get("datetime") or 0), reverse=True)

        out: list[dict[str, Any]] = []
        for a in pool:
            item = _normalize_article(
                headline=a.get("headline") or "",
                source=a.get("source") or "",
                url=a.get("url") or "",
                dt=int(a.get("datetime") or 0),
                summary=a.get("summary") or "",
            )
            if item:
                out.append(item)
        return out
    except Exception:
        return []


def _yf_extract_url(content: dict) -> str:
    for key in ("clickThroughUrl", "canonicalUrl"):
        val = content.get(key)
        if isinstance(val, dict) and val.get("url"):
            return str(val["url"])
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _fetch_yfinance_articles(ticker: str) -> list[dict[str, Any]]:
    """
    Fallback via yfinance.Ticker(ticker).news.
    Schema matches Finnhub-normalised articles.
    """
    try:
        import yfinance as yf
    except ImportError:
        return []

    try:
        raw = yf.Ticker(ticker).news or []
    except Exception:
        return []

    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        # Modern Yahoo shape: {id, content: {...}}
        content = item.get("content") if isinstance(item.get("content"), dict) else None
        if content:
            provider = content.get("provider") or {}
            source = ""
            if isinstance(provider, dict):
                source = provider.get("displayName") or provider.get("sourceId") or ""
            item_n = _normalize_article(
                headline=content.get("title") or "",
                source=source,
                url=_yf_extract_url(content),
                dt=_parse_iso_ts(content.get("pubDate") or content.get("displayTime")),
                summary=content.get("summary") or content.get("description") or "",
            )
        else:
            # Legacy flat shape
            item_n = _normalize_article(
                headline=item.get("title") or "",
                source=item.get("publisher") or "",
                url=item.get("link") or "",
                dt=_parse_iso_ts(item.get("providerPublishTime")),
                summary="",
            )
        if item_n:
            out.append(item_n)

    out.sort(key=lambda a: int(a.get("datetime") or 0), reverse=True)
    return out


def fetch_headlines(
    ticker: str,
    lookback_hours: int = 24,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """
    Fetch normalised headlines for *ticker*.

    Prefers Finnhub; falls back to yfinance when the key is missing/invalid
    or Finnhub returns zero usable headlines. Truncated to *limit*.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return []

    articles = _fetch_finnhub_articles(ticker, lookback_hours)
    if not articles:
        articles = _fetch_yfinance_articles(ticker)

    # Drop internal summary from public headline objects when returning
    trimmed = []
    for a in articles[: max(0, int(limit))]:
        trimmed.append({
            "headline": a["headline"],
            "source":   a["source"],
            "url":      a["url"],
            "datetime": a["datetime"],
            "summary":  a.get("summary", ""),
        })
    return trimmed


def get_news_sentiment(
    ticker: str,
    lookback_hours: int = 24,
    limit: int = 5,
) -> dict[str, Any]:
    """
    Fetch recent company news and return a catalyst sentiment dict.

    Schema is identical regardless of whether Finnhub or yfinance supplied
    the headlines. On total failure returns neutral catalyst_score = 0.0.
    """
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return _neutral_payload("UNKNOWN")

    # Pull a slightly larger pool so scoring has summary text available
    pool = fetch_headlines(ticker, lookback_hours=lookback_hours, limit=max(limit, 5))
    if not pool:
        return _neutral_payload(ticker)

    top = pool[: max(1, int(limit))]
    texts = [
        f"{h['headline']} {h.get('summary', '')}".strip()
        for h in top
    ]
    score, bias = calculate_catalyst_score(texts)

    return {
        "ticker": ticker,
        "catalyst_score": score,
        "news_bias": bias,
        "headline_count": len(top),
        "top_headlines": [
            {
                "headline": h["headline"],
                "source":   h["source"],
                "url":      h["url"],
                "datetime": h["datetime"],
            }
            for h in top
        ],
    }


def get_market_news(
    tickers: list[str],
    lookback_hours: int = 72,
    limit: int = 15,
) -> list[dict[str, Any]]:
    """
    Merge headlines across *tickers*, newest first, capped at *limit*.

    Each row includes ticker + per-article news_bias for UI badges.
    """
    merged: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for raw_t in tickers:
        t = (raw_t or "").strip().upper()
        if not t:
            continue
        for h in fetch_headlines(t, lookback_hours=lookback_hours, limit=limit):
            url = h.get("url") or ""
            key = url or f"{t}|{h.get('headline')}|{h.get('datetime')}"
            if key in seen_urls:
                continue
            seen_urls.add(key)
            text = f"{h.get('headline', '')} {h.get('summary', '')}".strip()
            merged.append({
                "ticker":    t,
                "headline":  h.get("headline") or "",
                "source":    h.get("source") or "",
                "url":       url,
                "datetime":  int(h.get("datetime") or 0),
                "news_bias": score_headline(text),
            })

    merged.sort(key=lambda a: int(a.get("datetime") or 0), reverse=True)
    return merged[: max(0, int(limit))]


# ── Self-test ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    result = get_news_sentiment("AAPL", lookback_hours=24)
    print(json.dumps(result, indent=2))
