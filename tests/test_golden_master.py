"""Golden-master: config extraction must preserve scoring behaviour."""

from __future__ import annotations

import importlib.util
import json
import math
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
import pytz

from best_value import calculate_best_value

_GOLDEN = Path(__file__).resolve().parent / "golden"
_CHAIN = _GOLDEN / "chain_aapl.json"
_EXPECTED = _GOLDEN / "scored_expected.json"

ET = pytz.timezone("US/Eastern")
_COMPARE_COLS = [
    "side", "strike", "expiry", "dte", "last", "volume", "openInterest",
    "iv", "delta", "Value_Score", "Status", "Optimal_Strategy", "Strategy_Tag",
    "_nlev", "_nflow",
]


def _load_chain() -> tuple[pd.DataFrame, float, datetime]:
    payload = json.loads(_CHAIN.read_text())
    df = pd.DataFrame(payload["contracts"])
    now = datetime.fromisoformat(payload["now_et"])
    if now.tzinfo is None:
        now = ET.localize(now)
    return df, float(payload["spot"]), now


def _score(df: pd.DataFrame, spot: float, now: datetime) -> pd.DataFrame:
    out = calculate_best_value(
        df,
        spot_price=spot,
        min_volume=500,
        daily_bias=None,
        market_state=None,
        news_bias=None,
        vwap_state=None,
        now_et=now,
        profited_shares_pct=None,
        upper_1sd=None,
        lower_1sd=None,
        optimal_strategy=None,
        has_catalyst=False,
        spot_below_support=False,
        odte_info=None,
        pov_info=None,
    )
    return out[_COMPARE_COLS].sort_values(
        ["Value_Score", "side", "strike", "expiry"],
        ascending=[False, True, True, True],
    ).reset_index(drop=True)


def _close(a, b, *, rel=1e-9) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if isinstance(a, float) or isinstance(b, float):
        if pd.isna(a) and pd.isna(b):
            return True
        if pd.isna(a) or pd.isna(b):
            return False
        return math.isclose(float(a), float(b), rel_tol=rel, abs_tol=rel)
    return a == b


def test_golden_master_matches_expected():
    df, spot, now = _load_chain()
    got = _score(df, spot, now)
    expected = json.loads(_EXPECTED.read_text())["rows"]
    assert len(got) == len(expected)
    diffs = []
    for i, exp in enumerate(expected):
        row = got.iloc[i]
        for col in _COMPARE_COLS:
            gv = row[col]
            if isinstance(gv, float) and pd.isna(gv):
                gv = None
            elif hasattr(gv, "item"):
                try:
                    gv = gv.item()
                except Exception:
                    pass
            ev = exp[col]
            if not _close(gv, ev):
                diffs.append(
                    f"row{i} {exp.get('side')} {exp.get('strike')} "
                    f"{exp.get('expiry')} {col}: got={gv!r} expected={ev!r}"
                )
    assert not diffs, "golden divergence:\n" + "\n".join(diffs[:40])


def test_pre_refactor_engine_matches_current(tmp_path):
    """
    Load best_value.py from pre-config commit 6a113f8 and compare scores.
    If they differ, this test fails with the field-level report (finding).
    """
    import subprocess

    pre = tmp_path / "best_value_pre.py"
    src = subprocess.check_output(
        ["git", "show", "6a113f8:best_value.py"],
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    pre.write_bytes(src)
    spec = importlib.util.spec_from_file_location("best_value_pre", pre)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    df, spot, now = _load_chain()
    current = _score(df, spot, now)
    old = mod.calculate_best_value(
        df.copy(),
        spot_price=spot,
        min_volume=500,
        daily_bias=None,
        market_state=None,
        news_bias=None,
        vwap_state=None,
        now_et=now,
        profited_shares_pct=None,
        upper_1sd=None,
        lower_1sd=None,
        optimal_strategy=None,
        has_catalyst=False,
        spot_below_support=False,
        odte_info=None,
        pov_info=None,
    )
    # Old engine may lack _nlev/_nflow — compare Value_Score + identity + Status
    cols = ["side", "strike", "expiry", "Value_Score", "Status"]
    for c in cols:
        assert c in old.columns, f"pre-refactor missing column {c}"
    old = old[cols].sort_values(
        ["Value_Score", "side", "strike", "expiry"],
        ascending=[False, True, True, True],
    ).reset_index(drop=True)
    cur = current[cols].reset_index(drop=True)
    assert len(old) == len(cur)
    diffs = []
    for i in range(len(cur)):
        for col in cols:
            gv, ev = cur.iloc[i][col], old.iloc[i][col]
            if isinstance(gv, float) and pd.isna(gv):
                gv = None
            if isinstance(ev, float) and pd.isna(ev):
                ev = None
            if not _close(gv, ev):
                diffs.append(
                    f"row{i} {cur.iloc[i]['side']} {cur.iloc[i]['strike']} "
                    f"{cur.iloc[i]['expiry']} {col}: current={gv!r} pre={ev!r}"
                )
    assert not diffs, (
        "pre-refactor vs current divergence (do not paper over):\n"
        + "\n".join(diffs[:50])
    )
