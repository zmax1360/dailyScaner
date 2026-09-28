# Scoring engine changes

Attribution rows must be segmented by `config_hash` / `engine_tag`. Never pool
across versions when measuring lift.

| engine_tag   | config_hash       | window                         | note |
|--------------|-------------------|--------------------------------|------|
| engine-v1    | `dc2906741dbb2b15` | through 2026-08-07 (pre abs-delta) | Signed delta floored puts |
| engine-v1.1  | `1e191ea1832c2c9a` | abs(delta) leverage            | Puts comparable; 0DTE still compressed 1DTE+ |
| engine-v1.2  | `243ecda68cfc8618` | from next session after land   | Separate 0DTE / 1DTE+ normalisation pools |
| engine-v1.3  | `d60c1855a9ca0923` | from next session after land | `score_cap` 1.0 after multiplier product (F-03 / F-S1-09) |
| engine-v1.4  | `0384124ff1be03b1` | from next session after land | `max_spread_pct` 0.25 pre-rank quote gate |

## engine-v1.4

**Bug:** stale quotes with `spread_pct > 0.25` were ranked as tradeable (2026-09-14 rank 1 was 40.6%).

**Fix:** After the score cap, reject those rows (`Value_Score = NaN`) before ranking. Historical `flags.rank` / `flags.score` are unchanged. The ledger drops the same names from a reconstructed pick set and does not refill with rank 11+.

**config_hash:** `d60c1855a9ca0923` → `0384124ff1be03b1`

Do not pool v1.3 rows with v1.4.

## Full-chain volume history (recording only)

Every scan now stores each traded contract (volume > 0) with volume, open interest, bid/ask,
last and IV in `data/volume_history.db` (`volume_history.py`), after the scan passes its
quality gates. Archives still keep only the top 30 per side; this is what makes per-contract
history and "building positions" (OI rising day after day) possible. Missing values are NULL,
never 0. Recording is fail-soft and never aborts a scan. Not a scoring input — `config_hash`
unchanged. Roughly 20 MB per trading day for AAPL + NVDA.

## EMA stack banner (display only)

15-min EMA 9/21/50 trend rule shown at the top of every app page (`ema_stack.py`):
9 > 21 > 50 → bullish, calls only; 9 < 21 < 50 → bearish, puts only; anything else → no trade.
EMAs are computed from the scanner's existing 15-min bars and stored in the archive
(`timeframes.15M.ema9/ema21/ema50`). Not a scoring input — `config_hash` unchanged
(`tests/test_ema_stack.py::test_ema_stack_is_not_a_scoring_input`).

## Picks ledger (report)

`picks_ledger.py` is the daily 1DTE+ report. Default `eod_report.py` / `nightly.sh` path delegates here (`--legacy` keeps the old aggregate).

- Pick set is the first live 1DTE+ top-10 scan of the session (not a hindsight union by score).
- Session date is ET trading day (`session_date_et`); `--session YYYY-MM-DD` pins it.
- Coverage `< 80%` is `INSUFFICIENT DATA` and is excluded from aggregates. Header is `n_usable / n_picks`.
- MAE is vs `entry_bid`; `spread_cost` is a separate column. Overlay includes a same-DTE/same-side non-pick benchmark.
- Future scans pin dropped top-10 names (`flags.pinned=1`) so paths do not truncate. History is not backfilled.

## engine-v1.3

**Bug:** directional multipliers applied after min-max let `Value_Score` exceed 1.0
and perturb within-pool rank (F-03 / F-S1-09).

**Fix:** After `round(4)`, clip `Value_Score` at `SCORING["score_cap"]` (1.0) **before**
ranking. Historical `flags.rank` / `flags.score` are unchanged (live decisions).

**config_hash:** `243ecda68cfc8618` → `d60c1855a9ca0923`

Do not pool v1.2 rows with v1.3.

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
