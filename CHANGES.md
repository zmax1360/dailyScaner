# Scoring engine changes

Attribution rows must be segmented by `config_hash` / `engine_tag`. Never pool
across versions when measuring lift.

| engine_tag   | config_hash       | window                         | note |
|--------------|-------------------|--------------------------------|------|
| engine-v1    | `dc2906741dbb2b15` | through 2026-08-07 (pre abs-delta) | Signed delta floored puts |
| engine-v1.1  | `1e191ea1832c2c9a` | abs(delta) leverage            | Puts comparable; 0DTE still compressed 1DTE+ |
| engine-v1.2  | `243ecda68cfc8618` | from next session after land   | Separate 0DTE / 1DTE+ normalisation pools |

## engine-v1.2

**Bug:** `_minmax` over the whole universe let cheap 0DTE inflate leverage and
vol/OI inflate flow, compressing every 1DTE+ contract toward zero on both legs.

**Fix:** Assign `pool` ∈ {`0DTE`, `1DTE+`} from `dte` (shared `scoring_pool.py`).
Normalise leverage and flow **within each pool**. Rank is within-pool. One ATM
control pair per pool. NULL `dte` is excluded (not silently pooled). Pools with
fewer than `min_pool_size` (5) survivors are not ranked and not merged.

**Boundary:** `dte == 0` → `0DTE`; `dte >= 1` → `1DTE+` (same as eod_report).

**config_hash:** `1e191ea1832c2c9a` → `243ecda68cfc8618`

Do not pool v1 / v1.1 rows with v1.2. Restart the 15-session clock from the next
full trading day after this lands (Monday ≈ day 1 of 15, through ~2026-08-28).
