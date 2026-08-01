# Cursor Tasks — Data quality, then delta

Branch: `attribution`

**Run these in order, one session each.** Each depends on the previous being trustworthy.
Task C computes garbage if Task B hasn't landed. Task B is meaningless if Task A hasn't.

Fixture files to copy into `tests/golden/` before starting — these are real captures and
the guards must be tested against them, not against synthetic data:

- `AAPL_20260727_160721.txt` (prior session close)
- `AAPL_20260728_093102.json` (next morning, 09:31 — all stale)
- `AAPL_20260728_095049.json` (next morning, 09:50 — partially rolled)

---

## Task A — Session rollover guard 🔴 HIGHEST PRIORITY

```
Yahoo's option-chain volume does not reset at the open. It carries the PRIOR session's
cumulative volume and rolls over per contract at staggered times during the first
half hour. This is proven by three real captures now in tests/golden/:

  contract          7/27 16:07   7/28 09:31   7/28 09:50
  340C  2026-07-29      35,033       36,654       36,654   <- still yesterday at 09:50
  337.5C 2026-07-29     26,944       28,317       28,317   <- still yesterday
  340C  2026-07-31      11,216       11,534        1,011   <- rolled over
  total_call_vol             -      397,631      218,671   <- DECREASED

Cumulative volume within a session cannot decrease. The 09:31 archive is entirely the
prior session; the 09:50 archive is a MIXTURE of two sessions with nothing marking
which rows are which.

Consequence: dVol is the difference between consecutive scans, and best_value.py:169
does `work["dVol"].abs().fillna(1.0)`. A contract rolling from 11,534 to 1,011 yields
dVol = -10,523, which .abs() converts to +10,523 and feeds into the flow leg as the
strongest possible conviction signal. The flow leg is 60% of Value_Score. The engine
is reading the calendar changing as institutional accumulation.

Implement:

1. Chain-level detection. If total_call_vol or total_put_vol is LOWER than the most
   recent prior scan of the same ET session date, the chain has rolled over. Abort the
   scan: write no archive, write no attribution rows, log the reason with both values.

2. Contract-level detection in attach_dvol. If current volume < prior volume for a
   given (side, strike, expiry), that contract rolled over. Set its dVol to NaN and
   flag the row (e.g. a `dvol_suspect` boolean). Do NOT take the absolute value of a
   negative dVol under any circumstance.

3. Remove the `.abs()` at best_value.py:169. Direction of flow is signal, not noise.
   A liquidation and an accumulation must not score identically. Keep the existing
   NaN handling behaviour otherwise unchanged in this task — the fillna(1.0)
   inversion is a separate known defect (F-05) and is covered by an xfail test; do
   not "fix" it here.

4. Do not attempt to repair or backfill existing rows. Historical data stays as is.

Tests (must use the golden fixtures):
- test_chain_rollover_detected_from_real_archives: feed 09:31 then 09:50 totals,
  assert the guard trips
- test_contract_rollover_sets_dvol_nan: 340C 7/31 at 11,534 -> 1,011 must yield NaN,
  not +10,523
- test_normal_intraday_growth_not_flagged: volume increasing must NOT trip the guard
- test_negative_dvol_never_becomes_positive

Report the diff and actual test output.
```

**Verify yourself:**
```bash
python -m pytest tests/ -q                       # green, 8 xfailed
grep -n "abs().fillna" best_value.py             # expect: no hits
```

---

## Task B — IV / data-quality gate 🔴

```
The same captures show implied volatility is unusable early in the session:

  09:31: impliedVolatility = 1.0000000000000003e-05 on the top contract
  09:50: 5 of 30 calls still below 0.001
  09:31: 21 of 30 calls had bid = 0.0 AND ask = 0.0

When bid/ask are both zero, attribution._mid_from_row falls back to lastPrice — which
at 09:31 is the PRIOR session's last trade. So the entry price recorded in the flags
table is not the price at flag time. Every ret_t1h computed against it silently
includes an overnight gap.

Implement:

1. A per-contract quality predicate: usable if (bid > 0 AND ask > 0) AND iv >= 0.01
   AND dte >= 0. Expose it as a single function; do not inline the thresholds in
   multiple modules. Put the thresholds in config.py so they are covered by
   config_hash.

2. Chain-level gate: if more than 20% of the top-30 fails the predicate, abort the
   scan. Write nothing. Log the failure counts. A missing run is honest; a run scored
   on yesterday's prices is not.

3. Never substitute a default for a degraded value. A bad IV means the row is
   excluded from any IV-derived computation, not given a fallback.

4. When IV is degraded chain-wide, do not compute upper_1sd / lower_1sd at all, and
   skip the 1SD strike multiplier entirely rather than applying it against a
   collapsed expected-move band.

Tests using the golden fixtures:
- test_0931_archive_fails_quality_gate (21/30 zero bid/ask, iv 1e-5)
- test_0950_archive_fails_quality_gate
- test_healthy_chain_passes
- test_1sd_multiplier_skipped_when_iv_degraded

Report the diff and test output.
```

---

## Task C — Black-Scholes delta (only after A and B are green) 🟡

```
yfinance does not return greeks. The maintainers closed the request (issue #1465) as
not planned, and the installed version hard-codes the column list. Delta must be
computed.

best_value.py:146-151 currently does:

    if "delta" in work.columns:
        delta_col = work["delta"].fillna(0.5)
    else:
        delta_col = 0.5              # always this branch today
    lev = (delta_col * spot_price) / work["last"]

Because no module produces a delta column, the "leverage" term is permanently
0.5 * spot / price — a constant over price, i.e. a CHEAPNESS ranking. It is 40% of
Value_Score. This is finding F-01 and it is the leading explanation for the measured
result that top-ranked AAPL picks underperformed bottom-ranked ones over T+1h.

Implement:

1. bs_delta(side, spot, strike, dte_days, iv, r) in data_adapter.py (or a new
   greeks.py). Standard Black-Scholes:
       d1 = (ln(S/K) + (r + iv^2/2) * T) / (iv * sqrt(T)),  T = dte/365
       call: N(d1)      put: N(d1) - 1
   Use math.erf for the normal CDF — do not add scipy.

2. Return None — never a default — when: iv < 0.01, dte <= 0, spot <= 0, strike <= 0,
   or any input is NaN. Mirror the discipline already used in
   attribution.fetch_option_mid.

3. Risk-free rate: read from config.py with a documented default (~0.045). Do not
   fetch it per-scan. Note in a comment that delta is insensitive to r at these
   maturities.

4. Emit `delta` from build_best_value_df and fetch_full_chain so it reaches
   calculate_best_value.

5. In calculate_best_value: rows with a null delta must be EXCLUDED from the leverage
   leg and the leg renormalised over the survivors. Do NOT substitute 0.5, a median,
   or any other value. Substituting a default is the bug being fixed — do not replace
   it with a subtler one. Delete the `else: delta_col = 0.5` branch entirely.

## Expected test consequences — do not paper over these

Landing this WILL break two tests in tests/test_best_value_engine.py, by design:

  - test_delta_column_is_never_produced_by_the_pipeline asserts "delta" not in
    df.columns. It must now fail. DELETE this test and say so in the report — its
    purpose was to pin the defect's existence.
  - test_leverage_leg_should_not_be_monotonic_in_price_alone is xfail(strict=True).
    It should now XPASS, which turns CI red. Remove ONLY that xfail marker.

Do not remove, weaken, or skip any other xfail marker. If any OTHER xfail test
changes status, STOP and report which one and why — that would mean this change had
an effect outside its intended scope.

Also add:
- test_delta_matches_known_values: ATM call ~0.5, deep ITM ~1.0, deep OTM ~0.0
- test_put_delta_is_negative
- test_degraded_iv_returns_none
- test_null_delta_row_excluded_from_leverage_not_defaulted

Report: the diff, full test output, and the new config_hash.
```

**After Task C, record the new `config_hash`.** Everything logged before it is engine
v1; everything after is v2. They must be segmented in analysis, never pooled.

---

## Standing constraints (paste with each task)

```
- Do NOT modify attribution.py, mark_runner.py, portfolio_store.py, or journal writes
- Do NOT delete or rewrite rows in the flags table
- Do NOT weaken, skip, or delete an existing test except where a task explicitly
  instructs it
- Do NOT remove an xfail(strict=True) marker except where explicitly instructed
- Report actual command output, not a summary. If you did not run it, say so.
```

---

## After all three land

The engine has changed materially, so the two days already collected are engine v1 and
cannot be pooled with what follows. Before the next collection window:

- [ ] drop QQQM from the ticker list (near-duplicate of QQQ; n=7 and adds no
      independent information)
- [ ] raise the scan interval from 5 min to 15 min (200 runs/day produces heavily
      autocorrelated rows, not 200 observations)
- [ ] confirm `SELECT run_kind, COUNT(*) FROM runs WHERE date(ts_et)=date('now',
      'localtime') GROUP BY run_kind;` returns BOTH kinds after 16:25 ET
- [ ] restart the scheduler so the running process actually holds the new code
- [ ] then collect 15–20 TRADING DAYS before re-running the bucket analysis

Trading days are the sample unit, not rows.
