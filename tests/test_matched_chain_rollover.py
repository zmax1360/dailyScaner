"""Chain rollover: both-sides per-contract majority + circuit breaker."""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path

from chain_quality import (
    MIN_CHAIN_ROLLOVER_MATCHES,
    chain_rollover_from_volume_blocks,
    chain_rollover_guard_disabled,
    chain_volume_rolled_over,
    note_chain_rollover_abort,
    note_chain_rollover_clean,
    reset_chain_rollover_guard_state,
)

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "archive"
GOLDEN = Path(__file__).resolve().parent / "golden"


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _vol(path: Path) -> dict:
    return _load(path)["volume"]


def _force_majority_decrease_both_sides(prev: dict) -> dict:
    """Curr where a majority of matched calls AND puts each fall."""
    curr = copy.deepcopy(prev)
    for side_key in ("top_calls", "top_puts"):
        rows = curr[side_key]
        for i, c in enumerate(rows):
            v = int(c["volume"])
            # Drop volume on > half of each side.
            if i < (len(rows) // 2) + 1:
                c["volume"] = max(0, v // 2)
            else:
                c["volume"] = v + 10
    curr["total_call_vol"] = sum(int(c["volume"]) for c in curr["top_calls"])
    curr["total_put_vol"] = sum(int(c["volume"]) for c in curr["top_puts"])
    return curr


def test_one_sided_decrease_does_not_trip():
    """Calls fall, puts rise — not a session rollover."""
    prev = {
        "top_calls": [
            {"strike": 100.0 + i, "expiry": "2026-08-15", "volume": 1000}
            for i in range(10)
        ],
        "top_puts": [
            {"strike": 90.0 + i, "expiry": "2026-08-15", "volume": 1000}
            for i in range(10)
        ],
        "total_call_vol": 10_000,
        "total_put_vol": 10_000,
    }
    curr = copy.deepcopy(prev)
    for c in curr["top_calls"]:
        c["volume"] = 400  # all calls down
    for c in curr["top_puts"]:
        c["volume"] = 1200  # puts up
    curr["total_call_vol"] = 4000
    curr["total_put_vol"] = 12_000
    assert chain_volume_rolled_over(10_000, 10_000, 4000, 12_000)  # OR legacy
    rolled, detail = chain_rollover_from_volume_blocks(prev, curr)
    assert rolled is False, detail
    assert detail["reason"] == "ok"


def test_majority_both_sides_trips():
    base = ARCHIVE / "AAPL_20260803_095148.json"
    if not base.exists():
        base = GOLDEN / "AAPL_20260728_093102.json"
    prev = _vol(base)
    curr = _force_majority_decrease_both_sides(prev)
    rolled, detail = chain_rollover_from_volume_blocks(prev, curr)
    assert rolled is True, detail
    assert detail["reason"] == "per_contract_majority_both_sides"
    assert detail["n_matched"] >= MIN_CHAIN_ROLLOVER_MATCHES


def test_sum_decrease_without_majority_does_not_trip():
    """
    A few large contracts falling can drop the matched SUM while a majority
    of names are flat/up — that must not abort (2026-08-05 failure mode).
    """
    prev = {
        "top_calls": (
            [{"strike": 200.0, "expiry": "2026-08-15", "volume": 50_000}]
            + [
                {"strike": 201.0 + i, "expiry": "2026-08-15", "volume": 1_000}
                for i in range(9)
            ]
        ),
        "top_puts": (
            [{"strike": 180.0, "expiry": "2026-08-15", "volume": 50_000}]
            + [
                {"strike": 181.0 + i, "expiry": "2026-08-15", "volume": 1_000}
                for i in range(9)
            ]
        ),
        "total_call_vol": 59_000,
        "total_put_vol": 59_000,
    }
    curr = copy.deepcopy(prev)
    curr["top_calls"][0]["volume"] = 1_000  # one big drop
    curr["top_puts"][0]["volume"] = 1_000
    for c in curr["top_calls"][1:]:
        c["volume"] = 1_100
    for c in curr["top_puts"][1:]:
        c["volume"] = 1_100
    rolled, detail = chain_rollover_from_volume_blocks(prev, curr)
    assert rolled is False, detail


def test_aug3_churn_style_does_not_trip():
    path = ARCHIVE / "AAPL_20260803_095148.json"
    if not path.exists():
        return
    prev = _vol(path)
    curr = copy.deepcopy(prev)
    for c in curr["top_calls"]:
        c["volume"] = int(c["volume"]) + 100
    puts = sorted(curr["top_puts"], key=lambda r: int(r["volume"]), reverse=True)
    kept = puts[8:]
    for p in kept:
        p["volume"] = int(p["volume"]) + 10
    replacements = [
        {**d, "strike": float(d["strike"]) + 50 + i, "volume": 50}
        for i, d in enumerate(puts[:8])
    ]
    curr["top_puts"] = kept + replacements
    curr["total_call_vol"] = int(prev["total_call_vol"]) + 25_000
    curr["total_put_vol"] = int(prev["total_put_vol"]) - 40_000
    rolled, detail = chain_rollover_from_volume_blocks(prev, curr)
    assert rolled is not True, detail


def test_consecutive_same_session_archives_do_not_trip():
    day = sorted(ARCHIVE.glob("AAPL_20260803_*.json"))
    if len(day) < 2:
        day = sorted(ARCHIVE.glob("AAPL_20260731_16*.json"))
    if len(day) < 2:
        return
    a, b = day[-2], day[-1]
    rolled, detail = chain_rollover_from_volume_blocks(_vol(a), _vol(b))
    assert rolled is not True, (a.name, b.name, detail)


def test_insufficient_overlap_skips():
    prev = {
        "top_calls": [{"strike": 100.0, "expiry": "2026-08-01", "volume": 5000}],
        "top_puts": [{"strike": 90.0, "expiry": "2026-08-01", "volume": 5000}],
        "total_call_vol": 5000,
        "total_put_vol": 5000,
    }
    curr = {
        "top_calls": [{"strike": 200.0, "expiry": "2026-08-01", "volume": 100}],
        "top_puts": [{"strike": 190.0, "expiry": "2026-08-01", "volume": 100}],
        "total_call_vol": 100,
        "total_put_vol": 100,
    }
    rolled, detail = chain_rollover_from_volume_blocks(prev, curr)
    assert rolled is None
    assert detail["reason"].startswith("insufficient_overlap")


def test_circuit_breaker_disables_after_more_than_three_aborts():
    reset_chain_rollover_guard_state()
    sess = date(2026, 8, 5)
    reason = "per_contract_majority_both_sides"
    for i in range(3):
        do_abort, st = note_chain_rollover_abort("AAPL", sess, reason)
        assert do_abort is True, (i, st)
        assert st["disabled"] is False
        assert st["consecutive"] == i + 1
    # 4th consecutive → more than 3 → disable, do not abort
    do_abort, st = note_chain_rollover_abort("AAPL", sess, reason)
    assert do_abort is False
    assert st["disabled"] is True
    assert chain_rollover_guard_disabled("AAPL", sess) is True
    # Clean scan would not re-enable a disabled guard mid-session
    note_chain_rollover_clean("AAPL", sess)
    assert chain_rollover_guard_disabled("AAPL", sess) is True
    reset_chain_rollover_guard_state()


def test_save_archive_accepts_rollover_flag(tmp_path, monkeypatch):
    import dailyScaner as ds
    import pandas as pd

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ds, "TICKER", "TEST")
    (tmp_path / "archive").mkdir()
    calls = pd.DataFrame([{
        "strike": 100.0, "expiry": "2026-08-15", "dte": 5,
        "lastPrice": 1.0, "bid": 0.9, "ask": 1.1,
        "volume": 1000, "openInterest": 500, "impliedVolatility": 0.3,
    }])
    puts = calls.copy()
    fname, _ = ds.save_archive(
        100.0, {}, calls, puts, chain_volume_rollover=True,
    )
    payload = json.loads(Path(fname).read_text())
    assert payload["chain_volume_rollover"] is True
