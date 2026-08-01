# Cursor Task — Source-aware archive comparison + page cap

Branch: `cursor/sources-steps`
**Scope:** `chain_quality.py`, archive write path in `dailyScaner.py`, `sources/massive.py`.
Do NOT touch scoring math, attribution schema, greeks, portfolio_store, or UI.

---

## Observed failure

```
$ python dailyScaner.py AAPL --source massive
  Comparing to → archive/AAPL_20260729_195851.json
ABORT: chain volume rollover detected (same ET session 2026-07-29).
  prev call/put=944,496/789,904   curr call/put=284,582/326,120
  No archive, no attribution.
```

Nothing rolled over. The previous archive was written by the **yahoo** source; this run
used **massive**. The two feeds report materially different volume for the same session
(~3x), so the decrease detector fired on a source change.

Also observed earlier:
```
Massive chain pagination hit 5-page cap for AAPL
```
So Massive's chain — and therefore its session volume totals — may be truncated.

---

## Prompt

```
Two defects to fix. They are related: the second may be part of the cause of the first.

## Defect 1 — the rollover detector is source-blind

chain_quality's chain-level rollover check compares the current run's total call/put
volume against the previous archive on disk, without checking which data source wrote
that archive. Switching sources therefore looks identical to a session rollover, and the
scan aborts.

Fix:

1. Add `source` to the archive JSON payload — the `name` attribute of the
   MarketDataSource that produced it ("yahoo", "massive", "fixture").

2. The chain-level rollover check must only compare against a previous archive whose
   `source` matches the current source. On mismatch: SKIP the check, log at WARNING
   ("previous archive written by source X, current source is Y — rollover check
   skipped"), and continue the scan. Do not abort.

3. Same rule for the per-contract decrease detector and for the EOD-match stale
   detector: both are only valid within a single source's volume semantics. A
   cross-source comparison is meaningless, not suspicious.

4. Backward compatibility: archives written before this change have no `source` key.
   Treat a missing key as "yahoo" (that is what wrote them) rather than as unknown, and
   note that assumption in a comment.

5. Also require the same ET session date for these comparisons — if it already does
   this, leave it; if not, add it.

## Defect 2 — Massive chain pagination cap

sources/massive.py caps pagination at 5 pages and logs when the cap is hit. It was hit
for AAPL, which means the returned chain is truncated and any session volume total
computed from it is understated.

Fix:

1. Raise the cap to a value that can actually cover a filtered chain. Put it in
   config.py as `massive_max_pages` (suggest 20) so it is visible and tunable.

2. Tighten the server-side filter so fewer pages are needed. `expiration_date.lte` is
   already applied; also send `expiration_date.gte` = today (ET) so expired contracts
   are excluded, and keep `limit=250`.

3. When the cap IS hit, do not silently return a partial chain. Either:
     (a) mark the result as truncated and have the scan abort with a clear message, or
     (b) log ERROR and set a `chain_truncated: true` flag in the archive payload.
   Choose (a) if it is straightforward — a truncated chain produces wrong volume totals
   and wrong top-30 rankings, and silently scoring on it is worse than not scoring.
   State which you chose and why.

## Diagnostic to run and REPORT (this is the deliverable)

After raising the cap, report for AAPL:

  - total contracts returned by massive, before vs after the cap change
  - total call volume and total put volume, massive vs the yahoo archive for the SAME
    session (archive/AAPL_20260729_195851.json: call=944,496 put=789,904)
  - the top-30-by-volume contract sets from each source, and how many overlap

The overlap number matters most. If both sources pick largely the same top contracts,
the engine would score a similar universe and the migration is low risk. If they pick
different contracts, that is a much larger finding than any per-field difference.

Do NOT recalibrate any threshold (min_volume, vol/OI cutoffs, MIN_OI_FOR_MAGNET) based
on this. Just report the numbers.

## Tests

- test_rollover_check_skipped_on_source_mismatch: prev archive source="yahoo", current
  source="massive", volume lower → check skipped, scan continues, warning logged
- test_rollover_check_applied_when_source_matches: same source, volume drops → still
  aborts
- test_missing_source_key_treated_as_yahoo
- test_decrease_detector_skipped_across_sources
- test_eod_stale_detector_skipped_across_sources
- test_archive_payload_includes_source
- test_page_cap_hit_does_not_return_partial_chain_silently
- test_expiration_date_gte_sent_server_side

## Constraints

- Do NOT modify scoring math, attribution schema, greeks.py, portfolio_store, or UI
- Do NOT change any threshold value in config.py other than adding massive_max_pages
- Do NOT substitute a default for missing data anywhere
- Do NOT delete chain_quality.py or either rollover detector
- Do NOT weaken, skip, or delete an existing test. Four xfail(strict=True) must remain:
  F-02, F-05, the [0,1] range defect, and the golden pin. If any changes status, STOP.
- Report the config_hash before and after (expect one new key), and the actual
  diagnostic output
```

---

## Verify yourself

```bash
python -m pytest tests/ -q                      # green, 4 xfailed
python dailyScaner.py AAPL --source massive     # must NOT abort on source mismatch
python dailyScaner.py AAPL --source yahoo       # must still work
git log --oneline -1                            # confirm it committed
```

---

## Note on the volume gap

The ~3x difference (Yahoo 944k calls vs Massive 285k) is worth understanding before you
change any threshold. Two candidate explanations:

**Trade qualification.** Massive's aggregates are documented as derived only from
trades meeting specific OPRA conditions — multi-leg spread legs and certain condition
codes are excluded. Yahoo's figure likely includes everything.

**Truncation.** The 5-page cap. Testable, and this task tests it.

If raising the cap closes the gap, it was truncation. If the gap persists at ~3x, it is
trade qualification — and that is a meaningful semantic difference, not an error.
Arguably Massive's filtered number is a *better* signal for your thesis, since spread
legs are hedges rather than directional bets. But every threshold in config.py was
calibrated against the unfiltered Yahoo number, so switching sources means those
thresholds are no longer tuned for the data.

Do not adjust them yet. Get the numbers, then decide deliberately — and remember that
any threshold change moves `config_hash` and forks your sample.
