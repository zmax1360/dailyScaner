# Baseline test results

Captured 2026-09-12. Session 1 of `instruction/CURSOR_DOCUMENT_BASELINE.md`.
No tests were modified, deleted, or marked.

## Environment

```
python --version
Python 3.12.7

git rev-parse HEAD
ce1bf691f82c5f84bff93e948dfaeb77c34238e5

config_hash(SCORING) at session start
243ecda68cfc8618

pytest --version (from suite banner)
pytest-9.1.1  pluggy-1.6.0
platform darwin
rootdir: /Users/alizafarianaraki/works/optionTrading
configfile: pyproject.toml
testpaths: tests
plugins: cov-7.1.0, socket-0.8.1, langsmith-0.7.24, anyio-4.13.0
```

`git status --porcelain` at session start (verbatim):

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
```

(Additional untracked files existed; the block above is the head of the capture. Full `pip freeze` is in `docs/baseline_pip_freeze.txt`. Notable installed versions: `pytest==9.1.1`, `pandas==2.3.3`, `optionlab==1.6.3`, `yfinance==1.5.2`. `pyproject.toml` pins `pytest==7.4.4` and `yfinance==1.2.0`; the running env does not match those pins.)

`pyproject.toml` `addopts` is `-q` (`pyproject.toml:30`). The session invoked `pytest -v --tb=short`, which overrides quiet mode for the captured logs.

## Official invocation (as specified)

```
pytest -v --tb=short 2>&1 | tee docs/baseline_tests.txt
```

Run 1 verbatim log: `docs/baseline_tests.txt` (wall 2.95s).
Run 2 verbatim log: `docs/baseline_tests_run2.txt` (wall 1.93s).

Both official runs stopped at collection:

```
collected 369 items / 2 errors
!!!!!!!!!!!!!!!!!!! Interrupted: 2 errors during collection !!!!!!!!!!!!!!!!!!!!
============================== 2 errors in 2.95s ===============================
```

Run 2 is the same two errors (1.93s). Pass/fail sets: empty both times. No flake on the official invocation.

| count | run 1 | run 2 |
|---|---|---|
| passed | 0 | 0 |
| failed | 0 | 0 |
| errored | 2 | 2 |
| skipped | 0 | 0 |
| xfailed | 0 | 0 |
| xpassed | 0 | 0 |
| wall-clock | 2.95s | 1.93s |

The pipeline `pytest … | tee` reports exit 0 from `tee` even when pytest is interrupted. Recorded as a finding; not changed.

## Collection errors (official + remainder)

| nodeid | one-line reason | file:line |
|---|---|---|
| `tests/test_pre_trade_check.py` (module collection) | `ModuleNotFoundError: No module named 'pre_trade_check'` | `tests/test_pre_trade_check.py:9` (`from pre_trade_check import`) |
| `tests/test_pretrade_bridge.py` (module collection) | `ModuleNotFoundError: No module named 'pre_trade_check'` | `tests/test_pretrade_bridge.py:11` (`import pre_trade_check as ptc`) |

`pre_trade_check.py` is deleted from the repo root (`git status`: `D pre_trade_check.py`). A copy exists at `script/pre_trade_check.py`. Tests import the top-level name. Logged in `docs/findings.md` as F-S1-01.

## Supplementary invocation (collection abort workaround)

The specified command executes zero tests. After the two official runs, the same suite was run with `--continue-on-collection-errors` so the 369 already-collected items could execute. This is **not** the command in the task pack.

```
pytest -v --tb=short --continue-on-collection-errors 2>&1 | tee docs/baseline_tests_remainder.txt
```

Run A: `docs/baseline_tests_remainder.txt` (5.28s).
Run B: `docs/baseline_tests_remainder_run2.txt` (5.31s).

Both remainder runs: **6 failed, 359 passed, 4 xfailed, 2 warnings, 2 errors**. Same fail/xfail/error nodeids. No flake.

| count | remainder A | remainder B |
|---|---|---|
| passed | 359 | 359 |
| failed | 6 | 6 |
| errored | 2 | 2 |
| skipped | 0 | 0 |
| xfailed | 4 | 4 |
| xpassed | 0 | 0 |
| wall-clock | 5.28s | 5.31s |

## Failing tests (remainder only; official run executed none)

| nodeid | one-line reason | assertion / raise file:line |
|---|---|---|
| `tests/test_journal_metrics.py::test_live_journal_concat_fifo_expected_totals` | live FIFO `n_closed` is 119, expected 289 | `tests/test_journal_metrics.py:168` `assert stats["n_closed"] == 289` |
| `tests/test_journal_pnl.py::test_aug21_trade_schema_loads_as_fills` | `load_journal_day("2026-08-21")` returns empty frame | `tests/test_journal_pnl.py:11` `assert not df.empty` |
| `tests/test_journal_pnl.py::test_aug17_fills_have_no_stored_pnl` | `load_journal_day("2026-08-17")` returns empty frame | `tests/test_journal_pnl.py:19` `assert not df.empty` |
| `tests/test_lot_match.py::test_legacy_file_drops_stored_pnl` | `data/journal/2026-07-28.json` missing | `tests/test_lot_match.py:92` `Path(...).read_text()` → `FileNotFoundError` |
| `tests/test_massive_strike_window.py::test_no_pagination_cap_hit_with_window` | mocked `fetch_chain` returned empty frame | `tests/test_massive_strike_window.py:73` `assert not df.empty` |
| `tests/test_scanner_greeks_display.py::test_display_band_matches_pretrade_gate` | `ModuleNotFoundError: No module named 'pre_trade_check'` | `tests/test_scanner_greeks_display.py:282` `from pre_trade_check import DELTA_MAX, DELTA_MIN` |

`load_journal_day` returns an empty `FILL_COLS` frame when the day file is absent or unreadable (`scanner/journal_io.py:189-198`). `data/journal/` is gitignored (`.gitignore` lists `data/journal/`).

## SKIPPED and XFAIL

### Skipped (executed: none)

No test reported SKIPPED in official or remainder runs.

Conditional skip sites that did **not** fire:

| nodeid | skip reason string | conditional? |
|---|---|---|
| `tests/test_dashboard_adapters.py::test_app_no_yfinance_import` | `app.py not yet written` (`skipif(not APP.exists())`) | conditional (`tests/test_dashboard_adapters.py:192`) |
| `tests/test_dashboard_adapters.py::test_app_no_indicator_computation` | `app.py not yet written` | conditional (`tests/test_dashboard_adapters.py:199`) |
| `tests/test_journal_metrics.py::test_live_journal_concat_fifo_expected_totals` | `no journal day files` / `journal too small for rebuilt-broker expected totals` | conditional (`tests/test_journal_metrics.py:157`, `:165`); ran and failed instead (`n_closed` 119 ≥ 20) |

No unconditional `pytest.mark.skip` found under `tests/`. No dead (always-skipped) test executed.

### XFAIL (remainder; official did not reach them)

| nodeid | reason string | conditional? |
|---|---|---|
| `tests/test_best_value_engine.py::test_new_entrant_is_not_systematically_penalised` | `DEFECT: NaN -> 1.0 is not neutral. On a scale where dVol is thousands, 1.0 is effectively zero, so brand-new sweeps are ranked LAST.` | unconditional `xfail(strict=True)` (`tests/test_best_value_engine.py:129`) |
| `tests/test_best_value_engine.py::test_score_is_stable_when_an_unrelated_contract_joins_the_universe` | `DEFECT: min-max is computed within each snapshot, so Value_Score is a rank, not a level. best_value_archive.py persists it and derives Score_Velocity from it.` | unconditional `xfail(strict=True)` (`tests/test_best_value_engine.py:170`) |
| `tests/test_best_value_engine.py::test_value_score_stays_within_its_documented_range` | `DEFECT: multipliers are applied AFTER normalisation and compound without a cap, so Value_Score escapes [0, 1]` | unconditional `xfail(strict=True)` (`tests/test_best_value_engine.py:186`) |
| `tests/test_golden_master.py::test_pre_refactor_engine_matches_current` | `engine v2 (CURSOR_DELTA_TASKS C): BS delta changes ranking vs pre-config commit 6a113f8 — intentional, do not revert` | unconditional `xfail(strict=True)` (`tests/test_golden_master.py:103`) |

### XPASS

None.

## Flaky tests

Official run 1 vs run 2: identical (2 collection errors, 0 executed).
Remainder A vs B: identical nodeid sets (6 failed, 4 xfailed, 2 errors, 359 passed).

No flake observed.

## Wall-clock runtime

- Official specified command: 2.95s + 1.93s.
- Remainder (extra): 5.28s + 5.31s.
- Combined wall of all four pytest processes: ~15.5s.

## Invariants encoded by the seven named files

### `tests/test_golden_master.py`

`test_golden_master_matches_expected` (`tests/test_golden_master.py:77`) loads `tests/golden/chain_aapl.json`, runs `calculate_best_value` (`tests/test_golden_master.py:16`, `:39`), and compares `_COMPARE_COLS` (`tests/test_golden_master.py:22-27`) including `Value_Score`, `delta`, `_nlev`, `_nflow`, `pool` to `tests/golden/scored_expected.json`. Any scoring-literal change that moves those columns fails this test. `test_pre_refactor_engine_matches_current` (`tests/test_golden_master.py:108`) loads `best_value.py` from git `6a113f8` and is xfail-strict because current ranking diverges.

### `tests/test_import_graph.py`

`test_yfinance_import_graph_allowlist` (`tests/test_import_graph.py:53`) AST-walks project `.py` files and fails if `yfinance` is imported outside `_YF_ALLOWED` (`sources/yahoo.py`, `news_service.py`) (`tests/test_import_graph.py:12-15`). `test_app_does_not_import_yfinance_directly` (`tests/test_import_graph.py:75`) applies the same walk to `app.py` only.

### `tests/test_best_value_engine.py`

Characterisation of `calculate_best_value` / `build_best_value_df` / `attach_dvol` (`tests/test_best_value_engine.py:18`). Pins output columns (`:49`), empty-frame behaviour (`:57`), `min_volume` exclusion (`:65`), 16:15 ET 0DTE drop (`:75`), expired-row drop (`:86`), BS `delta` emission (`:94`), signed `dVol` (`:147`), multiplicative multipliers (`:229`), and 1SD penalty (`:237`). Three `xfail(strict=True)` tests (`:129`, `:170`, `:186`) encode known scoring defects (NaN-dVol fill, per-snapshot min-max, uncapped product).

### `tests/test_attribution.py`

Pins WAL + schema names `runs` / `flags` / `v_outcomes` (`tests/test_attribution.py:52`). `test_config_hash_stable_and_sensitive` (`:68`) requires `config_hash(SCORING)` stable on copy and different after a 0.01 multiplier tweak. `test_multipliers_are_recorded` (`:77`) rebuilds `Value_Score` as `_nlev*w_lev + _nflow*w_flow` times the product of `_multipliers` values. `test_log_run_reconciles_counts` (`:126`) requires `n_flags == n_scored + n_ctrl` and no empty multiplier JSON. `test_control_is_independent_of_engine_output` (`:110`) pins control selection to chain/spot/expiry, not scores.

### `tests/test_dailyScaner_regressions.py`

Regression fixtures from 2026-07-15 (`tests/test_dailyScaner_regressions.py:1-21`). R1: 15M opening range uses 9:30–9:45 only (`:75`) and is immutable after 9:45 (`:83`). R2: `proximity_filter` drops 0DTE (`:134`) and OI below `MIN_OI_FOR_MAGNET` (`:139`); OI=0 does not auto-pass (`:144`). R3: `market_is_open` is False at 16:32 (`:159`).

### `tests/scoring_fixtures.py`

Not a test module. `pad_min_pool` (`tests/scoring_fixtures.py:11`) appends far-strike fillers so each present DTE pool has at least `SCORING["min_pool_size"]` / `DEFAULT_MIN_POOL_SIZE` survivors (`:26-28`), using `scoring_pool` (`:8`, `:35`). Engine-v1.2 tests that would otherwise hit an under-size pool use this pad.

### `tests/test_sources_fixture.py`

`FixtureSource` serves a recorded archive (`tests/test_sources_fixture.py:23`) with `CHAIN_COLUMNS`, deterministic repeats (`:36`), and dict-payload construction (`:48`). `test_no_test_reaches_network` (`:56`) AST-forbids direct `yfinance` / `requests` imports in `tests/` (`:20`).

## Coverage

| path | read | skimmed | not opened |
|---|---|---|---|
| `docs/baseline_tests.txt` | all (official run 1) | | |
| `docs/baseline_tests_run2.txt` | all (official run 2) | | |
| `docs/baseline_tests_remainder.txt` | summary + failures | progress dots | |
| `docs/baseline_tests_remainder_run2.txt` | summary + failures | progress dots | |
| `tests/test_golden_master.py` | 1–173 | | |
| `tests/test_import_graph.py` | 1–79 | | |
| `tests/test_best_value_engine.py` | 1–264 | | |
| `tests/test_attribution.py` | 1–180 | 181–475 | |
| `tests/test_dailyScaner_regressions.py` | 1–176 | 179–end (R4) | |
| `tests/scoring_fixtures.py` | 1–61 | | |
| `tests/test_sources_fixture.py` | 1–86 | | |
| `tests/test_journal_metrics.py` | 154–174 | 1–153 | |
| `tests/test_journal_pnl.py` | 1–31 | | |
| `tests/test_lot_match.py` | 81–99 | 1–80 | |
| `tests/test_massive_strike_window.py` | 1–77 | 79–end | |
| `tests/test_scanner_greeks_display.py` | 270–285 | rest | |
| `tests/test_dashboard_adapters.py` | 189–206 | rest | |
| `tests/test_pre_trade_check.py` | import line 9 only | | remainder of file |
| `tests/test_pretrade_bridge.py` | import line 11 only | | remainder of file |
| `scanner/journal_io.py` | 189–207 | | rest |
| `pyproject.toml` | 1–47 | | |
| other `tests/*.py` | | collected via pytest file list | bodies not read |

Could not run: every test under `tests/test_pre_trade_check.py` and `tests/test_pretrade_bridge.py` (collection `ModuleNotFoundError`). Official `pytest -v --tb=short` ran zero collected tests because pytest interrupted on those two errors.
