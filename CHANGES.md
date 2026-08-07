# Scoring engine changes

Attribution rows must be segmented by `config_hash` / `engine_tag`. Never pool
across versions when measuring lift.

| engine_tag   | config_hash       | window                         | note |
|--------------|-------------------|--------------------------------|------|
| engine-v1    | `dc2906741dbb2b15` | through 2026-08-07 (pre-fix) | Signed delta in leverage → puts floored at `_minmax` min |
| engine-v1.1  | `1e191ea1832c2c9a` | from next full session after fix | Leverage uses `abs(delta)`; flow/multipliers/blend unchanged |

## engine-v1.1 (2026-08-07)

**Bug:** Put delta is negative, so `delta × spot / price` was negative for every
put. `_minmax` across the mixed universe put the most-negative put at 0.000 —
every put lost on 40% of `Value_Score` regardless of leverage magnitude.

**Fix:** `lev = abs(delta) * spot / price`. Direction stays in `side` and the
directional multipliers.

**Do not pool** engine-v1 rows with engine-v1.1 rows in outcome analysis.
Restart the 15-session measurement clock from the next full trading day.
