# Tree cleanup report

**Generated:** 2026-09-12. Diagnosis only. No working-tree mutation in the diagnosing session.

---

## DECISION POINT — do not `git checkout HEAD` the deleted modules

`script/pre_trade_check.py` and `script/portfolio_store.py` are **not identical** to `HEAD`. A plain restore (`git checkout HEAD -- pre_trade_check.py portfolio_store.py` or `git restore`) **discards working-tree features that `app.py` and the modified tests already call.**

| file | HEAD (`git show HEAD:…`) | `script/` copy | identical? |
|---|---|---|---|
| `pre_trade_check.py` | 1703 lines | 2105 lines (mtime 2026-08-28 21:49) | **NO** — unified diff **638** lines |
| `portfolio_store.py` | 665 lines | 680 lines (mtime 2026-08-26 22:50) | **NO** — unified diff **39** lines (comments / docstrings only) |

### `pre_trade_check.py` — merge required

`script/` adds (not in HEAD):

- `from time_stop import hold_hours_for_dte` (top-level). Importing the script copy **requires** root `time_stop.py` (currently untracked).
- Paths moved `data/journal/pre_trade_*.json` → `data/pretrade/` (matches already-unstaged `.gitignore` `+data/pretrade/` and `tests/test_pre_trade_check.py::test_pretrade_paths_are_outside_journal_dir`).
- Archive → Pre-Trade API used by current `app.py`: `ARCHIVE_PREFILL_KEY`, `archive_greeks_refusal`, `archive_prefill_from_row`, `stage_archive_prefill`, `consume_archive_prefill`, `archive_quote_marker`, `pattern_candidates`, `PATTERN_*`.
- `_usable_quote_pair`, `hold_hours` on prefill keys, pattern-selector constants.

HEAD still has `DELTA_MIN` / `DELTA_MAX` / `hold_hours` on `PreTradeInputs`. The gap is the archive-prefill / pattern / path / `time_stop` layer.

If HEAD is restored and the current `app.py` is kept: Streamlit imports succeed, then **runtime `AttributeError`** on `pre_trade_check.ARCHIVE_PREFILL_KEY` / `archive_greeks_refusal` / `stage_archive_prefill`. Fourteen `tests/test_pretrade_bridge.py` tests that call `ptc.archive_*` fail. `test_pretrade_paths_are_outside_journal_dir` fails (HEAD paths still contain `/data/journal/`).

`fix_tree.sh` commit 1 therefore **copies `script/` → repo root**, not `HEAD`. That is a merge choice: keep the newer scratch copies. Review the 638-line diff before arming the script if you wanted something else.

### `portfolio_store.py` — comments only

`script/` adds docstring notes that the Journal tab uses `scanner.journal_io` + `scanner.lot_match` and that `journal_dataframe` / `journal_performance` / `day_performance` / `backfill_daily_journal_from_ledgers` are superseded. **No function-body change** in the 39-line diff. Copying `script/` is safe; restoring HEAD is also safe for this file alone.

---

## 1. Verbatim git snapshots

### `git status --porcelain`

```
 M .gitignore
 M app.py
 M best_value_ui.py
 D portfolio_store.py
 D pre_trade_check.py
 M tests/test_journal_pnl.py
 M tests/test_lot_match.py
 M tests/test_pre_trade_check.py
 M tests/test_pretrade_bridge.py
 M tests/test_scanner_greeks_display.py
?? attribution_report.html
?? cha.txt
?? data/
?? docs/
?? instruction/CURSOR_DOCUMENT_BASELINE.md
?? instruction/HANDOVER_dailyScaner.md
?? instruction/ROADMAP.md
?? match.py
?? rebuild_journal.py
?? reconcile.py
?? scanner/journal_view.py
?? scheduler.pid
?? scheduler.pid.lock
?? script/
?? tests/test_journal_metrics.py
?? tests/test_time_stop.py
?? tickers_excluded.json
?? time_of_day.py
?? time_stop.py
?? verify_attribution.py
?? vperday.csv
?? vwap.json
```

### `git log --oneline -5`

```
ce1bf69 Ignore stale earnings so the weekly spread gate never uses a past hard-exit date.
00a96db Show provider Delta and Theta/Prem on the scanner table without changing ranking.
0e4072c Add a self-contained HTML EOD report and a standalone Schwab quote probe.
c874424 Derive journal P&L from FIFO lots instead of stored event fields.
109e001 Seal unmarkable pre-window close rows; fix close-path source reuse
```

### `git diff --stat`

```
 .gitignore                           |    1 +
 app.py                               |  319 +++++--
 best_value_ui.py                     |   89 ++
 portfolio_store.py                   |  665 -------------
 pre_trade_check.py                   | 1703 ----------------------------------
 tests/test_journal_pnl.py            |   11 +-
 tests/test_lot_match.py              |    2 +-
 tests/test_pre_trade_check.py        |    9 +
 tests/test_pretrade_bridge.py        |  376 +++++++-
 tests/test_scanner_greeks_display.py |  144 +++
 10 files changed, 867 insertions(+), 2452 deletions(-)
```

`git status --porcelain -u` expands `?? data/` to **86** files and `?? script/` to 9 files (listed in §4).

---

## 2. HEAD vs `script/` (detail)

Compared via `git show HEAD:<file> > /tmp/…` then `diff` (writes only under `/tmp`).

### `pre_trade_check.py`

Not identical. HEAD 1703 lines; `script/` 2105. First hunks:

- Import `hold_hours_for_dte` from `time_stop`.
- `CHECKS_PATH` / `PREFS_PATH` / `SCANS_PATH` under `data/pretrade/` instead of `data/journal/`.
- `hold_hours` added to prefill key tuples.
- New `ARCHIVE_PREFILL_KEY`, `ARCHIVE_APPLIED_KEY`, `ARCHIVE_SOURCE_FLAG`, `ARCHIVE_GREEKS_CAPTION`, `ARCHIVE_BLANK_CHART`, `PATTERN_*`.
- New `_usable_quote_pair`, `archive_greeks_refusal`, `archive_prefill_from_row`, `stage_archive_prefill`, `apply_archive_prefill_to_session` (diff continues past the first 200 lines: `consume_archive_prefill`, `archive_quote_marker`, `pattern_candidates`, UI wiring).

### `portfolio_store.py`

Not identical. Comment-only: module docstring + `backfill_daily_journal_from_ledgers` docstring + note above `journal_dataframe`. Ledger paths and `close_position` body match HEAD in the diff that was read.

---

## 3. Import surface

`app.py` imports are at **`:88-89`**, not `:81-82` (those lines have moved).

### `pre_trade_check`

| file:line | form | top-level or inside a function |
|---|---|---|
| `app.py:89` | `import pre_trade_check as pre_trade_check` | top-level |
| `tests/test_pre_trade_check.py:9-20` | `from pre_trade_check import DISTANCE_ERROR, …, min_dte_for_hold` | top-level |
| `tests/test_pre_trade_check.py:329` | `from pre_trade_check import CHECKS_PATH, PREFS_PATH, SCANS_PATH` | **inside** `test_pretrade_paths_are_outside_journal_dir` |
| `tests/test_pretrade_bridge.py:11` | `import pre_trade_check as ptc` | top-level |
| `tests/test_pretrade_bridge.py:13-33` | `from pre_trade_check import CHART_FIELDS, …, store_scan_snapshot` | top-level |
| `tests/test_scanner_greeks_display.py:282` | `from pre_trade_check import DELTA_MAX, DELTA_MIN` | **inside** `test_display_band_matches_pretrade_gate` |

Uses (not imports) in `app.py` after the top-level import: `:2635`, `:2638`, `:3138-3139`, `:5065`, `:5077`, `:5084`, `:5846`, `:5852`, `:5890`, `:5894`. Several of those names exist only on the `script/` copy.

No other `*.py` import of the top-level module (grep). `script/pre_trade_check.py` is the module file, not an importer.

### `portfolio_store`

| file:line | form | top-level or inside a function |
|---|---|---|
| `app.py:88` | `import portfolio_store as portfolio_store` | top-level |

Uses: `app.py:1663-1664`, `:1670`, `:1674`, `:1735`, `:1969`, `:2211`, `:2234`, `:3179`. **No test file imports `portfolio_store`.**

---

## 4. Classification

Porcelain paths (one bucket each). `data/` and `script/` are expanded below so every escaping file is named by prefix.

| path | bucket | reason |
|---|---|---|
| `.gitignore` | COMMIT-CODE | Already +1 `data/pretrade/` vs HEAD; belongs in the ignore commit with further lines |
| `app.py` | COMMIT-CODE | Display / journal / pre-trade wiring; imports the two root modules |
| `best_value_ui.py` | COMMIT-CODE | Display-band helpers used by scanner tests and `app.py` |
| `portfolio_store.py` (`D`) | COMMIT-CODE | Restore from `script/` (see decision point), then this is the import path |
| `pre_trade_check.py` (`D`) | COMMIT-CODE | Same — restore from `script/`, not HEAD |
| `tests/test_journal_pnl.py` | COMMIT-CODE | Journal fill-schema assertions |
| `tests/test_lot_match.py` | COMMIT-CODE | Legacy journal file assertion tweak |
| `tests/test_pre_trade_check.py` | COMMIT-CODE | Adds path test that expects `data/pretrade/` |
| `tests/test_pretrade_bridge.py` | COMMIT-CODE | +376 lines of archive-prefill / pattern tests |
| `tests/test_scanner_greeks_display.py` | COMMIT-CODE | Display-band tests; one imports `DELTA_MIN`/`DELTA_MAX` |
| `tests/test_journal_metrics.py` | COMMIT-CODE | FIFO metrics tests; already collected when present |
| `tests/test_time_stop.py` | COMMIT-CODE | Tests for untracked `time_stop.py` |
| `time_stop.py` | COMMIT-CODE | Imported by `app.py:56` and by `script/pre_trade_check.py`; required after script-copy restore |
| `scanner/journal_view.py` | COMMIT-CODE | Imported by `app.py:91-99`; Journal tab |
| `docs/architecture.md` | COMMIT-DOCS | Session 4 architecture |
| `docs/baseline_pip_freeze.txt` | COMMIT-DOCS | Session 1 env pin |
| `docs/baseline_tests.md` | COMMIT-DOCS | Session 1 baseline |
| `docs/baseline_tests.txt` | COMMIT-DOCS | Official pytest log |
| `docs/baseline_tests_run2.txt` | COMMIT-DOCS | Official pytest log 2 |
| `docs/baseline_tests_remainder.txt` | COMMIT-DOCS | Remainder pytest log |
| `docs/baseline_tests_remainder_run2.txt` | COMMIT-DOCS | Remainder pytest log 2 |
| `docs/data_contract.md` | COMMIT-DOCS | Session 3 |
| `docs/findings.md` | COMMIT-DOCS | Findings table |
| `docs/roadmap.md` | COMMIT-DOCS | Session 4 gate record |
| `docs/scoring_spec.md` | COMMIT-DOCS | Session 2 |
| `docs/tree_cleanup_report.md` | COMMIT-DOCS | This report |
| `instruction/CURSOR_DOCUMENT_BASELINE.md` | COMMIT-DOCS | Task pack (other `instruction/` files already tracked) |
| `instruction/HANDOVER_dailyScaner.md` | COMMIT-DOCS | Handover |
| `instruction/ROADMAP.md` | COMMIT-DOCS | Evolution roadmap |
| `attribution_report.html` | GITIGNORE | Generated HTML report |
| `cha.txt` | GITIGNORE | Scratch dump |
| `scheduler.pid` | GITIGNORE | Live PID file |
| `scheduler.pid.lock` | GITIGNORE | Lock next to PID |
| `data/` (86 untracked files) | GITIGNORE | Live DB / journal / broker exports / journal backups — never commit |
| `script/` | GITIGNORE | Stated scratch for a one-off report, not a package |
| `rebuild_journal.py` | DECIDE | Byte-identical twin of `script/rebuild_journal.py` (394 lines each, `diff` empty). One-off rebuild tool; also copied under `data/journal.bak*` |
| `match.py` | DECIDE | No `script/` twin. 37-line attribution-vs-journal rank probe; no importers |
| `reconcile.py` | DECIDE | No `script/` twin. 20-line FIFO printout; no importers |
| `time_of_day.py` | DECIDE | No importers. Hour-bucket printout over journal fills |
| `verify_attribution.py` | DECIDE | No importers. Ad-hoc SQLite checks against `data/attribution.db` |
| `tickers_excluded.json` | DECIDE | `scheduler.py` reads `tickers_excluded.json`; machine-local exclude list |
| `vperday.csv` | DECIDE | Unreferenced dump |
| `vwap.json` | DECIDE | Unreferenced dump |
| `fix_tree.sh` | DECIDE | Human-armed helper; this session does not stage it |

### Root vs `script/` twins

| pair | result |
|---|---|
| `rebuild_journal.py` vs `script/rebuild_journal.py` | **Byte-identical** (394 lines, `diff` empty, both mtime 2026-09-03 16:05, 15166 bytes). Still DECIDE whether the tool belongs at repo root. |
| `match.py` | No `script/` twin. |
| `reconcile.py` | No `script/` twin. |
| `hourly_report.py` / `contract.py` / `correctContract.py` | Only under `script/` (scratch). |

### `?? data/` — what escaped `.gitignore`

`data/attribution.db*`, `data/journal/`, `data/pretrade/`, `data/portfolio.json`, `data/portfolio_closed.json` **are** ignored. Nothing under `data/` is tracked (`git ls-files data/` empty).

The 86 visible files are **not** a single file. Prefixes:

| prefix | count | examples |
|---|---|---|
| `data/broker/` | 8 | `activities-export-2026-08-27.csv` … `2026-09-11.csv` |
| `data/journal copy/` | 6 | `2026-07-28.json`, `2026-08-17.json`, `2026-08-21.json`, … |
| `data/journal.bak/` | 3 | `activities-export-2026-08-27.csv`, `rebuild_journal.py`, `data/broker/rebuild_journal.py` |
| `data/journal.bak-20260903-160550/` | 52 | daily `2026-06-05.json` … `2026-08-26.json` plus nested `rebuild_journal.py` |
| `data/journal.bak-20260909-214347/` | 7 | `2026-08-31.json` … `2026-09-03.json` plus rebuild scripts |
| `data/journal.bak-20260911-174550/` | 10 | `2026-08-31.json` … `2026-09-09.json` plus rebuild scripts |

### `?? script/` contents (all GITIGNORE)

`script/contract.py`, `script/correctContract.py`, `script/hourly_report.py`, `script/portfolio_store.py`, `script/pre_trade_check.py`, `script/rebuild_journal.py`, `script/data/portfolio.json`, `script/data/pretrade/pre_trade_prefs.json`, `script/data/pretrade/pretrade_scans.json`.

---

## 5. `git check-ignore -v`

| path | already covered? | output |
|---|---|---|
| `data/attribution.db` | yes | `.gitignore:7:data/attribution.db*` |
| `data/journal/` | yes | `.gitignore:10:data/journal/` |
| `data/pretrade/` | yes (working-tree line, not in HEAD) | `.gitignore:11:data/pretrade/` |
| `data/portfolio.json` | yes | `.gitignore:8:data/portfolio.json` |
| `data/broker/activities-export-2026-08-27.csv` | **no** | `(not ignored)` |
| `data/journal copy/2026-07-28.json` | **no** | `(not ignored)` |
| `data/journal.bak/rebuild_journal.py` | **no** | `(not ignored)` |
| `data/journal.bak-20260903-160550/2026-06-05.json` | **no** | `(not ignored)` |
| `scheduler.pid` | **no** | `(not ignored)` |
| `scheduler.pid.lock` | **no** | `(not ignored)` |
| `attribution_report.html` | **no** | `(not ignored)` |
| `cha.txt` | **no** | `(not ignored)` |
| `script/pre_trade_check.py` | **no** | `(not ignored)` |
| `vperday.csv` | **no** | `(not ignored)` — left DECIDE, no new ignore line |
| `vwap.json` | **no** | `(not ignored)` — left DECIDE |
| `tickers_excluded.json` | **no** | `(not ignored)` — left DECIDE |
| `docs/architecture.md` | no (correct) | documentation |

### Proposed **new** `.gitignore` lines (only uncovered GITIGNORE paths)

Working tree already has `data/pretrade/`. Add:

```
scheduler.pid
scheduler.pid.lock
attribution_report.html
cha.txt
data/broker/
data/journal copy/
data/journal.bak/
data/journal.bak-*/
script/
```

Do not add `vperday.csv`, `vwap.json`, or `tickers_excluded.json` until a human picks GITIGNORE vs keep.

---

## Predicted test result after `fix_tree.sh` (armed, as written)

Assumes commit 1 copies **`script/` → root** (not HEAD) and commit 3 includes `time_stop.py` so `import pre_trade_check` can resolve `hold_hours_for_dte`.

**Collection errors: 0.** The two official `ModuleNotFoundError` collection interrupts (`tests/test_pre_trade_check.py`, `tests/test_pretrade_bridge.py`) go away. `test_scanner_greeks_display.py::test_display_band_matches_pretrade_gate` can import `DELTA_MIN` / `DELTA_MAX`.

**If the human instead restores HEAD** (not what the script does): collection is still 0, but **14** `test_pretrade_bridge` archive/pattern tests error with `AttributeError`, `test_pretrade_paths_are_outside_journal_dir` fails, and Streamlit dies on missing archive-prefill names. `import pre_trade_check` would **not** need `time_stop`.

### Failures still expected (same causes as Session 1 remainder, F-S1-03…06)

Remainder baseline: 6 failed / 359 passed / 4 xfailed / 2 collection errors. After this script the 2 collection errors become 0 and **one of the six fails should pass**:

| nodeid | after script | why |
|---|---|---|
| `tests/test_scanner_greeks_display.py::test_display_band_matches_pretrade_gate` | **pass** | `pre_trade_check` is importable; `DELTA_MIN`/`DELTA_MAX` exist on both HEAD and `script/` |
| `tests/test_journal_pnl.py::test_aug21_trade_schema_loads_as_fills` | **fail** | `load_journal_day("2026-08-21")` empty; live file is under gitignored `data/journal/` (or only in `data/journal copy/`) |
| `tests/test_journal_pnl.py::test_aug17_fills_have_no_stored_pnl` | **fail** | same for `2026-08-17` |
| `tests/test_lot_match.py::test_legacy_file_drops_stored_pnl` | **fail** | `data/journal/2026-07-28.json` missing → `FileNotFoundError` |
| `tests/test_journal_metrics.py::test_live_journal_concat_fifo_expected_totals` | **fail** | live FIFO `n_closed` 119 vs 289 |
| `tests/test_massive_strike_window.py::test_no_pagination_cap_hit_with_window` | **fail** | mocked Massive `fetch_chain` empty frame (`:73`) |

**Predicted fail count: 5.** (The five rows marked fail. Not 6.)

### Xfail (unchanged, 4)

- `tests/test_best_value_engine.py::test_new_entrant_is_not_systematically_penalised`
- `tests/test_best_value_engine.py::test_score_is_stable_when_an_unrelated_contract_joins_the_universe`
- `tests/test_best_value_engine.py::test_value_score_stays_within_its_documented_range`
- `tests/test_golden_master.py::test_pre_refactor_engine_matches_current`

### Newly collected, expected to pass (not a guarantee — not executed this session)

- `tests/test_pre_trade_check.py` T1–T10 (10) + `test_pretrade_paths_are_outside_journal_dir` (needs `script/` paths)
- Existing `tests/test_pretrade_bridge.py` tests plus the 14 archive/pattern tests (need `script/` APIs)
- `tests/test_time_stop.py` (8)
- Other `tests/test_journal_metrics.py` tests except the live concat one
- New `tests/test_scanner_greeks_display.py` display-band tests (`test_filtered_view_only_pretrade_delta_band`, `test_null_delta_excluded_from_display_filter`, `test_show_all_restores_full_ranked_list`, `test_delta_band_boundaries_and_signed`)

Official collect-only after the script should show **0 errors**. Item count will be **higher than 369** because `test_pre_trade_check.py` + `test_pretrade_bridge.py` now collect (11 + ~25) and `test_time_stop.py` adds 8. Exact collected N was not re-run here.

If collection errors ≠ 0, or failures are not exactly the five nodeids above, something unexpected changed.
