"""CURSOR_STALE_VOLUME_FIX — EOD-reference stale volume detector."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import pytz

from best_value import attach_dvol
from chain_quality import (
    eod_volume_lookup,
    find_prior_eod_archive,
    is_volume_stale_vs_eod,
    majority_stale_abort,
    prior_trading_day,
    stale_check_active,
)

ET = pytz.timezone("US/Eastern")
GOLDEN = Path(__file__).resolve().parent / "golden"


def _load(name: str) -> dict:
    return json.loads((GOLDEN / name).read_text())


def _as_eod(archive: dict, *, converged: bool = True) -> dict:
    """Annotate a golden capture as a usable EOD reference."""
    out = dict(archive)
    out["is_eod"] = True
    out["settlement_converged"] = converged
    return out


def _row(strike, expiry, volume, side="CALL"):
    return {
        "side": side,
        "strike": float(strike),
        "expiry": expiry,
        "volume": int(volume),
        "last": 1.0,
        "openInterest": 1000,
        "dte": 3,
        "iv": 0.3,
    }


def test_stale_contract_detected_from_eod_reference():
    """340C 7/29 at 36,654 vs EOD 35,033 → stale."""
    eod = _as_eod(_load("AAPL_20260727_160721.json"))
    morning = _load("AAPL_20260728_093102.json")
    lookup = eod_volume_lookup(eod)
    row = next(
        c for c in morning["volume"]["top_calls"]
        if float(c["strike"]) == 340.0 and c["expiry"] == "2026-07-29"
    )
    assert is_volume_stale_vs_eod(row["volume"], lookup[("CALL", 340.0, "2026-07-29")])
    now = ET.localize(datetime(2026, 7, 28, 9, 31))
    df = pd.DataFrame([_row(340.0, "2026-07-29", row["volume"])])
    out = attach_dvol(df, None, eod_vol_lookup=lookup, now_et=now)
    assert bool(out.loc[0, "stale_volume"]) is True
    assert bool(out.loc[0, "dvol_suspect"]) is True
    assert pd.isna(out.loc[0, "dVol"])


def test_rolled_contract_not_flagged_by_eod_check():
    """340C 7/31 at 1,011 vs EOD 11,216 — NOT stale by EOD rule."""
    eod = _as_eod(_load("AAPL_20260727_160721.json"))
    morning = _load("AAPL_20260728_095049.json")
    lookup = eod_volume_lookup(eod)
    row = next(
        c for c in morning["volume"]["top_calls"]
        if float(c["strike"]) == 340.0 and c["expiry"] == "2026-07-31"
    )
    assert not is_volume_stale_vs_eod(
        row["volume"], lookup[("CALL", 340.0, "2026-07-31")]
    )
    now = ET.localize(datetime(2026, 7, 28, 9, 50))
    # Prior scan still had 11534 — decrease detector owns this
    prev = _load("AAPL_20260728_093102.json")["volume"]
    df = pd.DataFrame([_row(340.0, "2026-07-31", row["volume"])])
    out = attach_dvol(df, prev, eod_vol_lookup=lookup, now_et=now)
    assert bool(out.loc[0, "stale_volume"]) is False
    assert bool(out.loc[0, "dvol_suspect"]) is True  # decrease
    assert pd.isna(out.loc[0, "dVol"])
    assert out.attrs.get("n_decrease_suspect") == 1
    assert out.attrs.get("n_eod_stale") == 0


def test_stale_volume_is_nan_not_zero():
    eod = _as_eod(_load("AAPL_20260727_160721.json"))
    lookup = eod_volume_lookup(eod)
    now = ET.localize(datetime(2026, 7, 28, 9, 31))
    df = pd.DataFrame([_row(340.0, "2026-07-29", 36654)])
    out = attach_dvol(df, None, eod_vol_lookup=lookup, now_et=now)
    v = out.loc[0, "dVol"]
    assert pd.isna(v)
    assert v != 0 and v != 0.0


def test_missing_eod_archive_skips_check_and_warns(tmp_path, caplog):
    now = ET.localize(datetime(2026, 7, 28, 9, 31))
    with caplog.at_level(logging.WARNING):
        arch, reason = find_prior_eod_archive("AAPL", str(tmp_path), now_et=now)
    assert arch is None
    assert "no is_eod" in reason or "missing" in reason or "no EOD" in reason
    df = pd.DataFrame([_row(340.0, "2026-07-29", 36654)])
    # No eod lookup — not marked stale; decrease still works
    prev = {"top_calls": [{"strike": 340.0, "expiry": "2026-07-29", "volume": 35000}],
            "top_puts": []}
    out = attach_dvol(df, prev, eod_vol_lookup=None, now_et=now)
    assert bool(out.loc[0, "stale_volume"]) is False
    assert out.loc[0, "dVol"] == 1654.0


def test_unconverged_eod_archive_is_not_used_as_reference(tmp_path):
    eod = _as_eod(_load("AAPL_20260727_160721.json"), converged=False)
    path = tmp_path / "AAPL_20260727_160721.json"
    path.write_text(json.dumps(eod))
    now = ET.localize(datetime(2026, 7, 28, 9, 31))
    arch, reason = find_prior_eod_archive("AAPL", str(tmp_path), now_et=now)
    assert arch is None
    assert "settlement_converged" in reason


def test_stale_ratio_boundary():
    # 0.94 of EOD → not stale; 0.96 → stale
    eod_v = 10000
    assert not is_volume_stale_vs_eod(9400, eod_v, ratio=0.95)
    assert is_volume_stale_vs_eod(9600, eod_v, ratio=0.95)


def test_check_not_applied_after_cutoff():
    eod = _as_eod(_load("AAPL_20260727_160721.json"))
    lookup = eod_volume_lookup(eod)
    now = ET.localize(datetime(2026, 7, 28, 14, 0))
    assert not stale_check_active(now)
    df = pd.DataFrame([_row(340.0, "2026-07-29", 36654)])
    out = attach_dvol(df, None, eod_vol_lookup=lookup, now_et=now)
    assert bool(out.loc[0, "stale_volume"]) is False
    assert pd.isna(out.loc[0, "dVol"])  # no prev → NaN entrant, not suspect


def test_majority_stale_chain_aborts_scan():
    eod = _as_eod(_load("AAPL_20260727_160721.json"))
    morning = _load("AAPL_20260728_093102.json")["volume"]
    lookup = eod_volume_lookup(eod)
    from chain_quality import flag_stale_vs_eod

    flags = flag_stale_vs_eod(morning["top_calls"][:30], lookup, side="CALL")
    n_stale = sum(flags)
    assert n_stale > 15  # large share at 09:31
    assert majority_stale_abort(n_stale, len(flags)) is True
    assert majority_stale_abort(1, 30) is False


def test_friday_eod_used_as_monday_reference(tmp_path):
    # Friday 2026-07-24 → Monday 2026-07-27 should use Friday
    assert prior_trading_day(datetime(2026, 7, 27).date()).isoformat() == "2026-07-24"
    fri = _as_eod(_load("AAPL_20260727_160721.json"))
    # Rewrite timestamp to Friday Jul 24 EOD
    fri["timestamp"] = "2026-07-24T16:20:00-04:00"
    (tmp_path / "AAPL_20260724_162000.json").write_text(json.dumps(fri))
    now = ET.localize(datetime(2026, 7, 27, 9, 35))  # Monday
    arch, reason = find_prior_eod_archive("AAPL", str(tmp_path), now_et=now)
    assert reason == "ok"
    assert arch is not None
    assert arch["timestamp"].startswith("2026-07-24")
