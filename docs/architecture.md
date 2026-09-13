# Architecture (as implemented)

`config_hash(SCORING)` at documentation: `243ecda68cfc8618`.

Evidence-only. Line refs are the lines that were read. Claims without a line sit under `## Unverified`.

---

## 1. PROCESS MODEL

Long-running / scheduled processes observed in `plist/`, wrappers, and entry points. Intervals are taken from the plist keys or from the process’s own sleep — not inferred from comments alone.

| process | what starts it | cadence (as configured) | entry | logs | exit codes that were read |
|---|---|---|---|---|---|
| Scheduler | launchd `plist/com.optiontrading.scheduler.plist` (`KeepAlive` true `:10`, `RunAtLoad` true `:19`); also `app.py` `_ensure_services` (`:831-870`) `Popen`s `scheduler.py` | Plist has **no** `StartInterval`. Loop sleeps **30 s** (`scheduler.py:524`). Per-ticker scan interval from `scheduler_config.json` (`default_interval_min` 5, `AAPL.interval_min` 5). Market window `09:30` → `eod_time` `16:20` ET (`scheduler_config.json:2-4`, `scheduler.py:132-157`). Closed-market sleep **60 s** (`:473-474`). | `scheduler.py` `__main__` → `main` (`:533-540`) | plist stdout/stderr → `logs/scheduler.log` (`plist:21-24`); `setup_logging("scheduler")` (`scheduler.py:61`) | Duplicate instance: `sys.exit(0)` (`:418-428`, `:438`). Scan child: 0 / 3 / 1 (`run_scan` `:312-314`). Timeout / exception mapped to **1** (`:343-350`). `SIGTERM`/`SIGINT` → `sys.exit(0)` (`:435-438`) |
| Health check | launchd `plist/com.optiontrading.health-check.plist` | `StartCalendarInterval`: every local hour at **minute 30** (`:31-177`). Args `--require-et 16:30` `--et-window-min 20` (`:25-28`) skip unless ET clock is in that window (`health_check.py:189-196`). Plist sets `TZ=America/New_York` (`:186-187`) | `health_check.py` `main` (`:166`) | `logs/health_check.log` (`plist:178-181`) | Outside ET window: **0** (`:196`). Missing DB or any failed `_checks` row: **1** (`:200-220`). All pass: **0** (`:222`) |
| Nightly | launchd `plist/com.optiontrading.nightly.plist` | `StartCalendarInterval` weekday **1–5**, Hour **17**, Minute **15** (`:25-31`). No `TZ` key in this plist. Script header says “17:15 ET” (`nightly.sh:3`) | `/bin/bash nightly.sh` (`plist:9-12`) | launchd: `logs/nightly.launchd.log` (`plist:34-37`); script: `logs/nightly.log` (`nightly.sh:14`) | DB missing: `exit 1` (`nightly.sh:31-32`). `cd` fail: `exit 1` (`:58`). Report failure is logged; script does **not** `exit` on report fail (`:60-64`, `set -uo pipefail` without `-e` `:8`) |
| Mark runner | launchd `plist/com.optiontrading.mark-runner.plist` | `StartInterval` **900** seconds (`:20-21`) | `mark_runner.py` `main` (`:944`) | `logs/mark_runner.log` (`plist:22-25`) | `main` returns **0** on success and on runtime-cap early exit (`:1019`, `:1048-1049`). `__main__` `sys.exit(main())` (`:1054-1055`) |
| Weekly scanner (template) | launchd `plist/com.zmax.scanner.weekly.plist` | Sunday `Weekday` **0**, Hour **18**, Minute **0** (`:19-24`) | `/bin/bash` + **placeholder** path `/Users/YOURUSER/trading/scanner/run_scanner.sh weekly` (`:11-14`) | `/tmp/com.zmax.scanner.weekly.{out,err}` (`:26-29`) | Wrapper: unknown mode `exit 1` (`run_scanner.sh:56`); weekend daily skip `exit 0` (`:39`). Scanner failure is recorded as `STATUS=FAILED` and does **not** change the shell exit (`:60-64`) |
| Streamlit UI | not in any read plist. Process is `app.py` loaded by Streamlit (`:5906-5907` always calls `main()`) | user-driven; sidebar can `subprocess.run` `dailyScaner.py` (`:670-686`, timeout 1800 s) | `app.py` `main` (`:5835`) | `setup_logging` after imports (`:104` region not fully read); scanner child output shown in UI (`:951`) | UI treats scanner success as `returncode == 0` only (`:686`) |
| Telegram bot (interactive) | `app.py` `_ensure_services` (`:844-870`); also `python telegram_bot.py` | long-poll `getUpdates` `timeout=25` (`telegram_bot.py:910-914`); fail sleep 5 s (`:918`) | `telegram_bot.py` `main` (`:897`) | `logs/telegram_bot.log` (`:39`) | Missing token or chat allow-list: `sys.exit(1)` (`:72-78`). `getMe` fail: `sys.exit(1)` (`:899-901`) |
| `dailyScaner.py` (child) | scheduler `subprocess.run` (`scheduler.py:327-334`); Streamlit `_run_daily_scanner` (`app.py:677-683`); `run_scanner.sh` daily (`:54`) | as parent | `run` (`dailyScaner.py:953`); `__main__` (`:1373-1389`) | `OPTIONTRADING_PROCESS` env (`:957-958`); scheduler sets `"scheduler"` (`scheduler.py:325`) | `ScanRefused` → **3** (`:1381-1383`). `MassiveChainTruncatedError` / `ValueError` → **1** (`:1384-1389`) |
| `weekly.py` | `run_scanner.sh weekly` (`:55`); weekly plist (placeholder path) | as parent | `weekly.py` `run` (`:723`); `__main__` (`:776-778`) | `OPTIONTRADING_PROCESS` or `"scheduler"` (`:777`) | `__main__` has no `sys.exit`; uncaught exception is a non-zero interpreter exit |
| `eod_report.py` | `nightly.sh` weekday daily (`:60`); Friday also `--days 15` (`:69-74`) | as parent | `eod_report.py` `main` (`:2228`) | appended to `logs/nightly.log` | `main` returns **1** when `section_coverage` is false (`:2285-2295`); **0** after full write (`:2333`) |

`SCANNER_DB` is set to `data/attribution.db` in scheduler / mark-runner / health-check / nightly plists.

---

## 2. CONTROL FLOW

Scan path that writes archive + attribution + (maybe) Telegram. Hops are the scheduler-driven path.

1. **Trigger.** launchd keeps `scheduler.py` alive (`plist/com.optiontrading.scheduler.plist:10,19`). Loop (`scheduler.py:449`) reloads config, discovers tickers from `archive/*_*.json` minus `tickers_excluded.json` (`:120-124`). Intraday: `market_is_open` (`:132-157`) and interval due (`:500-515`). EOD: `should_run_eod` → `eod_settlement.is_eod_slot` (`scheduler.py:160-163`, `eod_settlement.py:106-113`) once per ticker per ET day (`scheduler.py:485-498`).
2. **Subprocess.** `run_scan` runs `[python, dailyScaner.py, ticker]` plus `--eod` when EOD (`scheduler.py:318-322`). `cwd` repo root, `timeout` 300 s intraday / 2400 s EOD (`:305-306`, `:489-490`). Env `OPTIONTRADING_PROCESS=scheduler` (`:325`).
3. **Fetch.** `dailyScaner.run` (`:953`) constructs `MarketDataSource` (`:960-961`) or uses the CLI `--source` object (`:1378-1380`). Intraday: `fetch_data` (`:1010`). EOD: `await_volume_convergence` polls `fetch_data` (`:964-998`, `eod_settlement.py:44-82`); defaults `gap_sec=600`, `max_attempts=3` overridable from `scheduler_config.json` (`dailyScaner.py:966-971`).
4. **Analyze / previous archive.** `analyze_tf` / `opening_range` (`:1013-1014`). `load_previous_report` + `diff_reports` (`:1017-1033`). Direction from `direction_score` (`:1038`).
5. **Gates (can abort).** Rollover / quality / stale — see §3. Quality and majority-stale abort **before** the success archive. Rollover abort writes a **flagged** archive then aborts (`:1126-1159`).
6. **Score (archive snapshot).** After gates pass, `build_best_value_df` + `serialize_best_value_rows` (`:1270-1295`, `notify_delivery.py:165`). Failure here is logged; `_bv_payload` becomes `None` (`:1296-1298`). Scan continues.
7. **Archive write.** `save_archive(..., best_value=_bv_payload)` (`:1300-1309`). Text report written later (`:1366-1367`).
8. **Attribution write.** `_log_scan_attribution` → `build_best_value_df` + `log_run` (`:69-146`, called `:1328-1343`). Docstring: fail-soft, never abort (`:87-88`). VWAP omitted on this path (`:132-133`).
9. **Process exit.** Success falls off `run()` with no `sys.exit`. `__main__` maps `ScanRefused` → 3, listed errors → 1 (`:1373-1389`).
10. **Notify decision.** `handle_scan_result` (`scheduler.py:259-301`):
    - **exit 0** → reset refusal streak; if `notify_telegram` then `_notify_success` (`:277-281`).
    - **exit 3** → increment streak; skip success notify; one streak alert when `streak == REFUSAL_STREAK_ALERT_AT` (3) (`:284-296`, `notify_delivery.py:20`).
    - **else** → log FAILED; no Telegram (`:298-301`).
    `_notify_success` loads the latest archive via `telegram_bot._load_latest` and formats with `_fmt_report` (`scheduler.py:198-220`). Empty text skips send (`:221-223`).

Streamlit “Run Scan” is a parallel trigger (`app.py:670-686`) that treats only `returncode == 0` as success (`:686`). It does not call `handle_scan_result`.

---

## 3. ABORT PATHS

`abort_scan` prints `ABORT_REASON={reason}` to stderr and raises `ScanRefused` (`dailyScaner.py:33-36`). `__main__` exits **3** (`:1381-1383`). Scheduler parses the marker (`notify_delivery.py:23-31`, `scheduler.py:336`).

| reason / condition | threshold | config key | log / stderr | archive / attribution | exit |
|---|---|---|---|---|---|
| `volume_rollover` | Evaluated only when `rollover_detectors_active` (not session-scoped volume) (`chain_quality.py:416-421`, `dailyScaner.py:1082-1085`). Need ≥ `MIN_CHAIN_ROLLOVER_MATCHES` **10** matched (side, strike, expiry) (`chain_quality.py:215,325-327`), ≥1 CALL and ≥1 PUT (`:341-343`), **strict majority** of matched CALLs **and** PUTs each decreased (`:271-287`, `:345-347`). Circuit breaker: after `MAX_CONSECUTIVE_CHAIN_ROLLOVER_ABORTS` **3**, further hits disable the guard for the session and **do not** abort (`:217`, `:410-412`, `dailyScaner.py:1116-1124`) | Match floor is a module constant, not a `SCORING` key. Detectors dormant when `source.volume_is_session_scoped` (`dailyScaner.py:1061,1077-1081`) | `ABORT: chain volume rollover detected...` (`:1147-1158`); `ABORT_REASON=volume_rollover` | Flagged `save_archive(..., chain_volume_rollover=True)` (`:1128-1138`); text `CHAIN VOLUME ROLLOVER - attribution skipped` (`:1141-1145`); **no** `_log_scan_attribution` | 3 |
| `quality_gate` | Top-N calls (`quality_top_n` default 30). Prefer `dte>=1` sample when that sample has `>= max(5, n//3)` rows (`chain_quality.py:151-166`). Fail if call `total > 0` and `unusable/total > max_unusable_frac` default **0.20** (`:179-182`). Per-contract: `iv >= min_iv_usable` (default 0.01), `dte >= 0`, and (`bid>0` and `ask>0`) when `provides_quotes`, else `last>0` (`:41-67`) | `quality_top_n`, `max_unusable_frac`, `min_iv_usable` via `SCORING` (`:150-152`, `:41`) | `ABORT: chain quality gate failed...` (`dailyScaner.py:1173-1180`); `ABORT_REASON=quality_gate` | **No** archive, **no** attribution (`:1179`) | 3 |
| `majority_stale` | Only if EOD archive found and `stale_check_active` (ET clock **before** `stale_check_cutoff_et` default `11:00`) (`chain_quality.py:532-546`, `dailyScaner.py:1197-1219`). Abort when `n_stale/n_tot > stale_majority_abort_frac` default **0.50** (`chain_quality.py:707-718`). `n_total<=0` → False (`:716-717`) | `stale_check_cutoff_et`, `stale_majority_abort_frac` | `ABORT: majority stale volume ({n}/{n} > 50%)` (`dailyScaner.py:1220-1224`); `ABORT_REASON=majority_stale` | **No** archive, **no** attribution (`:1223`) | 3 |
| `MassiveChainTruncatedError` | raised from Massive fetch (`sources/massive.py:35` class; caught in `__main__`) | — | `ABORT: {e}` (`dailyScaner.py:1385`) | scan did not complete `run()` | 1 |
| `ValueError` from `fetch_data` | empty 5m history (`:466-471`); no spot and no daily close (`:506-508`); empty call or put chain (`:517-522`) | — | `ERROR: {e}` (`:1388`) | none | 1 |
| `TypeError` from `fetch_data` | source is not a `MarketDataSource` (`:458-459`) | — | uncaught in `__main__` (not in the `except` list `:1381-1389`) | none | interpreter non-zero |
| Scheduler timeout | `subprocess.TimeoutExpired` (`scheduler.py:343-346`) | `timeout` arg 300 / 2400 | `scan TIMED OUT` | child killed; no notify (`handle_scan_result` sees rc 1) | treated as **1** |
| Scheduler spawn error | any `Exception` in `run_scan` (`:347-350`) | — | `scan error` | — | treated as **1** |

Not a scan abort:

- Quality/stale **skipped** (no prior EOD, past cutoff, dormant session-scoped source) logs WARN/GRAY and continues (`dailyScaner.py:1077-1081`, `:1192-1200`).
- `_log_scan_attribution` and archive `best_value` snapshot exceptions are fail-soft (`:87-88`, `:1296-1298`).
- `spread_gate.evaluate_spread_gate` is used by `weekly.py` (`:749`) and the Streamlit Spread Gate tab (`app.py:5407`). `NO-TRADE` does not call `abort_scan`. Failure-to-evaluate is a block on that gate (`spread_gate.py:5-6`), not a scanner exit.
- `notify_delivery.ranking_has_signal` / `archive_is_fresh` / `provenance_line` omit or annotate Telegram Best Value (`telegram_bot.py:396-400`, `:558-581`, `:588-594`). They do not abort the scan.

`abort_scan` call sites in `dailyScaner.py` are only `:1159`, `:1181`, `:1225`. Tests name those three reasons (`tests/test_notify_gates.py` grep).

---

## 4. LAYER BOUNDARIES

### yfinance

Allowlist in `tests/test_import_graph.py:12-15`: `sources/yahoo.py`, `news_service.py`. AST walk (`:36-50`). Skip list includes `dashboard.py` (`:30-31`).

Direct `import yfinance` found at:

- `sources/yahoo.py:24`
- `news_service.py:222` (news fallback; docstring `:215-217`)
- `dashboard.py:59` (skipped by the test)

`test_app_does_not_import_yfinance_directly` (`tests/test_import_graph.py:75-79`) asserts `app.py` has no yfinance import. Grep of `app.py` imports (`:7-100`) shows no `yfinance`.

### Other network libraries (scoped files)

| module | import | role |
|---|---|---|
| `sources/yahoo.py` | `yfinance` `:24` | `MarketDataSource` |
| `sources/massive.py` | `urllib.request` `:14-16` | Massive HTTP |
| `news_service.py` | `finnhub` `:164`; `yfinance` `:222` | headlines |
| `scheduler.py` | `urllib.request` `:176-183` | Telegram `sendMessage` |
| `telegram_bot.py` | `urllib.request` (`_call` `:87-90`); no yfinance | Bot API |
| `app.py` | `urllib.request` `:16`; Telegram helper `:373` | UI send |
| `schwab.py` | `requests` `:31` | not on the yfinance allowlist; not in Session 4 “FILES IN SCOPE” beyond this grep |
| `data_adapter.py` | no yfinance/requests (`:1-16`); `MarketDataSource` only (`:15`) | dashboard fetch adapter |
| `volume_analysis.py` | no network import in `:1-20` | — |
| `spread_gate.py` | `optionlab` `:9` | pricing, not market HTTP |
| `chain_quality.py` | no network | gates |
| `notify_delivery.py` | no network | delivery predicates |

`dailyScaner.fetch_data` talks only to `MarketDataSource` (`:447-524`).

---

## 5. UI SURFACE

`main` (`app.py:5835-5903`) renders a page radio from `_main_tab_labels` (`:214-232`):

| page | render fn | backing modules (imports / calls that were read) |
|---|---|---|
| Options Flow | `_render_tab1` (`:3411`) | latest `archive/{ticker}_*.json` (`:3415-3424`); magnets / expiry / cost tabs (`:3713`); Best Value zone `_render_best_value_panel` (`:3687-3705`) |
| Scanner Archive | `_render_tab2` (`:4803`) | `best_value_archive` (`:4807`); archive JSON expiry tables (`:4811-4840`) |
| Spread Gate | `_render_tab3` (`:5360`) | `evaluate_spread_gate` (`:5407-5421`) |
| Tickers | `_render_tab4` (`:5168`) | `_run_daily_scanner` (`:5200`); `tickers_excluded.json` (`:5173`) |
| Market News | `_render_tab5` (`:5506`) | `news_service.get_market_news` via `_cached_market_news` (`:5514`) |
| Journal → Trade log | `_render_tab_journal` (`:5585`) | `scanner.journal_view` + `try_match` (`:91-99`, `:5594-5596`) |
| Journal → Pre-Trade Check | `pre_trade_check.render_pre_trade_page` (`:5894`) | `pre_trade_check` (`:89`) |

Sidebar (`_sidebar` `:911`): ticker select, manual scan, Flow filters (`min_dte`, `top_n`, sort) (`:960-975`), `_ensure_services` (`:957`).

Module docstring (`app.py:1-4`) states the dashboard is display-only and “No indicators recomputed here.” The Best Value zone calls `build_best_value_df` (see findings).

### Ranked-table dataframe path

1. `_render_tab1` passes archive `vol` / `prev_vol` into `_render_best_value_panel` (`:3687-3705`).
2. `_build_best_value_df` (`:1618-1653`) forwards to `best_value.build_best_value_df` (`:1638`) with VWAP / POV / catalyst / 0DTE kwargs (`:1644-1652`).
3. Empty or all-NaN `Value_Score` returns (`:2437-2448`).
4. Session velocity / action / target columns added (`:2452-2573`).
5. Global `sort_values("Value_Score", ascending=False).head(show_n)` (`:2594-2598`). `show_n` is sidebar `top_n` clamped 1–30 (`:2593`).
6. `log_best_value_run(top5, ...)` (`:2602`) — UI ledger, not `attribution.log_run`.
7. `greeks_display_columns(top5, vol_curr)` (`:2656`) — provider chain greeks.
8. `format_exit_by_cell` (`:2712`).
9. Display filter: `filter_ranked_display` / `ranked_delta_band_mask` unless “Show all ranked contracts” (`:2719-2737`). Caption: default hides outside `|δ|` 0.35–0.50 including missing delta (`:2723-2724`).
10. `_render_best_value_table_with_plus(ticker, vis_top5, vis_disp, ...)` (`:2740`).

Attribution is not invoked from the ranges of `app.py` that were read.

---

## 6. DELIVERY

Two Telegram surfaces:

**A. Scheduler success notify** (`scheduler.py:190-220`): after exit 0 only. `telegram_bot._load_latest(ticker)` (`:198-199`) reads `archive/{ticker}_*.json` (`telegram_bot.py:132-134`). `_fmt_report` with `best_value=True` and several other sections (`scheduler.py:206-218`).

**B. Interactive bot** (`telegram_bot.py:897+`): `getUpdates` loop; sections built by the same `_fmt_report`.

Best Value inside `_fmt_report` (`:558-559`):

```
# ── Best Value Option (read from scan archive — never re-score here) ───────
bv = payload.get("best_value")
```

No `calculate_best_value` / `build_best_value_df` call in the lines that were read. Rows come from the archive payload. Additional gates on that payload:

- `archive_is_fresh` (`:561`, `notify_delivery.py:49-60`): age ≤ `ARCHIVE_MAX_AGE_MIN` **15**; `chain_volume_rollover` → not fresh.
- `provenance_line(bv, payload)` (`telegram_bot.py:575`, `notify_delivery.py:133-145`): missing `n_scored` / `flow_dispersion` / engine / timestamp → omit section.
- per-pool `ranking_has_signal` (`telegram_bot.py:588`, `notify_delivery.py:80-130`): `MIN_SCORED_FOR_RANKING` 5, `MIN_NONNULL_DVOL` 3, `MIN_FLOW_IQR` 0.05 (`:16-19`).

`best_value.py` module docstring claiming Telegram imports the scoring engine is the existing F-S2-01 discrepancy.

---

## 7. REPORTING

`eod_report.main` (`:2228`) builds one `ReportDoc`, then writes `.txt` + `.html`.

Order when coverage passes (`:2285-2317`):

| # | call | section title / gate |
|---|---|---|
| 1 | `section_coverage` `:2285` (`:1188`) | `COVERAGE`. **Halt:** `COUNT(*)` flags on `v_outcomes` is 0 → `return False` (`:1207-1209`). `main` writes the partial doc and **returns 1** (`:2285-2295`). There is **no** percentage-coverage threshold in the lines that were read. `engines > 1` only adds a warning line (`:1223-1227`) and still returns True |
| 2 | `section_short_buckets` t15m `:2298` | `OUTCOMES @ T15M` (`:1307-1312`). Empty usable obs → lines, no process exit |
| 3 | `section_short_buckets` t30m `:2299` | same for T30M |
| 4 | `section_buckets` t1h `:2300` | `OUTCOMES @ T1H` (`:1350-1368`) |
| 5 | `section_buckets` t1d `:2301` | `OUTCOMES @ T1D` |
| 6 | `section_verdict` `:2302` | `VERDICT` (`:1401`). Missing TOP3/CONTROL → “insufficient data” (`:1405-1407`), no `return 1` |
| 7 | `section_paper_strategy` `:2303` | `PAPER STRATEGY` (`:1660`) |
| 8 | `section_factor_separation` `:2304` | `FACTOR SEPARATION` (`:1948`) |
| 9 | `section_charts` `:2312` | HTML `CHARTS` (`:1032`) |
| 10 | `section_flags` `:2313` | `DATA-QUALITY FLAGS` (`:2106`) |
| 11 | `SAVED` footer `:2315-2316` | output path |

`nightly.sh` then logs weekday ALERTs if today’s `runs` / `eod` / `mark_close` / `mark_t1h` counts are 0 (`:91-96`). Those ALERTs do not `exit 1`.

---

## Unverified

- Whether these launchd plists are loaded on this machine (`launchctl list` not run).
- Whether `com.zmax.scanner.weekly` is installed; the ProgramArguments path is a placeholder (`plist/com.zmax.scanner.weekly.plist:13`).
- `volume_analysis.py` fetch path after `:20` (who calls `MarketDataSource`).
- `news_service._fetch_finnhub_articles` beyond the `import finnhub` line (`:157-169`).
- `mark_runner.py` marking loop (`:19-943`) except `main` horizon selection (`:968-983`).
- `eod_report.py` SVG / paper-trade internals (`:367-1023`, `:1450-1933`).
- Interactive Telegram handlers after `_fmt_report` (`telegram_bot.py:631-896`).
- `save_archive` body (`dailyScaner.py:893+`) except the call sites.
- `app.py` zones between `:3441-3686` and `:3714-4802` (magnets / expiry / cost tab bodies).

---

## Coverage

| file | read | skimmed (grep/symbol) | not opened |
|---|---|---|---|
| `dailyScaner.py` | 1–55, 69–148, 447–524, 953–1226, 1270–1343, 1373–1389 | 527–536 (`analyze_tf`), 893 (signature), 631 | 56–68, 149–446, 525–892, 1227–1269, 1344–1372 |
| `scheduler.py` | 1–80, 120–157, 160–350, 430–540 | 407–428 (pid lock exits) | 81–119, 351–406 |
| `app.py` | 1–100, 214–241, 670–691, 831–908, 911–975, 1618–1661, 2308–2343, 2388–2757, 3411–3440, 3687–3705, 4803–4840, 5168–5207, 5360–5438, 5506–5545, 5585–5624, 5835–5907 | `^def` / `st.tabs` / imports; `:110` `page_title` | remainder (~228KB): 101–213, 242–669, 692–830, 976–1617, 1662–2307, 2344–2387, 2758–3410, 3441–3686, 3706–4802, 4841–5167, 5208–5359, 5439–5505, 5546–5584, 5625–5834 |
| `telegram_bot.py` | 1–80, 132–134, 367–630, 897–920 | 181, 719 | 81–131, 135–366, 631–896, 921–end |
| `notify_delivery.py` | 1–160, 165 (signature) | 255 (`engine_sha` from Session 3) | 161–164, 166–end except known serialize |
| `mark_runner.py` | 1–18, 944–1055 | horizon names in docstring | 19–943 |
| `health_check.py` | 50–137, 166–226 | `:48` window helper | 1–49, 138–165 |
| `eod_settlement.py` | 44–82, 91–113 | — | 1–43, 114–end |
| `eod_report.py` | 1024–1036, 1188–1228, 1296–1320, 1350–1374, 1399–1418, 1650–1666, 1935–1953, 2101–2118, 2228–2337 | `^def` list | ~1–1023, 1037–1187, 1229–1295, 1321–1349, 1375–1398, 1419–1649, 1667–1934, 1954–2100, 2119–2227 |
| `weekly.py` | 723–778 | — | 1–722 |
| `chain_quality.py` | 1–191, 215–217, 271–421, 532–546, 707–718 | 194–214, 422–531, 547–706 | remainder after 718 |
| `spread_gate.py` | 1–26 | — | 27–end |
| `volume_analysis.py` | 1–20 | — | 21–end |
| `news_service.py` | 1–14, 157–169, 211–228 | — | 15–156, 170–210, 229–end |
| `data_adapter.py` | 1–40 | — | 41–end |
| `plist/com.optiontrading.scheduler.plist` | all | — | — |
| `plist/com.optiontrading.health-check.plist` | all | — | — |
| `plist/com.optiontrading.nightly.plist` | all | — | — |
| `plist/com.optiontrading.mark-runner.plist` | all | — | — |
| `plist/com.zmax.scanner.weekly.plist` | all | — | — |
| `nightly.sh` | all (1–100) | — | — |
| `run_scanner.sh` | all (1–80) | — | — |
| `tests/test_import_graph.py` | 1–79 | — | — |
| `sources/yahoo.py` | `:24` only | — | rest |
| `sources/massive.py` | `:14-16`, `:35` | — | rest |
| `schwab.py` | `:31` only | — | rest |
| `dashboard.py` | `:59` only | — | rest |
