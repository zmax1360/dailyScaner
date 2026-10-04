"""Scan runner, ticker discovery and archive metadata shared by the shell and pages."""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time

import streamlit as st

# Repo root (this file lives in ui/).
_SCANNER_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXCLUDED_FILE = os.path.join(_SCANNER_DIR, "tickers_excluded.json")
_BG: dict[str, dict] = {}


def _run_daily_scanner(ticker: str = "AAPL") -> tuple[bool, str]:
    """
    Invoke dailyScaner.py <ticker> with the same Python that runs Streamlit.
    Returns (success, combined_output).
    """
    t0 = time.time()
    try:
        result = subprocess.run(
            [sys.executable, "dailyScaner.py", ticker.upper()],
            cwd=_SCANNER_DIR,
            capture_output=True,
            text=True,
            timeout=1800,   # 30-min hard cap
        )
        elapsed = time.time() - t0
        out = result.stdout + ("\n" + result.stderr if result.stderr.strip() else "")
        return result.returncode == 0, f"[{elapsed:.0f}s]\n{out}"
    except subprocess.TimeoutExpired:
        return False, f"{ticker} scanner timed out after 30 minutes."
    except Exception as exc:
        return False, f"Could not launch scanner for {ticker}: {exc}"


def _load_excluded() -> set[str]:
    """Load the set of tickers excluded from auto-scan. Never raises."""
    try:
        with open(_EXCLUDED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def _discover_all_tickers() -> list[str]:
    """All tickers with at least one daily archive file (including excluded)."""
    seen: set[str] = set()
    for fpath in glob.glob("archive/*.json"):
        name  = os.path.basename(fpath)
        parts = name.split("_")
        if len(parts) >= 3:
            seen.add(parts[0].upper())
    return sorted(seen)


def _discover_tickers() -> list[str]:
    """
    Active tickers — have archive data AND are not in the excluded list.
    Used by the sidebar selector and auto-scan watcher.
    Falls back to ['AAPL'] if nothing is active.
    """
    excluded = _load_excluded()
    active   = [t for t in _discover_all_tickers() if t not in excluded]
    return active or ["AAPL"]


def _bg_state(ticker: str) -> dict:
    """Return (creating if absent) the state dict for a given ticker."""
    if ticker not in _BG:
        _BG[ticker] = {"running": False, "last_ok": None, "last_ts": None, "t0": 0.0}
    return _BG[ticker]


@st.cache_data(ttl=120)
def _scan_archive_metadata() -> list[dict]:
    """
    Read minimal metadata from every archive file. Cached for 2 minutes.
    Only parses top-level JSON fields — no heavy rendering.
    """
    rows: list[dict] = []
    for pattern, atype in [
        ("archive/*.json",        "Daily"),
        ("archive_weekly/*.json", "Weekly"),
    ]:
        for fpath in sorted(glob.glob(pattern), reverse=True):
            fname = os.path.basename(fpath)
            try:
                parts    = fname.replace(".json", "").split("_")
                # parts[0]=TICKER, parts[1]=YYYYMMDD, parts[2]=HHMM
                run_date = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:]}"
                run_time = f"{parts[2][:2]}:{parts[2][2:4]}"
            except Exception:
                run_date, run_time = "?", "?"
            try:
                with open(fpath) as f:
                    p = json.load(f)
                spot      = p.get("spot")
                direction = p.get("direction", "—")
                pc_ratio  = (p.get("volume") or {}).get("pc_ratio")
                score     = p.get("checklist_score")
            except Exception:
                spot = direction = pc_ratio = score = None
            rows.append({
                "fpath":     fpath,
                "type":      atype,
                "date":      run_date,
                "time":      run_time,
                "spot":      spot,
                "direction": direction or "—",
                "pc_ratio":  pc_ratio,
                "score":     score,
            })
    return rows
