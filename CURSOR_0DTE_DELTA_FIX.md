# Cursor Task — Fix 0DTE delta unit mismatch

Commit under review: `268fd62`

**Scope:** `best_value.py::_t_days` (and any equivalent in `data_adapter.py`).
Do not change `greeks.bs_delta` — it is correct. Do not touch attribution, mark_runner,
portfolio_store, or the UI.

---

```
There is a unit mismatch in the 0DTE delta path introduced in 268fd62.

best_value.py::_t_days returns TWO DIFFERENT UNITS depending on which branch it takes:

    if d == d and d > 0:
        return d                                    # <- calendar DAYS (e.g. 28)
    ...
    secs = max((close_et - now_et).total_seconds(), 60.0)
    return secs / (365.25 * 24.0 * 3600.0)          # <- YEAR FRACTION

greeks.bs_delta then does `T = t_days / 365.0`. That is correct for the first branch
and wrong for the second: a year fraction gets divided by 365 a SECOND time.

Measured effect at spot 340, iv 0.30, 6 hours to the close:

    passed as coded: 0.000684  ->  T = 1.875e-06 years  (~59 seconds of time left)
    correct:         0.25      ->  T = 6.849e-04 years  (6 hours)

    strike    as coded      correct
     330      1.0           0.9999
     338      1.0           0.7762
     341      4.39e-13      0.3571
     344      0.0           0.0692

sigma*sqrt(T) collapses, so delta becomes a STEP FUNCTION: 1.0 for anything ITM,
0.0 for anything OTM. A strike one dollar OTM gets delta ~0 instead of ~0.36.

Since lev = delta * spot / price, every OTM 0DTE contract gets a leverage of zero and
drops out of the leg. Every ITM one gets maximum leverage. This defeats the purpose of
the delta fix specifically for 0DTE, which is a large share of the flagged universe.

## Required change

1. Make _t_days return DAYS in BOTH branches, matching the function name, the dte>0
   branch, and bs_delta's documented contract:

       return max((close_et - now_et).total_seconds(), 60.0) / 86400.0

   Six hours -> 0.25 days -> T = 0.25/365 = 6.85e-4 years. Correct.

2. Fix the docstring — it currently says "Year-fraction input for BS", which is what
   caused this. It should say the return is in DAYS and that bs_delta converts to
   years.

3. Audit data_adapter.py (around line 116) and best_value.py (around line 495) for the
   same pattern. Both also call bs_delta. Report what units each one passes and fix any
   that are inconsistent. Do NOT assume they are correct because they look different —
   check what value actually reaches bs_delta.

4. If more than one place computes a time-to-expiry, extract ONE shared helper and have
   all call sites use it. Three independent implementations of the same conversion is
   how this bug happened.

## Tests

Add to tests/test_best_value_engine.py (or a new tests/test_greeks.py):

- test_0dte_delta_is_not_a_step_function: at 6 hours to close, spot 340, iv 0.30, an
  ATM-ish CALL at strike 341 must have delta between 0.25 and 0.55. Assert it is NOT
  within 1e-6 of 0.0 or 1.0. This FAILS against current code.
- test_0dte_and_1dte_delta_are_continuous: an ATM contract at 6 hours to close and the
  same contract at dte=1 must both produce deltas in (0.2, 0.8). There must not be an
  order-of-magnitude discontinuity between the intraday path and the calendar path.
- test_t_days_returns_days_in_both_branches: call _t_days directly with dte=28 and with
  a 0DTE row 6 hours before close; assert 28.0 and approximately 0.25 respectively.
- test_delta_monotonic_across_strikes_0dte: for a 0DTE chain at 6 hours out, delta must
  decrease monotonically as call strikes increase, with at least 4 DISTINCT values
  across 5 strikes spanning +/- 3% of spot. A step function produces only two.

The last one is the important one — it cannot be satisfied by a step function no matter
how the thresholds are tuned.

## Constraints

- Do NOT modify greeks.bs_delta — the mathematics there is correct
- Do NOT modify attribution.py, mark_runner.py, portfolio_store.py, or UI code
- Do NOT remove or weaken any existing xfail(strict=True) marker. There are currently
  6 in test_best_value_engine.py and 1 in test_golden_master.py. If any changes status,
  STOP and report which one.
- The golden master will need regenerating since 0DTE deltas change. Regenerate it, and
  report the new config_hash.
- Report actual command output, not a summary.
```

---

## Verify yourself

```bash
python -m pytest tests/ -q                 # expect green, 7 xfailed (6 + golden)
python -c "
from greeks import bs_delta
# 6 hours to close, expressed in DAYS
for k in (330.0, 338.0, 341.0, 344.0, 352.0):
    print(k, bs_delta('CALL', 340.0, k, 0.25, 0.30))
"
```

Expect a smooth curve — roughly 0.9999 / 0.776 / 0.357 / 0.069 / 0.000005.
If you see only `1.0` and `0.0`, the fix did not take.

---

## Notes

**This does not affect historical data.** Before `268fd62`, 0DTE contracts returned
`None` from `bs_delta` (`dte <= 0`) and were excluded from the leverage leg entirely. So
no already-logged row carries a wrong delta. This only matters going forward — which is
why it is worth catching before the 15-day collection window starts.

**`config_hash` will change again.** You are still pre-collection, so this is free. Once
you start the real 15–20 day window, every engine change forks the sample and the old
rows can no longer be pooled with the new ones. Get the engine settled first, then start
the clock.

**Watch the last 15 minutes of the session.** As time-to-close approaches zero, delta
legitimately does approach a step function — that is real mathematics, not a bug. But it
means late-day 0DTE leverage becomes binary and the `max(..., 60.0)` floor puts a hard
bottom on T. Worth confirming the behaviour between 15:45 and 16:00 is what you want
rather than an artifact, and worth considering whether 0DTE contracts should simply be
excluded from the leverage leg in the final 30 minutes.
