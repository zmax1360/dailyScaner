# Findings

Defects observed while documenting. No fixes applied.

| id | file:line | one-line description | severity |
|---|---|---|---|
| F-S1-01 | `tests/test_pre_trade_check.py:9`, `tests/test_pretrade_bridge.py:11` | Collection `ModuleNotFoundError: No module named 'pre_trade_check'`. Root `pre_trade_check.py` is deleted (`git status` `D`); a copy sits at `script/pre_trade_check.py`. Official `pytest -v --tb=short` interrupts and executes 0 tests. | P0 |
| F-S1-02 | `tests/test_scanner_greeks_display.py:282` | Same missing top-level `pre_trade_check` import inside a collected test (`DELTA_MIN` / `DELTA_MAX`). | P0 |
| F-S1-03 | `tests/test_journal_pnl.py:11`, `tests/test_journal_pnl.py:19`; `scanner/journal_io.py:189-198` | `load_journal_day` returns an empty `FILL_COLS` frame for `2026-08-21` and `2026-08-17`. Day files live under gitignored `data/journal/`. | P1 |
| F-S1-04 | `tests/test_lot_match.py:92` | `data/journal/2026-07-28.json` is absent → `FileNotFoundError` (direct `Path.read_text`, not `load_journal_day`). | P1 |
| F-S1-05 | `tests/test_journal_metrics.py:168` | Live concat FIFO `n_closed` is 119 vs expected 289 (skip gates at `:157` / `:165` did not fire). | P1 |
| F-S1-06 | `tests/test_massive_strike_window.py:73` | Mocked Massive `fetch_chain` returned an empty frame; `assert not df.empty` failed. | P1 |
| F-S1-07 | `tests/test_best_value_engine.py:129` | Marked xfail-strict: NaN `dVol` filled as 1.0 ranks new entrants last. | P1 |
| F-S1-08 | `tests/test_best_value_engine.py:170` | Marked xfail-strict: per-snapshot min-max makes `Value_Score` a rank, not a level. | P1 |
| F-S1-09 | `tests/test_best_value_engine.py:186` | Marked xfail-strict: multiplier product after normalisation leaves `[0, 1]`. | P1 |
| F-S1-10 | `pyproject.toml:23` vs running env | `pyproject.toml` pins `pytest==7.4.4`; suite ran under `pytest-9.1.1`. Pins `yfinance==1.2.0`; `pip freeze` has `yfinance==1.5.2`. | P2 |
| F-S1-11 | official invoke `pytest … \| tee` | Pipeline exit code is `tee`'s 0; pytest collection interrupt is hidden from `$?`. | P2 |
| F-S2-01 | `best_value.py:5` vs `telegram_bot.py` | Module docstring says `telegram_bot.py` imports the scoring engine; that file has no `calculate_best_value` / `build_best_value_df` call. | P2 |
| F-S2-02 | `best_value.py:676` | Persisted `flags.score` is `Value_Score.round(4)`; `base_score * Π(multipliers)` differs at ~1e-5 on sampled rows 47578–47582. Blend (a) residual is 0. | P2 |
| F-S2-03 | `best_value.py:753` | `int(c.get("dte") or 0)` sends missing dte into the `0DTE` pool (`scoring_pool.py:52-53`). | P1 |
| F-S2-04 | `app.py:2391` vs `best_value.py:346-356` | UI caption states flow uses `\|ΔVol\|`; `_flow` uses signed `dVol` then `clip(lower=0)`. | P2 |
| F-S3-01 | `notify_delivery.py:255` vs `attribution.py:322` | Archive `best_value.engine_sha` is `config_hash(SCORING)`, not `git rev-parse`. | P1 |
| F-S3-02 | `snapshot_store.py:21-22`, `scheduler.py:52` | `flow_snapshot.json`, `gate_history.json`, and `scheduler.pid` are written at repo root and are not in `.gitignore`. | P2 |
| F-S3-03 | `script/portfolio_store.py:395-412` | `close_position` computes a second realized-P&L pair beside `scanner.lot_match._pnl`. | P1 |
| F-S4-01 | `app.py:1-4` vs `app.py:2421-2436` | Module docstring says display-only / “No indicators recomputed here”; Best Value panel calls `build_best_value_df` with VWAP/POV/catalyst kwargs. | P1 |
| F-S4-02 | `app.py:31` vs `app.py:1638` / `:2333` | Imports `calculate_best_value`; panel docstring names that function; the call is `build_best_value_df`. | P2 |
| F-S4-03 | `chain_quality.py:179-182` | Quality-gate fail uses call `unusable/total` only; `put_stats` are computed and logged, not in the predicate. | P2 |
| F-S4-04 | `plist/com.optiontrading.nightly.plist:24-31` vs `nightly.sh:3` | `StartCalendarInterval` is machine-local 17:15; comment/script say 17:15 ET; plist has no `TZ` (unlike health-check). | P2 |
| F-S4-05 | `plist/com.zmax.scanner.weekly.plist:13` | ProgramArguments path is `/Users/YOURUSER/trading/scanner/run_scanner.sh`. | P2 |
| F-S4-06 | `run_scanner.sh:14`, `:60-64` | `SCANNER_DIR="$HOME/trading/scanner"`; python failure sets `STATUS=FAILED` and does not propagate the scanner exit code. | P2 |
| F-S4-07 | `dailyScaner.py:1284-1295` vs `app.py:2421-2436` | Scan archive snapshot omits VWAP/POV/catalyst kwargs that the Streamlit panel passes into the same `build_best_value_df`. | P1 |
| F-S5-01 | `scheduler.py:146`, `dailyScaner.py:309-313` | 2026-09-07 is Labor Day (US markets closed). RO `data/attribution.db`: **127** `runs` / **7500** `flags` (AAPL; 2 EOD). Scheduler `market_is_open` treats Mon–Fri clock hours as open (`:146` Sat/Sun only). Scanner `market_is_open` same weekday test; docstring says holidays are unhandled (`:310-312`). That function is not an abort: `print_report` returns early (`dailyScaner.py:821-830`) after archive + `_log_scan_attribution` already wrote. | P0 |
| F-S5-02 | `scheduler.py:463`, `scheduler_config.json:8` | 2026-09-08 AAPL **231** runs / **12995** flags vs ~154 / ~8100 on 09-04 and 09-09. Inter-run avg gap **1.7 min** (min 0.0) vs **2.6 min** on 09-04/09-09; ~35 runs/hour vs ~24. Configured `default_interval_min` is 5. Three EOD rows that day vs two on adjacent sessions. | P0 |
| F-S5-03 | `scheduler.py:132-157`, `tests/test_dailyScaner_regressions.py:159-171` | Cadence and holiday coverage. Config 5 min implies ~12 AAPL runs/hour; observed ~24/hour on normal days (avg gap 2.6 min, min 0.0 — overlapping schedulers). `health_check.py:118-124` expects `rows_today>0` whenever `weekday()<5`, so Labor Day looks healthy. Tests pin `market_is_open` False at 16:32 (`:159`) and Saturday 2026-07-18 (`:169-171`). No test pins a US holiday. Grep of `tests/` finds no `holiday` / `Labor` / `2026-09-07`. | P0 |

## Consolidated (Sessions 1–4 + F-S5)

| id | file:line | one-line description | severity |
|---|---|---|---|
| F-S1-01 | `tests/test_pre_trade_check.py:9`, `tests/test_pretrade_bridge.py:11` | Collection `ModuleNotFoundError: No module named 'pre_trade_check'`. Root `pre_trade_check.py` is deleted (`git status` `D`); a copy sits at `script/pre_trade_check.py`. Official `pytest -v --tb=short` interrupts and executes 0 tests. | P0 |
| F-S1-02 | `tests/test_scanner_greeks_display.py:282` | Same missing top-level `pre_trade_check` import inside a collected test (`DELTA_MIN` / `DELTA_MAX`). | P0 |
| F-S1-03 | `tests/test_journal_pnl.py:11`, `tests/test_journal_pnl.py:19`; `scanner/journal_io.py:189-198` | `load_journal_day` returns an empty `FILL_COLS` frame for `2026-08-21` and `2026-08-17`. Day files live under gitignored `data/journal/`. | P1 |
| F-S1-04 | `tests/test_lot_match.py:92` | `data/journal/2026-07-28.json` is absent → `FileNotFoundError` (direct `Path.read_text`, not `load_journal_day`). | P1 |
| F-S1-05 | `tests/test_journal_metrics.py:168` | Live concat FIFO `n_closed` is 119 vs expected 289 (skip gates at `:157` / `:165` did not fire). | P1 |
| F-S1-06 | `tests/test_massive_strike_window.py:73` | Mocked Massive `fetch_chain` returned an empty frame; `assert not df.empty` failed. | P1 |
| F-S1-07 | `tests/test_best_value_engine.py:129` | Marked xfail-strict: NaN `dVol` filled as 1.0 ranks new entrants last. | P1 |
| F-S1-08 | `tests/test_best_value_engine.py:170` | Marked xfail-strict: per-snapshot min-max makes `Value_Score` a rank, not a level. | P1 |
| F-S1-09 | `tests/test_best_value_engine.py:186` | Marked xfail-strict: multiplier product after normalisation leaves `[0, 1]`. | P1 |
| F-S1-10 | `pyproject.toml:23` vs running env | `pyproject.toml` pins `pytest==7.4.4`; suite ran under `pytest-9.1.1`. Pins `yfinance==1.2.0`; `pip freeze` has `yfinance==1.5.2`. | P2 |
| F-S1-11 | official invoke `pytest … \| tee` | Pipeline exit code is `tee`'s 0; pytest collection interrupt is hidden from `$?`. | P2 |
| F-S2-01 | `best_value.py:5` vs `telegram_bot.py` | Module docstring says `telegram_bot.py` imports the scoring engine; that file has no `calculate_best_value` / `build_best_value_df` call. | P2 |
| F-S2-02 | `best_value.py:676` | Persisted `flags.score` is `Value_Score.round(4)`; `base_score * Π(multipliers)` differs at ~1e-5 on sampled rows 47578–47582. Blend (a) residual is 0. | P2 |
| F-S2-03 | `best_value.py:753` | `int(c.get("dte") or 0)` sends missing dte into the `0DTE` pool (`scoring_pool.py:52-53`). | P1 |
| F-S2-04 | `app.py:2391` vs `best_value.py:346-356` | UI caption states flow uses `\|ΔVol\|`; `_flow` uses signed `dVol` then `clip(lower=0)`. | P2 |
| F-S3-01 | `notify_delivery.py:255` vs `attribution.py:322` | Archive `best_value.engine_sha` is `config_hash(SCORING)`, not `git rev-parse`. | P1 |
| F-S3-02 | `snapshot_store.py:21-22`, `scheduler.py:52` | `flow_snapshot.json`, `gate_history.json`, and `scheduler.pid` are written at repo root and are not in `.gitignore`. | P2 |
| F-S3-03 | `script/portfolio_store.py:395-412` | `close_position` computes a second realized-P&L pair beside `scanner.lot_match._pnl`. | P1 |
| F-S4-01 | `app.py:1-4` vs `app.py:2421-2436` | Module docstring says display-only / “No indicators recomputed here”; Best Value panel calls `build_best_value_df` with VWAP/POV/catalyst kwargs. | P1 |
| F-S4-02 | `app.py:31` vs `app.py:1638` / `:2333` | Imports `calculate_best_value`; panel docstring names that function; the call is `build_best_value_df`. | P2 |
| F-S4-03 | `chain_quality.py:179-182` | Quality-gate fail uses call `unusable/total` only; `put_stats` are computed and logged, not in the predicate. | P2 |
| F-S4-04 | `plist/com.optiontrading.nightly.plist:24-31` vs `nightly.sh:3` | `StartCalendarInterval` is machine-local 17:15; comment/script say 17:15 ET; plist has no `TZ` (unlike health-check). | P2 |
| F-S4-05 | `plist/com.zmax.scanner.weekly.plist:13` | ProgramArguments path is `/Users/YOURUSER/trading/scanner/run_scanner.sh`. | P2 |
| F-S4-06 | `run_scanner.sh:14`, `:60-64` | `SCANNER_DIR="$HOME/trading/scanner"`; python failure sets `STATUS=FAILED` and does not propagate the scanner exit code. | P2 |
| F-S4-07 | `dailyScaner.py:1284-1295` vs `app.py:2421-2436` | Scan archive snapshot omits VWAP/POV/catalyst kwargs that the Streamlit panel passes into the same `build_best_value_df`. | P1 |
| F-S5-01 | `scheduler.py:146`, `dailyScaner.py:309-313` | 2026-09-07 is Labor Day (US markets closed). RO `data/attribution.db`: **127** `runs` / **7500** `flags` (AAPL; 2 EOD). Scheduler `market_is_open` treats Mon–Fri clock hours as open (`:146` Sat/Sun only). Scanner `market_is_open` same weekday test; docstring says holidays are unhandled (`:310-312`). That function is not an abort: `print_report` returns early (`dailyScaner.py:821-830`) after archive + `_log_scan_attribution` already wrote. | P0 |
| F-S5-02 | `scheduler.py:463`, `scheduler_config.json:8` | 2026-09-08 AAPL **231** runs / **12995** flags vs ~154 / ~8100 on 09-04 and 09-09. Inter-run avg gap **1.7 min** (min 0.0) vs **2.6 min** on 09-04/09-09; ~35 runs/hour vs ~24. Configured `default_interval_min` is 5. Three EOD rows that day vs two on adjacent sessions. | P0 |
| F-S5-03 | `scheduler.py:132-157`, `tests/test_dailyScaner_regressions.py:159-171` | Cadence and holiday coverage. Config 5 min implies ~12 AAPL runs/hour; observed ~24/hour on normal days (avg gap 2.6 min, min 0.0 — overlapping schedulers). `health_check.py:118-124` expects `rows_today>0` whenever `weekday()<5`, so Labor Day looks healthy. Tests pin `market_is_open` False at 16:32 (`:159`) and Saturday 2026-07-18 (`:169-171`). No test pins a US holiday. Grep of `tests/` finds no `holiday` / `Labor` / `2026-09-07`. | P0 |
