# Pre-Monday Remediation — Cursor Prompts

Branch: `attribution` @ `8bd33dc`

**Order matters.** Tasks 1–3 must land before the scheduler writes its first Monday row —
every row written without them is permanently degraded and cannot be repaired later.
Tasks 4–6 can land any time this week.

Give Cursor **one task at a time**. Paste the prompt, let it finish, run the verification
yourself, then move on. Do not paste two prompts at once — the agent will conflate them and
report success on the easier one.

---

## Task 1 — Restore mark timestamps 🔴 BLOCKING

- [x] `marked_t1h_at` / `marked_t1d_at` / `marked_exp_at` exist and are populated
- [x] `v_outcomes` exposes elapsed hours per horizon
- [x] Existing 61 rows preserved

```
In attribution.py, the flags table records mark VALUES but not mark TIMES. This makes
every mark's true horizon unknowable, which invalidates the T+1h analysis.

Add three nullable TEXT columns to the flags table: marked_t1h_at, marked_t1d_at,
marked_exp_at. They store ET ISO-8601 timestamps.

Requirements:

1. Use additive ALTER TABLE migration in _ensure_schema, guarded by checking
   PRAGMA table_info(flags) for each column before adding it. Must be idempotent —
   safe to run on a fresh DB and on the existing one. DO NOT drop or recreate the
   flags table. There are 61 live rows that must survive.

2. write_mark() sets the corresponding *_at column in the SAME UPDATE statement as
   the value. Never a second statement — a crash between two statements would leave
   a mark with no timestamp. Keep the existing write-once guard (`AND {col} IS NULL`)
   and the existing `v <= 0.0` rejection exactly as they are.

3. Rebuild the v_outcomes view to add three computed columns:
     hours_t1h = ROUND((julianday(marked_t1h_at) - julianday(ts_et)) * 24, 2)
     hours_t1d, hours_expiry likewise.
   NULL when the mark is unwritten.

4. Add tests to tests/test_attribution.py:
   - test_mark_records_timestamp: write a mark, assert *_at is non-NULL and parses as ET
   - test_mark_value_and_timestamp_are_atomic: assert no code path can set one without
     the other (inspect the SQL, or write a mark and assert both or neither)
   - test_migration_preserves_existing_rows: create a DB with the OLD schema (no *_at
     columns), insert a row, run _ensure_schema, assert the row survives and the new
     columns exist and are NULL
   - test_hours_elapsed_computed: insert a flag at T, mark it at T+3h, assert
     v_outcomes.hours_t1h is approximately 3.0

Do not change any other behaviour. Report the diff and the test output.
```

**Verify yourself:**
```bash
sqlite3 data/attribution.db "PRAGMA table_info(flags);" | grep marked_
sqlite3 data/attribution.db "SELECT COUNT(*) FROM flags;"   # must still be 61
python -m pytest tests/test_attribution.py -q
```

---

## Task 2 — Restore dropped flag-time state 🔴 BLOCKING

- [x] `dte`, `volume`, `open_interest`, `iv` on `flags`, populated by `log_run`

```
The flags table dropped four columns that are frozen-at-flag-time and cannot be
recovered later — Yahoo will not tell me what open interest was last Tuesday.

Add to the flags table: dte INTEGER, volume INTEGER, open_interest INTEGER, iv REAL.
Use the same idempotent ALTER TABLE migration pattern as Task 1. Preserve existing rows.

log_run() must populate all four from the scored DataFrame for both engine rows AND
control rows. Source columns are dte, volume, openInterest, iv — handle the case where
a column is missing by writing NULL, never 0. A zero open interest is meaningful data;
a missing one is not, and they must not be conflated.

Expose dte on v_outcomes so the analysis can segment 0DTE from longer-dated flow.

Add tests:
- test_flag_state_columns_populated: run log_run against a fixture frame, assert all
  four columns are non-NULL and match the input
- test_missing_source_column_writes_null: drop 'iv' from the input frame, assert the
  stored iv is NULL and not 0.0
- test_control_rows_also_carry_state: assert control rows have these columns populated

Report the diff and test output.
```

**Verify yourself:**
```bash
sqlite3 data/attribution.db "SELECT COUNT(*) FROM flags WHERE dte IS NULL;"   # 61 (old rows)
python dailyScaner.py AAPL
sqlite3 data/attribution.db "
  SELECT COUNT(*) new_rows, SUM(dte IS NULL) missing_dte, SUM(iv IS NULL) missing_iv
  FROM flags WHERE date(ts_et)=date('now','localtime');"
# missing_dte and missing_iv must be 0
```

---

## Task 3 — Make the health check window-aware 🔴 BLOCKING

- [x] Overdue check does not fire for rows whose due time falls outside the mark window

```
health_check.py line ~52 flags a row as overdue with a flat
`ts_et < datetime(?, '-4 hours')`. This ignores the fact that mark_runner.py only
runs t1h/t1d marks between 09:30 and 16:15 ET.

Consequence: a contract flagged at 15:30 is due at 16:30, outside the window, and
cannot be marked until 09:30 next morning. It is reported overdue every evening. This
produces a failing alert every single trading day, and I will stop reading the alerts
within a week.

Fix: a row is only overdue if its due time (ts_et + horizon) fell INSIDE a mark window
and at least one full mark_runner pass has elapsed since. Do not simply widen the 4-hour
threshold — that hides real breakage.

Reuse the window definition from mark_runner._in_mark_window; do not duplicate the
09:30/16:15 literals in a second file. If that requires refactoring the window logic
into a shared helper, do that.

Add tests:
- test_late_day_flag_not_reported_overdue: flag at 15:30, evaluate at 18:00, assert 0
- test_genuinely_stale_flag_is_reported: flag at 10:00, evaluate at 15:00 with no mark
  written, assert 1
- test_overnight_flag_becomes_overdue_next_session: flag at 15:30, evaluate at 11:00
  next day with no mark, assert 1

Report the diff and test output.
```

**Verify yourself:**
```bash
python health_check.py               # must pass on current data
python -m pytest tests/ -q -k overdue
```

---

## Task 4 — Land the defect tests

- [ ] `tests/test_best_value_engine.py` present, green, 8 xfail

```
I have a file test_best_value_engine.py (I will paste/attach it). Add it to tests/.

It contains 25 passing characterisation tests and 8 xfail(strict=True) tests that encode
known defects F-01, F-02, F-04, F-05.

Requirements:
- Do NOT fix the defects. Do NOT remove or weaken any xfail marker.
- If a test fails to import or errors because of the config.py refactor, fix ONLY the
  import or the fixture construction. Do not touch assertions, and do not touch
  best_value.py.
- Expected result: 25 passed, 8 xfailed. If anything XPASSes, stop and report which one
  and why — an XPASS means a defect was silently fixed or a test is now wrong, and I
  need to know which.

Report the test output.
```

**Verify yourself:**
```bash
python -m pytest tests/test_best_value_engine.py -q   # 25 passed, 8 xfailed
```

---

## Task 5 — Golden master over the Step 2/3 refactor

- [x] Byte-equality proof that config extraction preserved scoring behaviour

```
Steps 2 and 3 refactored the scoring path in best_value.py (extracted literals into
config.py, added a _multipliers accumulator). Both were intended to be behaviour-
preserving. Nothing proves that, so config_hash ba70b8bb4d6dda86 may fingerprint an
engine I did not intend to measure.

Build a golden-master test:

1. Create tests/golden/chain_aapl.json — a fixed synthetic option chain, ~40 contracts,
   both sides, three expiries, deterministic values. No network, no yfinance.
2. Create tests/golden/scored_expected.json — the output of calculate_best_value on
   that fixture at a FIXED now_et and FIXED spot, with all bias/state parameters pinned.
3. tests/test_golden_master.py asserts the current engine reproduces it exactly
   (rel=1e-9 on floats, exact on strings and ranks).

Then: check out the pre-refactor commit of best_value.py into a temp path, run it
against the same fixture, and compare. Report whether the outputs match.

If they DIFFER, do not fix anything — report exactly which contracts and which fields
differ, and stop. That is a finding, not a bug to paper over.
```

**Verify yourself:**
```bash
python -m pytest tests/test_golden_master.py -q
```
If it reports a divergence, tell me before doing anything else.

---

## Task 6 — Naive datetimes in the scan path (F-17)

- [x] No naive `datetime.now()` / `date.today()` in scan, archive, or snapshot paths

```
F-17: naive datetime.now() and date.today() appear in dailyScaner.py:531,
weekly.py:454/458, snapshot_store.py:59/99, data_adapter.py:57. These resolve to
local machine time, not ET. A machine in UTC, or a run after 20:00 ET, shifts DTE
by one day and mislabels archive timestamps.

Replace every one with the ET-aware helper already in attribution.py (now_et()).
Do not introduce a second time helper — import the existing one, or move it to a
shared module if that creates a circular import.

app.py date_input defaults are user-facing UI, leave them alone.

Add a test that greps the scan/archive/snapshot modules for naive datetime.now() and
date.today() and fails if any remain, so ruff's DTZ rule has a backstop.

Report the diff and confirm dte values are unchanged for a same-day run.
```

**Verify yourself:**
```bash
grep -n "datetime.now()\|date.today()" dailyScaner.py weekly.py snapshot_store.py data_adapter.py
# expect: no hits
```

---

## Standing instructions for every Cursor session on this branch

Prepend to any prompt where the agent might wander:

```
Constraints for this task:

- Do NOT modify scoring math in best_value.py. If you believe a change there is
  required, stop and explain why instead of doing it.
- Do NOT add new signal modules or multipliers.
- Do NOT delete or rewrite rows in the flags table. Migrations must be additive
  ALTER TABLE only.
- Do NOT weaken an existing test to make it pass. If a test fails, either fix the
  code or report that the test encodes a wrong expectation — do not edit the assertion.
- Do NOT remove an xfail(strict=True) marker.
- Report the actual command output, not a summary of it. If you did not run a
  command, say so explicitly.
```

---

## Final gate — before the scheduler runs Monday

- [ ] Tasks 1–3 merged, suite green
- [ ] `sqlite3 data/attribution.db "SELECT COUNT(*) FROM flags;"` still ≥ 61
- [ ] One live scan writes rows with all new columns populated
- [ ] `python health_check.py` passes at 18:00 ET, not just midday
- [ ] Run `VERIFY_PROMPT.md` in a **fresh** Claude Code session

That last one matters most now. `attribution.py` was re-implemented from spec rather than
from the reviewed file, and it dropped four columns and three timestamps in the process —
without that being flagged in the build log. The self-report was clean; the schema was not.
A fresh adversarial session is the cheapest way to find what else went quiet.
