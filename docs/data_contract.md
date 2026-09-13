# Data contract

Persistence boundaries as implemented. Read-only against `data/attribution.db`.
`config_hash(SCORING)` at write time: `243ecda68cfc8618`.

---

## 1. INVENTORY

| path / pattern | format | writer | readers | write frequency | `.gitignore` |
|---|---|---|---|---|---|
| `archive/{TICKER}_{YYYYMMDD_HHMMSS}.json` | JSON | `dailyScaner.save_archive` (`dailyScaner.py:893-950`) | `app.py` (latest-archive load ~`:289-310`, Flow tab `:421-426`), `telegram_bot.py:125-155`, `sources.fixture` (explicit path), `chain_quality` EOD lookup | Each successful `dailyScaner.run` (`:1300`) | yes (`archive/` `.gitignore:4`) |
| `archive/{TICKER}_{ts}.txt` | text | `save_archive` returns the `.txt` name (`dailyScaner.py:950`); body not written in the function that was read | Unverified | same | yes |
| `archive_weekly/{TICKER}_{YYYYMMDD_HHMMSS}.json` + `.txt` | JSON + thesis text | `weekly.save_archive` (`weekly.py:495-521`) | `app.py:3993-4000` (`_latest_weekly_archive`) | Each `weekly.py` run | yes (`archive_weekly/` `:5`) |
| `data/attribution.db` (+ WAL/SHM) | SQLite | `attribution.log_run` / `write_mark` (`attribution.py:631`, `:1100`) | `eod_report.py`, `health_check.py`, `mark_runner.py`, `verify_attribution.py` | Each scan (runs+flags); marks on `mark_runner` cadence | yes (`data/attribution.db*` `:7`) |
| `data/best_value_archive.csv` | CSV | `best_value_archive._save_archive_to_disk` (`best_value_archive.py:71-73`) via `log_best_value_run` | `load_archive_from_disk` (`:45`), `app.py` archive section | Each Streamlit Best Value refresh that logs (`app.py:2602`) | yes (`:6`) |
| `data/journal/YYYY-MM-DD.json` | JSON array of fills | `scanner.journal_io.append_fills` (`scanner/journal_io.py:210`) | `load_journal_day` (`:189`), `scanner.attribution`, `scanner.journal_view` | On fill append | yes (`data/journal/` `:10`) |
| `flow_snapshot.json` | JSON | `snapshot_store.save_snapshot` (`snapshot_store.py:54-65`) | `load_snapshot` (`:34`) | Dashboard overwrite | **no** |
| `gate_history.json` | JSON | `snapshot_store` (file named `:22`) | same module | last 20 gate evals (`snapshot_store.py:6`) | **no** |
| `scheduler.pid` | text PID | `scheduler.py` (`PID_FILE` `:52`) | `app.py:844`, `:878` | Scheduler start | **no** (appears untracked) |
| `logs/{name}.log` | rotating text | `logging_config.setup_logging` (`logging_config.py:75-88`) | humans / ops | continuous | yes (`logs/` `:19`, `*.log` `:18`) |
| `data/portfolio.json` / `data/portfolio_closed.json` | JSON | `script/portfolio_store.py` (root module deleted) | `app.py` imports `portfolio_store` | On position edit/close | yes (`:8-9`) |
| `data/pretrade/` | JSON | `pre_trade_check` (root deleted; copy `script/pre_trade_check.py`) | Pre-Trade UI | on check save | yes (`:11`) |
| `data/mark_runner_cursor.json` | JSON | `mark_runner.py` (`CURSOR_PATH` `:56`) | mark_runner | per pass | yes (`:17`) |
| `data/mark_runner_cap_hits.jsonl` | JSONL | mark_runner | mark_runner | on runtime cap | yes (`:16`) |
| `scheduler_config.json` | JSON | operator / `scheduler.load_config` (`scheduler.py:75`) | scheduler | rare | no |

Sources (`sources/yahoo.py`, `sources/massive.py`, `data_adapter.py`) do not write these artifacts. `sources/fixture.py` **reads** a caller-supplied archive path (`sources/fixture.py:2-4`).

---

## 2. ARCHIVE JSON

Writer payload keys, from `dailyScaner.save_archive` (`dailyScaner.py:901-941`):

| key | set at |
|---|---|
| `timestamp` | `datetime.now(ET).isoformat()` (`:897`, `:902`) |
| `source` | `source_name` (`:903`) |
| `quote_source` | `quote_source` (`:904`) |
| `is_eod` | bool (`:905`) |
| `settlement_converged` | bool or None (`:906-908`) |
| `chain_volume_rollover` | bool (`:909`) |
| `best_value` | `serialize_best_value_rows` result or None (`:910`, call site `:1295-1308`) |
| `spot` | `:911` |
| `or_data` | `:912` |
| `direction` | `:913` |
| `session` | `:914` |
| `timeframes` | filled from `tf_data` (`:915`, `:944-945`) |
| `signal_magnets` | call/put after `proximity_filter` (`:921-934`) |
| `volume` | `total_call_vol`, `total_put_vol`, `pc_ratio`, `top_puts`, `top_calls` (`:935-941`) |

`volume.top_*` records: core columns `strike, expiry, dte, lastPrice, bid, ask, volume, openInterest, impliedVolatility` plus `delta, theta` when present (`dailyScaner.py:395-413`).

`best_value` object from `notify_delivery.serialize_best_value_rows` (`notify_delivery.py:251-257`): `rows`, `n_scored`, `flow_dispersion`, `engine_sha`, `pools_skipped`. The field named `engine_sha` is set to `config_hash(SCORING)` (`:255`), not `attribution.engine_sha()`. See F-S3-01.

### Cross-check vs a live file

Newest file by mtime: `archive/AAPL_20260911_163025.json`. Top-level keys:

`best_value, chain_volume_rollover, direction, is_eod, or_data, quote_source, session, settlement_converged, signal_magnets, source, spot, timeframes, timestamp, volume`

That set equals the writer keys. No extra key. No missing key.

Older files (example `archive/TSLA_20260630_094458.json`) have only `spot, timeframes, timestamp, volume`.

### Consumers

`app.py` (grep): `volume`, `timeframes`, `signal_magnets`, `session`, `or_data`, `spot`, `direction`, `timestamp`. Weekly files: `macro`, `oi_structure`, `daily`, `weekly`, `checklist_score`, `earnings_date`, `earnings_days`, `thesis` (`app.py:3993-4000`).

`telegram_bot.py`: same daily keys plus `best_value` (`telegram_bot.py:376-382`, `:559-560`). Best Value is read from the archive; the bot does not call `calculate_best_value` (`telegram_bot.py:558-559`).

---

## 3. SQLITE SCHEMA

### Base DDL (`attribution._SCHEMA`, `attribution.py:47-109`)

`runs`: `run_id, ts_et, ticker, n_scored, config_hash, engine_sha, daily_bias, market_state, news_bias, spot, vwap_state, run_kind, notes`.

`flags`: `flag_id, run_id, ts_et, ticker, side, strike, expiry, score, rank, nlev, nflow, base_score, multipliers, mid, bid, ask, spot, is_control, mark_t1h, mark_t1d, mark_close, mark_expiry, mark_t15m, mark_t30m, marked_t1h_at, marked_t1d_at, marked_close_at, marked_exp_at, marked_t15m_at, marked_t30m_at, close_method, method_t15m, method_t30m, dte, volume, open_interest, iv, notes`.

The current `_SCHEMA` already contains columns that also appear on the migrate lists (new DBs get them from CREATE; old DBs get ALTER).

### `_FLAG_MIGRATE_COLS` (`attribution.py:224-255`)

`nlev, nflow, base_score, marked_t1h_at, marked_t1d_at, marked_exp_at, dte, volume, open_interest, iv, mark_close, marked_close_at, close_method, delta, leverage_raw, flow_raw, leverage_norm, flow_norm, extrinsic, realized_vol_20d, iv_premium, pool, mark_t15m, marked_t15m_at, method_t15m, mark_t30m, marked_t30m_at, method_t30m`.

### `_RUN_MIGRATE_COLS` (`attribution.py:257-261`)

`run_kind, optimal_strategy, strategy_outlook`.

### Live `PRAGMA table_info` (read-only)

**runs:** `run_id, ts_et, ticker, n_scored, config_hash, engine_sha, daily_bias, market_state, news_bias, spot, vwap_state, notes, run_kind, optimal_strategy, strategy_outlook`.

**flags:** `flag_id, run_id, ts_et, ticker, side, strike, expiry, score, rank, multipliers, mid, bid, ask, spot, is_control, mark_t1h, mark_t1d, mark_expiry, notes, nlev, nflow, base_score, marked_t1h_at, marked_t1d_at, marked_exp_at, dte, volume, open_interest, iv, mark_close, marked_close_at, close_method, delta, leverage_raw, flow_raw, leverage_norm, flow_norm, extrinsic, realized_vol_20d, iv_premium, pool, mark_t15m, marked_t15m_at, method_t15m, mark_t30m, marked_t30m_at, method_t30m`.

### Three-way

| column | base DDL | migrate list | live DB |
|---|---|---|---|
| All live `runs` columns | `run_kind` is in both base and migrate; `optimal_strategy`, `strategy_outlook` migrate-only | yes | present |
| All live `flags` columns | most mark/score cols now in both base and migrate; `delta` / `leverage_*` / `flow_*` / `extrinsic` / `realized_vol_20d` / `iv_premium` / `pool` migrate-only | yes | present |
| Live column in neither list | — | — | **none** |
| Listed column missing from live | — | — | **none** |

No P0 schema gap on this database.

---

## 4. VIEW `v_outcomes`

Defined in `attribution._VIEW_SQL` (`attribution.py:111-221`). Pass-through columns: `flag_id, run_id, ts_et, ticker, side, strike, expiry, dte, pool, score, rank, nlev, nflow, base_score, multipliers, mid, ask, is_control, mark_* , marked_*_at, close_method, method_t15m, method_t30m`, plus `r.config_hash, engine_sha, daily_bias, market_state, news_bias`.

Derived:

| column | expression (`attribution.py`) | entry | exit |
|---|---|---|---|
| `ret_t15m` | `(mark_t15m - ask) / ask` if ask>0 (`:154-156`) | **ask** | **bid** (stored in `mark_t15m`; `write_mark` `:1115`, `fetch_option_exit` `:1407-1408`) |
| `ret_t30m` | `(mark_t30m - ask) / ask` (`:158-160`) | **ask** | **bid** (same) |
| `ret_t15m_mid` | `(mark_t15m - mid) / mid` (`:163-165`) | mid | bid mark (comparison only, comment `:162`) |
| `ret_t30m_mid` | `(mark_t30m - mid) / mid` (`:167-169`) | mid | bid mark |
| `ret_t1h` | `(mark_t1h - mid) / mid` (`:171-173`) | **mid** | **mid** (`mark_runner.py:748` `fetch_option_mid`) |
| `ret_t1d` | `(mark_t1d - mid) / mid` (`:175-177`) | mid | mid |
| `ret_close` | `(mark_close - mid) / mid` (`:179-181`) | mid | mid (`_fetch_close_mark` `:742`) |
| `ret_expiry` | `(mark_expiry - mid) / mid` (`:183-185`) | mid | intrinsic vs underlying close (`:737`) stored in `mark_expiry` |
| `minutes_t15m` / `minutes_t30m` | `ROUND((julianday(marked_*) - julianday(ts_et))*24*60, 2)` (`:187-194`) | — | — |
| `hours_t1h` / `hours_t1d` / `hours_close` / `hours_expiry` | same with `* 24` (`:195-210`) | — | — |
| `rank_bucket` | CONTROL / UNRANKED / 01-03 / 04-10 / 11-20 / 21+ (`:211-218`) | — | — |

`v_outcomes` does not select `f.bid`. Short-horizon **primary** returns use ask-in / bid-out. t1h / t1d / close / expiry returns use mid-in / mid-out (expiry exit is intrinsic, still in the mid column).

`julianday` on offset-aware `ts_et` is a separate clock question — Unverified.

---

## 5. MARK SEMANTICS

From `mark_runner.py` + `attribution.write_mark`.

**Horizon order** (`mark_runner.py:74-76`): `t15m`, `t30m`, `t1h`, `t1d`, `close`, `expiry`.

**Windows**

| window | bounds (ET) | used for |
|---|---|---|
| live-quote / t1h-t1d | weekday `09:30 <= t < 16:15` (`:59-60`, `:106-114`) | t1h, t1d quotes |
| cash close | `16:00` (`attribution.CASH_CLOSE_TIME` `:35`) | short-horizon due-after-close → seal unavailable |
| session close due | `16:15` (`CLOSE_MARK_TIME` `:32`) | `mark_close` due |
| close-quote | `16:15 <= t < 17:00` (`mark_runner.py:61-62`, `:122-130`) | close marks after 16:15 |

**What is stored**

| horizon | price column | method column | price basis |
|---|---|---|---|
| t15m / t30m | `mark_t15m` / `mark_t30m` | `method_t15m` / `method_t30m` | **bid** (`quote`) or last (`trade`) — never mid (`attribution.py:1115`, `:1407-1408`) |
| t1h / t1d | `mark_t1h` / `mark_t1d` | none | **mid** (`mark_runner.py:748`) |
| close | `mark_close` | `close_method` | mid from `_fetch_close_mark` (`:739`) |
| expiry | `mark_expiry` | none (notes `unavailable:expiry`) | intrinsic (`:737`) |

**Short method values** (`attribution.SHORT_MARK_METHODS` `:38`): `quote`, `trade`, `stale`, `unavailable`.

**Close seal methods** (`CLOSE_SEAL_METHODS` `:40`): `stale`, `unavailable`. Seal writes method + timestamp, **mark left NULL** (`write_mark` `:1116-1118`, `:1181-1200`).

**Sealing rules** (`write_mark` `:1100-1223`): write-once (`IS NULL` guards). Seal-only methods refuse a non-null price (`:1138-1144`). Expiry unavailable: `marked_exp_at` + notes, `mark_expiry` NULL (`:1203-1223`).

**`UNMARKABLE_BEFORE`** = `"2026-08-10"` (`attribution.py:45`). Compared with `substr(ts_et, 1, 10)` / `substr(expiry, 1, 10)` (`:42-44`, SQL `:1293-1313`). Flags / expiries before that date are sealed, not quoted.

---

## 6. TIME AND TIMEZONE CONTRACT

`runs.ts_et` / `flags.ts_et` are written as `ts.astimezone(ET).isoformat(timespec="seconds")` (`attribution.py:673-676`). Example shape: `2026-09-11T16:30:25-04:00` (offset-aware ISO). `write_mark` timestamps use the same (`:1123`).

Archive `timestamp` is `datetime.now(ET).isoformat()` without `timespec="seconds"` (`dailyScaner.py:897,902`) — may include microseconds.

**Rule:** do not use SQLite `date()` / `time()` / `strftime` on these strings. They are reinterpreted as UTC. Helpers:

```
session_date_sql(col) -> substr(col, 1, 10)   # eod_report.py:101-103
et_clock_sql(col)     -> substr(col, 12, 8)   # eod_report.py:106-108
```

`attribution.py:43-44` and `eod_report.py:99-100` state the same rule.

### Group-by-day sites

| site | method |
|---|---|
| `eod_report.ReportFilter.where_sql` / report aggregations | `session_date_sql` (`eod_report.py:127`, `:864`, `:933`, `:1879`) |
| `health_check.py:70`, `:83` | `substr(ts_et, 1, 10)` |
| `attribution.SEAL_UNMARKABLE_*` | `substr(ts_et, 1, 10)` / `substr(expiry, 1, 10)` (`:1299-1312`) |
| `mark_runner.session_date_of` | Python `astimezone(ET).date()` (`mark_runner.py:133-140`) |
| `best_value_archive.filter_today` | first 10 chars of `Run_Timestamp` (`best_value_archive.py:97-109`) |

No production query in `eod_report.py` / `attribution.py` / `health_check.py` uses `date(ts_et)`. `health_check.py:68` comments that `date(ts_et)` shifts post-20:00 ET rows.

---

## 7. VERSIONING CONTRACT

**`config_hash`** (`attribution.py:316-319`): SHA-256 of `json.dumps(cfg, sort_keys=True, separators=(",", ":"))`, first 16 hex chars. Callers pass `SCORING` (`dailyScaner.py:137`). Included: every key in `config.SCORING` (`config.py:13-74`) — weights, multipliers, quality gates, `market_data_source`, mark-runner timeouts. **Not** included: `notify_delivery.py` thresholds (`notify_delivery.py:3-4`, `:15-20`), Streamlit display constants (`best_value_ui.DISPLAY_DELTA_*`), `EARNINGS` in `weekly.py`.

**`engine_sha`:** `attribution.engine_sha()` runs `git rev-parse --short HEAD` (`attribution.py:322-332`), else `"unknown"`. Scan path sets `engine_sha_val=engine_sha()` (`dailyScaner.py:143`).

Read-only: `SELECT COUNT(*), COUNT(engine_sha) FROM runs` → **4118 / 4118**. Zero NULL/empty. Zero `'unknown'`.

Archive `best_value.engine_sha` is **not** this function; it is `config_hash(SCORING)` (`notify_delivery.py:255`).

---

## 8. JOURNAL

**Storage:** `data/journal/YYYY-MM-DD.json` via `scanner.journal_io` (`scanner/journal_io.py:25`, `:38-43`). Writes `FILL_COLS` only (`:29-32`). `Entry_Price` / `PnL_*` are in `LEGACY_DROP` and are not written (`:34-35`, `:6-8`). Reads drop those columns (`:203-207`).

**FIFO:** `scanner.lot_match._match` (`scanner/lot_match.py:140-188`). Sorted by parseable `At`. BUY enqueue; SELL matches oldest lot. Oversell raises (`:158-167`).

**P&L formula** (`_pnl` `:97-99`):

```
PnL_Pct = round((exit - entry) / entry, 4)     # fraction
PnL_Dollars = round((exit - entry) * qty * 100, 2)
```

**Source of truth for realized P&L:** `scanner.lot_match.closed_trades` (`scanner/lot_match.py:5-7`, `:209`). `scanner.attribution.closed_for_day` calls that (`scanner/attribution.py:19-20`). Journal tab (`scanner/journal_view.py:4`) uses the same FIFO and does not read stored event PnL.

**Second paths that also compute P&L**

| path | file:line |
|---|---|
| `script/portfolio_store.close_position` | `(exit-entry)/entry` and `* qty * 100` (`script/portfolio_store.py:395-412`) into `data/portfolio_closed.json` |
| `script/portfolio_store.journal_performance` | sums **stored** `PnL_Dollars` on a ledger frame (`:601-639`); module comment says the Journal tab no longer uses this (`:8-12`, `:524-525`) |
| `scanner.attribution.summarize` | aggregates `closed_trades` output (`:31-63`) — not a second formula, a rollup |
| `scanner.journal_view.metrics_from_match` | sums `closed` `PnL_Dollars` from lot_match (`:81-113`) |

`rebuild_journal.py` / `script/rebuild_journal.py` rewrite day files from broker CSVs (operator tool, not the live SoT).

---

## Unverified

- Whether `save_archive`'s `.txt` sibling is actually written (function returns the name at `dailyScaner.py:950`; no `open(.txt)` in the lines that were read).
- Whether `julianday(ts_et)` in `v_outcomes` mis-buckets offset-aware strings the way `date()` does.
- Exact `gate_history.json` schema (only the filename was read).
- Full `sources/yahoo.py` / `sources/massive.py` fetch bodies (not persistence).

---

## 9. Coverage

| file | read | skimmed | not opened |
|---|---|---|---|
| `attribution.py` | 1–109, 111–261, 316–332, 631–676, 1100–1223, 1290–1314, 1398–1440 | `_ensure_schema` 278–295, `log_run` insert 740–845 | remainder (~700 lines) |
| `scanner/attribution.py` | 1–76 | — | — |
| `scanner/journal_io.py` | 1–80, 189–207, 210 | — | middle parsers |
| `scanner/lot_match.py` | 1–80, 97–99, 140–224 | rollup 225–end | — |
| `scanner/journal_view.py` | 1–4, 81–113 | — | rest |
| `dailyScaner.py` | 395–413, 893–950, 1295–1308 | `run` around 953 | indicators / fetch |
| `snapshot_store.py` | 1–65 | 70–end | — |
| `best_value_archive.py` | 1–73, 97–109 | — | rest |
| `mark_runner.py` | 55–76, 106–130, 720–751 | 154–292, 610–697 | fetch helpers |
| `eod_report.py` | 99–139 | GROUP BY greps | body (~2k lines) |
| `health_check.py` | 67–83 | — | rest |
| `weekly.py` | 495–521 | — | rest |
| `notify_delivery.py` | 1–20, 133–155, 165–257 | — | gates |
| `telegram_bot.py` | 125–155, 376–382, 558–605 | — | rest |
| `app.py` | payload.get greps, 2602, 3993–4000 | — | linear read |
| `scheduler.py` | 1–76 | — | loop body |
| `logging_config.py` | 7, 25, 75–88 | — | — |
| `script/portfolio_store.py` | 1–12, 390–412, 520–677 | — | rest |
| `data_adapter.py` | 18–44 (defs) | — | fetch bodies |
| `sources/base.py` | 1–38 | — | validate_chain |
| `sources/yahoo.py` | 1–13 | — | fetch |
| `sources/massive.py` | 1–25 | — | fetch |
| `sources/fixture.py` | 1–40 | — | rest |
| `config.py` | 13–74 (prior session) | — | — |
| `.gitignore` | 1–22 | — | — |
