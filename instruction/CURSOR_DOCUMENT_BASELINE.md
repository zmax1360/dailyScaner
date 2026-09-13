# Cursor Task Pack — Document current behavior before changing it

**Run these as FOUR separate sessions.** Paste one section per session. Pasting
more than one causes the agent to conflate them, do the easiest properly, and
report all of them as done.

---

## STANDING CONSTRAINTS — paste at the top of every session in this pack

```
You are documenting an existing system. You are NOT improving it.

ABSOLUTE RULES:

1. Do not modify, create, delete, move, or rename any file outside `docs/`.
   The only exception is `docs/` itself and files explicitly named in this task.

2. Do not change ANY .py file. Not formatting, not imports, not type hints,
   not docstrings, not dead-code removal, not lint fixes, not renames.
   If a linter or formatter runs automatically, disable it for this session.

3. `config.SCORING` must not change. Print `config_hash(SCORING)` at the start
   and at the end of the session and include both in your final report.
   If they differ, you broke rule 2 — stop and report it.

4. Do not fix bugs you find. Do not fix failing tests. Do not "clean up"
   anything. Log every defect you find to `docs/findings.md` with file:line
   and move on. Finding bugs is expected and valuable; fixing them is
   out of scope and will invalidate the baseline.

5. EVIDENCE RULE: every factual claim in a doc must carry a `file.py:LINE`
   reference. If you cannot point at a line, you may not state it as fact.
   Write it under a heading `## Unverified` instead, phrased as a question.

6. HONESTY RULE: you will not be able to read every line of large files.
   `app.py` is ~228KB, `eod_report.py` ~82KB, `dailyScaner.py` ~59KB.
   Every doc must end with a section `## Coverage` stating, per file:
   - which line ranges you actually read
   - which you skimmed by grep/symbol only
   - which you did not open at all
   An incomplete doc that says so is correct. A complete-looking doc that
   guessed is a failure and worse than nothing.

7. Do not summarize intent. Document behavior. If a function's docstring says
   one thing and the code does another, document the code and log the
   discrepancy in `docs/findings.md`.

FINAL REPORT (every session must end with this):
- `git status --porcelain` output, verbatim
- `git diff --stat` output, verbatim
- config_hash before / after
- confirmation that no .py file was modified
```

---

## SESSION 1 — Baseline test results

**Do this first. Nothing else in this pack is meaningful without it.**

```
Capture the current test baseline. Do not fix anything.

1. Record the exact environment:
   python --version, pip freeze, git rev-parse HEAD, git status --porcelain

2. Run the full suite and capture output VERBATIM:
   pytest -v --tb=short 2>&1 | tee docs/baseline_tests.txt

   If the suite takes too long or hangs, run it per-directory and note which
   invocation you used. Do not use -x. Do not deselect tests. Do not set
   markers to skip anything.

3. Run it a second time and diff the pass/fail sets. Flaky tests are a
   finding — record them.

4. Write `docs/baseline_tests.md` containing:
   - environment block from step 1
   - counts: passed / failed / errored / skipped / xfailed / xpassed
   - a table of every FAILING test: nodeid, one-line reason, file:line of
     the assertion that failed
   - a table of every SKIPPED and XFAIL test: nodeid, the skip reason string,
     and whether the reason is conditional (e.g. missing API key) or
     unconditional. Unconditional skips are dead tests — flag them.
   - any XPASS results (a test marked expected-fail that now passes means
     either the bug was fixed silently or the marker is stale)
   - flaky tests from step 3
   - total wall-clock runtime
   - a `## Coverage` section noting any tests you could not run and why

5. Do NOT fix a single failing test. Do NOT delete or mark any test.

Tests and fixtures to pay specific attention to, because they encode
invariants that later work must not break:
  tests/test_golden_master.py
  tests/test_import_graph.py
  tests/test_best_value_engine.py
  tests/test_attribution.py
  tests/test_dailyScaner_regressions.py
  tests/scoring_fixtures.py
  tests/test_sources_fixture.py

For each of those seven, add a short paragraph to `docs/baseline_tests.md`
stating what invariant it is protecting, with file:line.
```

---

## SESSION 2 — `docs/scoring_spec.md`

**The most important deliverable. It must be numerically verified, not prose.**

```
Document the scoring pipeline exactly as implemented, including its defects.

FILES IN SCOPE (read these; do not modify them):
  best_value.py, config.py, scoring_pool.py, greeks.py,
  strategy_engine.py, zero_dte_gex.py, pov_leakage.py, cost_distribution.py

Write `docs/scoring_spec.md` with these sections:

1. ENTRY POINTS
   Signature and caller of `calculate_best_value` and `build_best_value_df`,
   with file:line. Who calls each, from where.

2. UNIVERSE FILTERING
   Every condition that removes a contract before scoring, in execution order,
   with file:line and the config key that controls it. Include expiry drops
   and the 16:15 ET 0DTE rule.

3. FEATURE COMPUTATION
   For `delta`, `_lev`, `_flow`: the exact expression as written in code, with
   file:line. State the NaN policy for each — what produces NaN, whether NaN
   is filled, and whether a NaN row is excluded from ranking. Be explicit
   about `dVol` NaN handling and stale_volume interaction.

4. POOLING
   How `pool` is assigned, what happens to NULL/invalid dte, what
   `min_pool_size` does when a pool is short, and confirm whether an
   under-size pool's rows get a score at all.

5. NORMALISATION
   The `_minmax` implementation verbatim. State explicitly:
   - what it returns when max == min
   - whether the reference is per-scan, rolling, or absolute
   - what happens when every value in the pool is small
   This section must be neutral description, not evaluation.

6. BASE SCORE
   The weighted blend with weights sourced from config, file:line.

7. MULTIPLIER STACK
   A table with one row per `_apply(...)` call site: condition, mask
   expression, config key, value, file:line. Order of application matters —
   list them in execution order. State whether any two can apply to the same
   row and whether the result is a product.

8. NUMERICAL VERIFICATION  ← this is the part that makes the doc trustworthy
   Open `data/attribution.db` READ-ONLY. Do not write to it. Do not use a
   GUI. Use `sqlite3` with `file:...?mode=ro` or a read-only connection.

   Pick 5 rows from `flags` where score, base_score, nlev, nflow and
   multipliers are all non-null, taken from config_hash 243ecda68cfc8618.

   For each row, show:
     a) 0.4*nlev + 0.6*nflow  vs stored base_score       — must match
     b) base_score * product(multipliers values)  vs stored score  — must match
   Present as a table with the residuals. Any row that does not reconcile is
   a P0 finding — log it to docs/findings.md with the flag_id.

   Also verify: does the `multipliers` JSON always contain the `_base: 1.0`
   sentinel, and does the product include it?

9. RANK ASSIGNMENT
   How rank is computed, whether it is within-pool, how ties break, and
   whether controls (`is_control`) are included in the ranking.

10. DISPLAY-ONLY LAYERS
    Anything that affects what is shown but not what is scored — the delta
    band filter, provider delta/theta columns, time-stop display. For each,
    prove it is display-only by pointing at the line where the filtered frame
    diverges from the frame passed to attribution. If you cannot prove it,
    say so under `## Unverified`. Do not assume.

11. `## Findings` — anything surprising, cross-referenced to docs/findings.md
12. `## Coverage` — per the standing constraints

CONSTRAINT: the spec describes what the code does TODAY, defects included.
Do not describe intended behavior. Do not recommend changes. Do not use the
words "should" or "ideally" anywhere in this document.
```

---

## SESSION 3 — `docs/data_contract.md`

```
Document every persistence boundary and the contract across it.

FILES IN SCOPE:
  attribution.py, scanner/attribution.py, snapshot_store.py,
  best_value_archive.py, portfolio_store.py, scanner/journal_io.py,
  scanner/lot_match.py, dailyScaner.py (archive write path only),
  data_adapter.py, sources/base.py, sources/yahoo.py, sources/massive.py,
  sources/fixture.py

Write `docs/data_contract.md`:

1. INVENTORY
   Table of every persisted artifact: path/pattern, format, writer module,
   reader modules, write frequency, whether it is in .gitignore. Cover
   archive/*.json, archive_weekly/*.json, data/attribution.db,
   data/best_value_archive.csv, data/journal/*.json, snapshot store files,
   scheduler.pid, logs/.

2. ARCHIVE JSON
   The full top-level key structure actually written, from the code that
   writes it (file:line), not from an example file. Then cross-check against
   one real file in archive/ and note any key present in one but not the
   other. Mark which keys each consumer reads (app.py, telegram_bot.py).

3. SQLITE SCHEMA
   For `runs` and `flags`: reproduce the CREATE TABLE from _SCHEMA verbatim,
   then list the columns added by _FLAG_MIGRATE_COLS / _RUN_MIGRATE_COLS
   separately, since those are absent from the base DDL. Then run
   `PRAGMA table_info` READ-ONLY against the live DB and produce a
   three-way comparison: base DDL / migration list / live database.
   Any column in the live DB that appears in neither list, or any column in
   the lists missing from the live DB, is a P0 finding.

4. VIEW `v_outcomes`
   Every derived column with its exact expression. For each return column
   state the entry basis and the exit basis (ask/bid/mid) — this differs
   between short and long horizons and is the single most misread thing in
   this codebase.

5. MARK SEMANTICS
   From mark_runner.py: horizon order, the mark window bounds, what each
   horizon's `method` values can be, the sealing rules, and
   UNMARKABLE_BEFORE. State which horizons store bid and which store mid.

6. TIME AND TIMEZONE CONTRACT
   How ts_et is written, its exact string format, and the rule against
   SQLite date() on it. Point at session_date_sql / et_clock_sql. List every
   place in the codebase that groups by day and state which method each uses.
   Any site using raw date()/substr inconsistently is a finding.

7. VERSIONING CONTRACT
   config_hash: how computed, what is included, what is NOT included.
   engine_sha: where it is supposed to be set. Run a READ-ONLY query for
   COUNT(*) vs COUNT(engine_sha) on runs and report it.

8. JOURNAL
   Current storage, the FIFO matching contract, and which module is the
   single source of truth for realized P&L. Note any second code path that
   also computes P&L.

9. `## Coverage`

Do not modify the database. Read-only connections only. If any step would
require a write, skip it and say so.
```

---

## SESSION 4 — `docs/architecture.md` + roadmap

```
FILES IN SCOPE:
  dailyScaner.py, scheduler.py, app.py, telegram_bot.py, notify_delivery.py,
  mark_runner.py, health_check.py, eod_settlement.py, eod_report.py,
  weekly.py, chain_quality.py, spread_gate.py, volume_analysis.py,
  news_service.py, data_adapter.py, plist/, nightly.sh, run_scanner.sh

`app.py` is ~228KB. Do NOT attempt to read it linearly. Build its section by
grepping for function/class definitions and Streamlit tab/zone markers, then
read only the functions that touch scoring, attribution, or the ranked table.
State in `## Coverage` exactly which app.py line ranges you read.

Write `docs/architecture.md`:

1. PROCESS MODEL
   Every long-running or scheduled process: what starts it (launchd plist,
   shell script, Streamlit), its cadence, its entry point, its exit codes,
   where it logs. Read the plist files for actual intervals — do not infer.

2. CONTROL FLOW
   The scan path end to end, as a numbered sequence with file:line at each
   hop: trigger -> subprocess -> fetch -> gates -> score -> archive write ->
   attribution write -> notify decision. State exactly which exit codes gate
   the notify step and where that check lives.

3. ABORT PATHS
   Every condition that aborts a scan, its threshold, its config key, the
   log string it emits, and whether it causes a non-zero exit. This is the
   safety surface — be exhaustive.

4. LAYER BOUNDARIES
   Which modules import network libraries. Verify the claim that app.py never
   imports yfinance by grepping, and point at the test that enforces it.
   List any violation.

5. UI SURFACE
   app.py tabs/zones and which module backs each. For the ranked-table zone
   specifically, trace the exact dataframe path from build_best_value_df to
   what is rendered, noting every filter applied in between.

6. DELIVERY
   telegram_bot.py: what it sends, whether it reads the archive or recomputes,
   and where that decision is made. This has been a defect before — verify it
   at the line, do not assume.

7. REPORTING
   eod_report.py section execution order with line refs, and what each
   section gates on. Note the coverage gate that can halt the report.

8. `## Coverage`

Then write `docs/roadmap.md`:
- Copy the Review Gate statement verbatim at the top.
- List the four deliverables and their completion status.
- A `## Blocked until documented` list: any change anyone has proposed to
  this system, left unstarted.
- Do not propose solutions. Do not prioritize. This roadmap is a gate record,
  not a plan.

Finally, append to `docs/findings.md` a consolidated table of every defect
found across all four sessions: id, file:line, one-line description,
severity (P0 breaks a documented invariant / P1 wrong behavior / P2 dead or
inconsistent code). No fixes. No recommendations.
```

---

## Verification to run yourself after all four sessions

```bash
git status --porcelain          # expect ONLY docs/ additions
git diff --stat -- '*.py'       # expect EMPTY
python -c "from config import SCORING; from <hash_module> import config_hash; print(config_hash(SCORING))"
                                # expect 243ecda68cfc8618
grep -rn "should\|ideally\|recommend" docs/scoring_spec.md   # expect nothing
```

If `git diff -- '*.py'` is non-empty, the baseline is void. Revert and re-run
the affected session with the standing constraints pasted again.
