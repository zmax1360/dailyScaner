"""
notify_delivery.py — delivery-layer gates for Telegram / scheduler notifies.

NOT in config.py — must not move config_hash. Thresholds live here.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Delivery thresholds (not hashed into config_hash)
ARCHIVE_MAX_AGE_MIN = 15
MIN_SCORED_FOR_RANKING = 5
MIN_NONNULL_DVOL = 3
MIN_FLOW_IQR = 0.05
REFUSAL_STREAK_ALERT_AT = 3  # send one alert on this consecutive count


def parse_abort_reason(stderr: str | None) -> str | None:
    """Extract ABORT_REASON=… from scanner stderr."""
    if not stderr:
        return None
    for line in stderr.splitlines():
        line = line.strip()
        if line.startswith("ABORT_REASON="):
            return line.split("=", 1)[1].strip() or None
    return None


def archive_age_minutes(payload: Mapping[str, Any] | None, *, now: datetime | None = None) -> float | None:
    if not payload or not payload.get("timestamp"):
        return None
    try:
        ts = datetime.fromisoformat(str(payload["timestamp"]).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=ET)
    now = now or datetime.now(ET)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    return (now.astimezone(ET) - ts.astimezone(ET)).total_seconds() / 60.0


def archive_is_fresh(
    payload: Mapping[str, Any] | None,
    *,
    max_age_min: float = ARCHIVE_MAX_AGE_MIN,
    now: datetime | None = None,
) -> bool:
    age = archive_age_minutes(payload, now=now)
    if age is None:
        return False
    if payload.get("chain_volume_rollover"):
        return False
    return age <= float(max_age_min)


def flow_dispersion_iqr(nflow_values: list[float]) -> float | None:
    """IQR of the flow leg (_nflow). None if fewer than 4 finite values."""
    vals = sorted(float(v) for v in nflow_values if v == v)  # drop NaN
    if len(vals) < 4:
        return None
    n = len(vals)

    def _pct(p: float) -> float:
        i = (n - 1) * p
        lo = int(i)
        hi = min(lo + 1, n - 1)
        frac = i - lo
        return vals[lo] * (1 - frac) + vals[hi] * frac

    return _pct(0.75) - _pct(0.25)


def ranking_has_signal(
    best_value: Mapping[str, Any] | None,
    *,
    min_scored: int = MIN_SCORED_FOR_RANKING,
    min_dvol: int = MIN_NONNULL_DVOL,
    min_flow_iqr: float = MIN_FLOW_IQR,
) -> tuple[bool, str]:
    """
    Refuse to publish a ranking when the flow leg is flat / missing.

    Returns (ok, reason). reason is 'ok' when publishable.
    """
    if not best_value:
        return False, "no_best_value_payload"
    rows = list(best_value.get("rows") or [])
    if len(rows) < int(min_scored):
        return False, f"too_few_scored:{len(rows)}<{int(min_scored)}"

    n_dvol = sum(
        1 for r in rows
        if r.get("dVol") is not None and r.get("dVol") == r.get("dVol")
    )
    if n_dvol < int(min_dvol):
        return False, f"too_few_dvol:{n_dvol}<{int(min_dvol)}"

    nflows: list[float] = []
    for r in rows:
        v = r.get("_nflow")
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:
            nflows.append(f)
    disp = flow_dispersion_iqr(nflows)
    if disp is None:
        return False, "flow_dispersion_unavailable"
    if disp < float(min_flow_iqr):
        return False, f"flow_dispersion_low:{disp:.4f}<{float(min_flow_iqr)}"
    return True, "ok"


def provenance_line(best_value: Mapping[str, Any] | None, payload: Mapping[str, Any] | None) -> str | None:
    """
    Footer for Best Value messages. Returns None if any field is missing —
    caller must omit the entire Best Value section.
    """
    if not best_value or not payload:
        return None
    n = best_value.get("n_scored")
    disp = best_value.get("flow_dispersion")
    engine = best_value.get("engine_sha") or best_value.get("config_hash")
    ts_raw = payload.get("timestamp")
    if n is None or disp is None or not engine or not ts_raw:
        return None
    try:
        ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ET)
        clock = ts.astimezone(ET).strftime("%H:%M:%S")
    except ValueError:
        return None
    try:
        n_i = int(n)
        d_f = float(disp)
    except (TypeError, ValueError):
        return None
    eng = str(engine)[:8]
    return (
        f"scan {clock} ET · {n_i} contracts scored · "
        f"flow dispersion {d_f:.2f} · engine {eng}"
    )


def serialize_best_value_rows(df, *, top_n: int = 30) -> dict[str, Any]:
    """
    Snapshot scored rows for the archive. Call only on a successful scan path.
    """
    import math

    from attribution import config_hash
    from config import SCORING

    if df is None or getattr(df, "empty", True):
        return {
            "rows": [],
            "n_scored": 0,
            "flow_dispersion": None,
            "engine_sha": config_hash(SCORING),
        }

    scored = df[df["Value_Score"].notna()].sort_values(
        "Value_Score", ascending=False,
    )
    n_scored = int(len(scored))
    nflows = []
    if "_nflow" in scored.columns:
        for v in scored["_nflow"].tolist():
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if f == f:
                nflows.append(f)
    disp = flow_dispersion_iqr(nflows)

    rows: list[dict[str, Any]] = []
    for _, r in scored.head(int(top_n)).iterrows():
        def _num(x):
            try:
                f = float(x)
                if math.isnan(f) or math.isinf(f):
                    return None
                return f
            except (TypeError, ValueError):
                return None

        rows.append({
            "side": str(r.get("side") or ""),
            "strike": _num(r.get("strike")),
            "expiry": str(r.get("expiry") or ""),
            "last": _num(r.get("last")),
            "volume": _num(r.get("volume")),
            "openInterest": _num(r.get("openInterest")),
            "dVol": _num(r.get("dVol")),
            "Value_Score": _num(r.get("Value_Score")),
            "Status": str(r.get("Status") or ""),
            "_nflow": _num(r.get("_nflow")),
            "_nlev": _num(r.get("_nlev")),
        })

    return {
        "rows": rows,
        "n_scored": n_scored,
        "flow_dispersion": None if disp is None else round(float(disp), 4),
        "engine_sha": config_hash(SCORING),
    }
