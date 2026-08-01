# Cursor Task — Detect stale volume by comparing against the prior EOD archive

**Scope:** `chain_quality.py` (or wherever `contract_is_usable` lives), plus the scan
path that builds `dVol`. Do not touch attribution, mark_runner, portfolio_store,
greeks, scoring math, or UI.

---

## Background

Task A added a **decrease detector**: if a contract's volume drops between consecutive
scans, it rolled over to a new session and `dVol` is set to NaN.

That catches contracts that have *already* rolled. It misses the opposite case — a
contract that has **not yet rolled** and is still serving the prior session's
cumulative volume. Proven in `tests/golden/`:

```
contract          7/27 EOD    7/28 09:31   7/28 09:50
340C  2026-07-29    35,033      36,654       36,654    <- stale at 09:50, never decreased
337.5C 2026-07-29   26,944      28,317       28,317    <- stale, never decreased
345C  2026-07-29    18,768      19,694       19,694    <- stale, never decreased
340C  2026-07-31    11,216      11,534        1,011    <- rolled; decrease detector catches this one
```

Three of four never decrease, so the existing guard never fires on them. They carry a
large fake volume straight into the flow leg, which is 60% of Value_Score.

Root cause (confirmed against a broker quote): Yahoo caches per contract and only
refreshes when that contract next trades. A broker showing the same contract displays
`Volume 0, High 0.00, Low 0.00` — the counter is properly reset. Yahoo serves the last
known cumulative value instead. Untraded or thinly traded contracts can hold the prior
session's number well past the open.

---

## Prompt

```
Add a second staleness detector that compares each contract's current volume against
the PRIOR TRADING DAY'S converged EOD archive.

## Rule

A contract's volume is STALE if, early in the session:

    today_volume >= prior_eod_volume * STALE_VOLUME_RATIO     (default 0.95)

Rationale: today's genuine cumulative volume, in the first part of a session, is almost
never >= 95% of ALL of yesterday's. When it is, the feed has not refreshed that
contract.

Do NOT use exact equality. Late prints keep clearing after the close, so today's cached
value is often slightly HIGHER than the EOD reading (35,033 -> 36,654 above). An
equality test produces a false negative on essentially every contract.

## Critical: write NaN, never 0

A stale contract's dVol and volume-derived inputs must be set to NaN and the row
flagged `stale_volume = True`.

Do NOT write 0. Zero means "this contract did not trade", which is a claim you cannot
support — the contract may have traded and the feed simply has not caught up. Writing 0
is a default masquerading as a measurement, and it flows into vol/OI as if it were
observed. This is the same class of defect as the old `delta = 0.5` fallback and the
`dVol.fillna(1.0)` substitution. Excluded, not defaulted.

## Reference data

The prior EOD archive is already produced by the 16:20 EOD run (`run_kind='eod'`,
`is_eod: true` in the payload). Read it from disk. Do NOT add a network call.

Guard conditions — all three must hold before the check runs:

  1. A prior-trading-day EOD archive exists for this ticker
  2. Its payload has `settlement_converged == true`. If convergence failed, the EOD
     numbers were not settled either and are unusable as a reference — skip the check.
  3. The archive is from the most recent PRIOR trading day, not an older one. A
     stale reference is worse than none. Compare ET session dates, and use a market
     calendar or at minimum a weekday check — Monday's reference is Friday's EOD.

If any guard fails: skip this check, log at WARNING that the EOD reference was
unavailable and why, and fall back to the decrease detector alone. Do NOT silently
treat every contract as fresh.

## Applicability window

Only apply while staleness is plausible. Once a contract has genuinely traded a full
session, exceeding 95% of yesterday's volume is normal and not suspicious.

Apply the check only when now_et is before a configurable cutoff (default 11:00 ET),
OR — better, if straightforward — until the contract has been observed to decrease at
least once this session, at which point it is known to have rolled and this check is no
longer needed for it. Implement whichever you can do cleanly; state which you chose and
why.

## Config

Add to config.py so these are covered by config_hash:

    "stale_volume_ratio":       0.95,
    "stale_check_cutoff_et":    "11:00",

## Interaction with the existing guard

Both detectors must coexist and both set the same NaN + suspect state:
  - decrease detector -> contract has already rolled
  - EOD-match detector -> contract has not yet rolled

Use a single shared flag so downstream code does not need to know which fired. Log
counts of each separately so the health check can report them.

## Chain-level escalation

If more than 50% of the top-N contracts are flagged stale by either detector, abort the
scan entirely: write no archive, write no attribution rows, log the reason with both
counts. A chain that is majority stale cannot produce a meaningful flow score.

## Tests — use the real golden fixtures, not synthetic data

- test_stale_contract_detected_from_eod_reference: 340C 7/29 at 36,654 against an EOD
  reference of 35,033 must be flagged stale
- test_rolled_contract_not_flagged_by_eod_check: 340C 7/31 at 1,011 against 11,216 must
  NOT be flagged by this detector (the decrease detector owns that case)
- test_stale_volume_is_nan_not_zero: assert the resulting value is NaN and explicitly
  assert it is NOT 0 or 0.0
- test_missing_eod_archive_skips_check_and_warns: no reference -> check skipped, warning
  logged, contracts NOT marked stale, decrease detector still active
- test_unconverged_eod_archive_is_not_used_as_reference
- test_stale_ratio_boundary: 0.94 ratio passes, 0.96 flagged
- test_check_not_applied_after_cutoff: same numbers at 14:00 ET must not be flagged
- test_majority_stale_chain_aborts_scan
- test_friday_eod_used_as_monday_reference

## Constraints

- Do NOT modify attribution.py, mark_runner.py, portfolio_store.py, greeks.py, or UI
- Do NOT change scoring math beyond consuming the new flag
- Do NOT write 0 for a stale value anywhere
- Do NOT remove or weaken any xfail(strict=True). Four should remain: F-02, F-05,
  the [0,1] range defect, and the golden pin. If any changes status, STOP and report.
- Regenerate the golden master if scores change; report the new config_hash
- Report actual command output, not a summary
```

---

## Verify yourself

```bash
python -m pytest tests/ -q          # green, 4 xfailed
```

Then, tomorrow morning after a live scan:

```sql
-- how many contracts got flagged, and by which detector
SELECT date(ts_et), COUNT(*) FROM flags
WHERE date(ts_et) = date('now','localtime');
```

and check the scan log for the stale counts. On a 09:31 scan you should expect a
**large** number flagged — most of the chain. That is the correct result, not a bug.

---

## Prerequisite

This depends on the EOD run actually producing archives. As of now `run_kind='eod'` has
never executed — yesterday's last scan logged as `intraday` at 16:14:57 because the
scheduler process predated the EOD commit.

Before this guard can work:

1. Restart the scheduler so it holds the current code
2. After 16:25 ET confirm:
   `SELECT run_kind, COUNT(*) FROM runs WHERE date(ts_et)=date('now','localtime') GROUP BY run_kind;`
   must return **both** `intraday` and `eod`
3. Confirm an archive on disk with `"is_eod": true` and `"settlement_converged": true`

Until step 3 holds, this detector will log "EOD reference unavailable" every morning and
silently do nothing — which is the correct fallback, but means the guard is not
actually protecting you.
