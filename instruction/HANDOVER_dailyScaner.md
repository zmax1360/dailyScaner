# dailyScaner — Handover Document

**Repo:** `github.com/zmax1360/dailyScaner`
**Local working copy:** `~/works/optionTrading`
**Owner:** Ali Zafari Anaraki
**Document date:** 2026-09-11
**Basis:** written against `origin/main` at commit `00a96db` ("Show provider Delta and Theta/Prem on the scanner table without changing ranking"), plus findings from measurement sessions Aug–Sep 2026.

> **Read this first.** The repository on GitHub is **not** the system that produced the analysis in this document. Nine files that do real work — including the report generator — exist only on the local machine and are untracked. See §10. Anyone taking this over from GitHub alone gets a scanner with no reporting layer.

---

## 1. What the system does

dailyScaner is a **systematic options scanner and measurement harness** for short-dated equity options — currently AAPL (NVDA added at one point, not in the live scheduler config).

It does four things:

1. **Scans** the options chain on a fixed interval during market hours, computes per-contract features, and ranks contracts by a composite "Best Value" score.
2. **Publishes** the ranked list to a Streamlit dashboard and a Telegram channel.
3. **Records** every scored contract into an attribution database at the moment it was flagged — entry price, score, rank, multipliers, greeks — then goes back later and marks the actual traded price at six forward horizons.
4. **Reports** on whether the ranking had any predictive value, using pre-registered buckets and an ATM control group.

Point 4 is the reason the project exists in its current form. The scanner existed from v1 through v18 with no outcome measurement at all; the attribution layer was added after it became clear there was no way to tell whether any of it worked.

**Design philosophy:** reactive, not predictive. The engine does not forecast direction. It ranks contracts already showing flow and leverage characteristics, then applies context multipliers (daily bias, macro, news, VWAP state, strategy outlook).

**Trading profile it was built for:** 0DTE and 1DTE+ AAPL contracts, 09:45–11:30 and 14:00–15:30 ET entry windows. Note the structural conflict recorded in §9 — the owner works full-time, which makes 0DTE monitoring impractical regardless of what the engine says.

---

## 2. Architecture

### 2.1 Layer separation

The hard rule of the codebase: **acquisition may touch the network; scoring may not; the UI may never import `yfinance`.**

| Layer | Modules | Network? |
|---|---|---|
| Acquisition & compute | `dailyScaner.py`, `data_adapter.py`, `news_service.py`, `volume_analysis.py`, `sources/` | Yes |
| Pure scoring / math | `best_value.py`, `strategy_engine.py`, `zero_dte_gex.py`, `scoring_pool.py`, `greeks.py`, `cost_distribution.py`, `pov_leakage.py` | **No** |
| Persistence | `attribution.py`, `scanner/attribution.py`, `snapshot_store.py`, `best_value_archive.py`, `portfolio_store.py`, `scanner/journal_io.py` | No |
| UI / delivery | `app.py` (Streamlit), `telegram_bot.py`, `notify_delivery.py`, `dashboard.py`, `best_value_ui.py` | Never (app.py) |
| Services | `scheduler.py`, `mark_runner.py`, `health_check.py`, `nightly.sh`, `eod_settlement.py` | Yes |
| Reporting | `eod_report.py` | No |

The contract between acquisition and display is **archive JSON**: `archive/{TICKER}_{YYYYMMDD}_{HHMM}.json`. The scanner writes it; the UI and bot read it. Live-only overlays (VWAP reclaim, POV leakage, cost distribution, news sentiment, macro) are *not* in the archive and are fetched fresh.

### 2.2 Runtime flow

```
scheduler.py  (launchd, market hours, 5-min interval per scheduler_config.json)
   └─ subprocess: dailyScaner.py AAPL
        ├─ fetch chain + OHLC  (sources/ adapter: yahoo | massive | fixture)
        ├─ chain_quality.py    → ABORT if >20% of top 30 contracts unusable
        ├─ stale-volume check  → ABORT if ≥50% of contracts show stale volume before 11:00 ET
        ├─ rollover detector   → ABORT if matched call/put volume decreases within a session
        ├─ compute features, score, rank        (best_value.calculate_best_value)
        ├─ write archive/*.json
        └─ write runs + flags rows              (attribution.log_run)
   └─ notify Telegram  ONLY if scan exit code == 0

mark_runner.py  (launchd, every 900s)
   └─ for each unmarked flag past its horizon: fetch quote, write mark
      horizons in priority order: t15m → t30m → t1h → t1d → close → expiry
      window 09:30–16:15 ET; wall-clock cap 600s

nightly.sh   (launchd, weekdays 17:15) → EOD settlement + report
health_check.py (launchd, hourly, gated to 16:30 ET) → alerts if t1h marks older than 90 min during RTH
weekly.py    (launchd, Sunday) → weekly/earnings-oriented scan path
```

### 2.3 Data sources

`sources/` is an adapter layer with three implementations: `yahoo.py` (current default, `market_data_source: "yahoo"`), `massive.py` (paid snapshot API, paginated, near-ATM window ±6%, max 20 strikes/expiry), and `fixture.py` (offline test data). `schwab.py` exists with OAuth wiring but is not in the adapter path.

Switching source does **not** change scoring math, but it does change the rollover-detection semantics — hence `test_source_aware_rollover.py`.

### 2.4 Defensive guards (these were all added after real failures)

| Guard | Why it exists |
|---|---|
| Chain quality gate | Aug 5: 19/19 top calls had zero bid/ask; scanner scored garbage |
| Rollover detector | Yahoo returns previous-session volume early in the session; created a feedback loop that aborted 80 of 81 scans on Aug 5 |
| Stale volume check | Same class of failure, cutoff-bounded to before 11:00 ET |
| Non-zero exit on abort | The scheduler logged `scan OK` after an ABORT and notified anyway |
| Notify gated on exit code | Telegram sent 81 alerts on a day with 1 successful scan |
| Bot reads archive, not its own fetch | `telegram_bot.py` used to call `build_best_value_df` independently, bypassing every gate above |
| launchd instead of terminal processes | Scheduler died with the terminal session; EOD silently did not run for six consecutive days |
| mark_runner runtime cap | Marker deadlocked 5.5 hours fetching quotes for expired contracts |

---

## 3. Database

**SQLite, WAL mode**, at `data/attribution.db`. Two tables and one view. Schema lives in `attribution.py` (`_SCHEMA`, `_VIEW_SQL`) with additive `ALTER TABLE` migrations in `_FLAG_MIGRATE_COLS` / `_RUN_MIGRATE_COLS`.

### 3.1 `runs` — one row per scan

| Column | Notes |
|---|---|
| `run_id` | PK |
| `ts_et` | ISO, offset-aware. **Never use SQLite `date()` on this** — see §8 |
| `ticker`, `n_scored`, `spot` | |
| `config_hash` | Scoring fingerprint. **All analysis must be segmented by this** |
| `engine_sha` | Column exists, **not populated** — version attribution is impossible retroactively |
| `daily_bias`, `market_state`, `news_bias`, `vwap_state` | Context inputs that fed the multipliers |
| `run_kind` | `intraday` \| eod |
| `optimal_strategy`, `strategy_outlook` | From `strategy_engine` |

### 3.2 `flags` — one row per scored contract per scan

Identity: `run_id`, `ts_et`, `ticker`, `side`, `strike`, `expiry`, `dte`, `pool`.

Scoring snapshot: `score`, `rank`, `base_score`, `nlev`, `nflow`, `multipliers` (JSON), `is_control`.

Scoring inputs (instrumentation, do not affect ranking): `delta`, `leverage_raw`, `flow_raw`, `leverage_norm`, `flow_norm`, `extrinsic`, `realized_vol_20d`, `iv_premium`, `iv`, `volume`, `open_interest`.

Entry basis: `mid`, `bid`, `ask`, `spot`.

Outcome marks — **these store prices, not returns**:

| Mark | Marked-at | Method |
|---|---|---|
| `mark_t15m` | `marked_t15m_at` | `method_t15m` — live **bid** (exit fill) |
| `mark_t30m` | `marked_t30m_at` | `method_t30m` — live **bid** |
| `mark_t1h` | `marked_t1h_at` | mid |
| `mark_t1d` | `marked_t1d_at` | mid |
| `mark_close` | `marked_close_at` | `close_method` — mid, with 0DTE intrinsic fallback; sealed `stale`/`unavailable` |
| `mark_expiry` | `marked_exp_at` | intrinsic |

`is_control` flags an ATM control pair per pool — the benchmark the engine has to beat.

### 3.3 `v_outcomes` — the view you should actually query

Joins `flags` to `runs` and computes returns:

- `ret_t15m`, `ret_t30m` = **(mark − ask) / ask** — ask entry, bid exit. This is the realistic one.
- `ret_t15m_mid`, `ret_t30m_mid` = mid-based, kept for comparison only.
- `ret_t1h`, `ret_t1d`, `ret_close`, `ret_expiry` = mid-to-mark.
- `minutes_t15m`, `minutes_t30m`, `hours_t1h` = actual elapsed time, so you can filter late marks.

**`UNMARKABLE_BEFORE = 2026-08-10`.** Live option quotes cannot recover sessions before this. Any query touching short-horizon marks must be restricted accordingly.

### 3.4 Other stores

- `archive/*.json`, `archive_weekly/*.json` — scan payloads
- `data/best_value_archive.csv` — top hits across refreshes
- `data/journal/*.json` — trade journal, FIFO-matched via `scanner/journal_io.py` + `scanner/lot_match.py`
- Pre-Trade Check records — one per check, logged from Aug 27 forward

---

## 4. How "Best Value" is calculated

Entry point: `best_value.calculate_best_value()` — a pure function, no IO, no Streamlit. Every literal that shapes the score lives in `config.py::SCORING` and is hashed into `config_hash`.

### 4.1 Universe gates (before scoring)

- `min_volume` = 500
- `min_last` = 0.01
- `min_oi_for_magnet` = 500
- Expired contracts always dropped; after 16:15 ET same-day 0DTE also dropped
- Per-contract quality: `min_iv_usable` 0.01, `quality_top_n` 30, `max_unusable_frac` 0.20

### 4.2 Features

**Delta** — Black-Scholes, computed in `greeks.bs_delta`, `r = 0.045` fixed. Never defaults to 0.5. Null delta excludes the row from ranking entirely (fix F-01: before this, "leverage" was literally inverse price).

**Leverage (raw):**
```
leverage_raw = |delta| × spot / last
```
Absolute delta is deliberate (engine-v1.1). Signed put delta previously floored the entire put universe at the min-max minimum on 40% of the score.

**Flow (raw):**
```
voi   = volume / max(open_interest, 1)
dVol  = volume change vs prior archive   (NaN → ×1 for new entrants; stale rows stay NaN)
flow_raw = clip(voi × dVol, lower=0)
```
`dVol` is **not** absolute-valued — direction of flow is treated as signal. Stale-volume rows carry NaN and are excluded from normalisation rather than zero-filled.

### 4.3 Pooling and normalisation (engine-v1.2)

Contracts are split into pools by `scoring_pool.py`: `dte == 0` → `0DTE`, `dte >= 1` → `1DTE+`, NULL/invalid → excluded (never silently pooled). Pools with fewer than `min_pool_size` (5) eligible survivors are **not ranked and not merged**.

Min-max normalisation runs **independently within each pool**:
```
nlev  = minmax(leverage_raw within pool)
nflow = minmax(flow_raw within pool)
```
Before v1.2 this ran over the whole universe, which let cheap 0DTE contracts inflate both legs and compress every 1DTE+ contract toward zero.

### 4.4 Base score

```
base_score = 0.4 × nlev + 0.6 × nflow
```
`w_lev` = 0.4, `w_flow` = 0.6. **These weights were hand-authored, not fitted.** That matters — see §8.

### 4.5 Multiplier stack

```
Value_Score = base_score × Π(applicable multipliers)
```
Each applied multiplier is recorded per-row in the `multipliers` JSON column, and the product must reproduce `Value_Score`.

| Condition | Key | Value |
|---|---|---|
| Daily bias heavy against the side | `heavy_bias_against` | 0.5 |
| Macro drag against the side | `macro_against` | 0.3 |
| News against / with | `news_against` / `news_with` | 0.8 / 1.2 |
| VWAP reclaim sniper | `vwap_sniper` | 1.5 |
| Outside 1SD expected move ("lottery") | `outside_1sd` | 0.2 |
| Strategy outlook +2 / −2 | `plus2_boost` / `minus2_boost` | 1.5 |
| Outlook +1 OTM / ITM-ATM | `plus1_otm` / `plus1_itm` | 0.5 / 1.3 |
| Outlook −1 OTM / ITM-ATM | `minus1_otm` / `minus1_itm` | 0.5 / 1.3 |
| Zero / unknown outlook, directional | `zero_outlook` | 0.3 |
| Straddle regime, near-ATM both sides | `straddle_atm` | 1.3 |
| 0DTE gamma reflexivity | `odte_boost` | 1.20 |
| POV institutional urgency (calls) | `pov_urgency` | 1.25 |

Rank is assigned **within pool**, descending `Value_Score`.

### 4.6 Display-only layers (do not affect ranking)

- Delta band filter 0.35 ≤ |δ| ≤ 0.50 on the ranked table
- Provider delta and theta/premium columns (commit `00a96db`)
- Time-stop display: now+30min for 0DTE, now+60min for 1DTE+, capped 15:45 ET

These were added deliberately as display-only so the frozen scoring surface stayed frozen.

---

## 5. Pre-Trade Check — seven gates

`pre_trade_check.py`. Takes chart levels and contract inputs, runs option math, and returns **TAKE** only if every gate passes. Constants at the top of the module:

| # | Gate | Threshold |
|---|---|---|
| 1 | Delta in range | 0.35 ≤ \|δ\| ≤ 0.50 |
| 2 | Spread acceptable | spread_pct ≤ 3% (inverted bid/ask fails) |
| 3 | Liquidity | open interest ≥ 500 |
| 4 | Reward:risk | ratio ≥ 2.0 : 1 (amber below 1.5) |
| 5 | DTE matches hold | DTE ≥ derived need from hold hours (6.5h = 1 session) |
| 6 | Trading window | 09:45–11:30 or 14:00–15:30 ET only |
| 7 | Position size | 1% account risk must fund ≥ 1 contract |

Other constants: `SLIPPAGE` 0.02, `LOSS_HOLD_FRACTION` 0.6, `TIME_STOP_FRACTION` 0.65, `CAPITAL_OUTLAY_FLAG` 0.10.

A **scanner→Pre-Trade bridge** exists ("Check this" from the ranked table), prefilling bid/ask from stored chain data and storing `scan_id` on each check, so scanner rank / page verdict / actual outcome can be joined later.

---

## 6. Reports

### 6.1 `eod_report.py` — the primary report

Self-contained HTML with inline SVG charts (no JS, no external deps). Section order as executed:

1. **Coverage** — mark completeness per horizon, late marks, sealed/unmarkable counts. If coverage fails, the report stops. This gate exists because a contaminated report was published once.
2. **Short-horizon buckets** (t15m, t30m) — clustered by contract, ask-entry/bid-exit returns.
3. **Buckets** (t1h, t1d) — mid-based.
4. **Verdict** — engine vs ATM control.
5. **Paper strategy** — three **pre-registered** entry rules, all specified before the data was seen:
   - `FIRST_SEEN` — enter on first appearance
   - `CLOCK_1000` — enter at 10:00 ET
   - `CONFIRM_5` — enter after 5 consecutive scans holding the rank (the conviction hypothesis)
6. **Factor separation** — quartile-edged buckets across `delta`, `spread`, `iv_premium`, etc., with sample size per bucket.
7. **Flags** — raw detail.

Charts: return histogram, exit-timing curve (matched observations across horizons), engine-vs-control bars.

Aggregation rules baked in, each one the residue of a specific error:
- Observations are clustered **per contract**, not per snapshot — the engine logs ~38 rows per contract per day, and counting snapshots makes return a deterministic function of entry price.
- Session date derived via `session_date_sql()` / `et_clock_sql()`, never SQLite `date()`.
- Late marks counted and separable from on-time marks.
- Unmarkable rows (flags whose horizon falls past 16:15 ET) separated from genuinely overdue ones.

### 6.2 `daily_report.py` — ⚠️ exists locally only, not in git

Per-day HTML: top N by best rank, four return panels by entry hour (t15m/t30m/t1h/close), cumulative volume chart; then an aggregate section by hour and delta bucket. Defaults: `--min-mid 0.10` (sub-dime contracts excluded — a $0.05 quote showing +889% is arithmetic, not a fill), 0DTE only.

### 6.3 Telegram

Per-scan Best Value + Magnets + Vol/Expiry + Changes + Catalyst, read from the scan archive, gated on scan exit code.

---

## 7. What has been built

- Scanner v1 → v18, weekly scanner with Anthropic-API thesis generation
- Streamlit dashboard: 5 tabs, 5 zones on the Flow tab
- Telegram bot with interactive reports
- Attribution database, six mark horizons, ATM control group
- Four launchd agents + weekly agent, verified surviving reboot
- Chain quality gate, rollover detector, stale-volume detector, circuit breakers, notify gating
- `spread_gate.py` — hard NO-TRADE vertical-spread gate via `optionlab`
- Pre-Trade Check with seven gates + scanner bridge + pattern→invalidation selector
- EOD report with factor separation, pre-registered entry rules, engine-vs-control
- Trade journal rebuilt from a Wealthsimple activities export (49 sessions, exact timestamps), FIFO lot matching, single source of truth via `scanner/journal_io.py`
- ~60 test modules including golden-master, import-graph, and regression suites
- Three scoring bugs found and fixed: F-01 (delta never computed), F-04 (missing bearish branches), put-leverage floor (signed delta)
- Engine versioning discipline: `config_hash`, `CHANGES.md`, one scoring change per collection window

---

## 8. Known defects and measurement findings

### 8.1 Open defects

| ID | Defect | Impact |
|---|---|---|
| D1 | `flow_norm` uses per-scan max normalisation | Exactly one contract gets 1.0 per scan regardless of absolute activity. Confirmed 15-for-15. Carries 60% of the score. Root cause of negative 0DTE returns |
| D2 | `leverage_norm` — same bug class | No absolute floor; when the whole pool is junk, the best junk still normalises near 1.0. A contract with delta 6.4e-13 once ranked #1 |
| D3 | Category multiplier inverted | `plus1_itm` (1.3) was the **worst**-performing bucket at −63%; `zero_outlook` (0.3) caps good base scores at 0.30. `base_score` alone correlates better with return (0.355) than final `score` (0.197) — the multiplier layer destroys signal the base score had |
| D4 | Stored `delta` unreliable | Saturates at ±1.000 for ITM contracts. The 0.35–0.50 display filter may be excluding the best performers, and all delta-bucket attribution is misgrouped |
| D5 | `engine_sha` never populated | Retroactive version attribution impossible |
| D6 | ~114 t1h marks lost per day | Flags at 15:12/15:14 come due 16:12–16:14, between the last in-window pass and `MARK_WINDOW_END` |
| D7 | No absolute delta floor anywhere in scoring | Zero-exposure contracts can be promoted to rank 1 |

### 8.2 What the data actually says

- **The collection window is not what it claims.** Frozen config `dc2906741dbb2b15` ran only **Aug 3–7** (304 runs). Config `243ecda68cfc8618` has run from **Aug 10 onward** (2,395 runs through Sep 7). All marked days sit on the later hash. **Segment by `config_hash`, never by date range.** The frozen window's registered predictions are unanswerable against their intended config.
- **Only 9 days have short-horizon marks:** Aug 10, 12, 14, 17, 19, 21, 24, 26, 28. Delta exists only from Aug 7.
- **Ranking does have signal** (Aug 28): top-3 beats ranks 4–10 at every horizon.
- **But the 0DTE/1DTE+ split inverts the story.** 0DTE top-3 is negative at every horizon (t15m −7.5%, t30m −7.5%, t1h −3.8%, close −36.9%). 1DTE+ is positive at every horizon (+1.1%, +1.1%, +2.6%, +1.6%). **These must never be pooled.**
- **Exit discipline, not selection, is the defect.** Top-3 peaks around T+1h (+2.3% mid-to-mark) then decays to a **−100% median at expiry**.
- **Attribution-derived gates:** delta <0.15 → −22.9% over n=226 at 2% win rate, and this is ~40% of all engine picks. Spread >0.08 → −30.7%, n=168. `iv_premium` >1.0 → −34.2%, n=70. **The ranker is sorting a universe that should have been gated out.**
- **22% of rank 1–3 flags have |delta| < 0.15** — the worst-performing bucket.
- **92.7% of attribution-era trades matched scanner-flagged contracts**, median rank 4.
- **AAPL hourly range peaks at 10:00 ($1.88) and 15:00 ($1.69)** — consistent with the 09:45–11:30 window.
- The Aug 29 report was **contaminated and its conclusions discarded**: it mixed in days lacking marks/delta, counted snapshots rather than contracts, and produced implausible early-day t1h values.

### 8.3 The structural criticism, stated plainly

`w_lev = 0.4`, `w_flow = 0.6`, and every multiplier in the table above were **hand-authored**. None were fitted to outcomes. They are plausible and evidentially empty. The correct build order — outcome database first, data-derived gates second, fitted ranker last — was followed backwards: the ranker came first and the outcome database came eighteen versions later.

The attribution data now says the same thing three different ways: the gates matter far more than the ranking, and the exit matters more than either. Any successor who spends their first month retuning multipliers is repeating the project's central mistake.

---

## 9. What remains

**Priority 1 — repo integrity (blocking everything else)**
1. Commit or delete the nine untracked files (§10). Until this is done, no one can reproduce any result in this document.
2. Populate `engine_sha` on every run.

**Priority 2 — act on what the measurement already proved**
3. **Exclude 0DTE from ranked output**, or gate it hard. It is negative at every horizon and structurally incompatible with a full-time job.
4. **Ship the exit engine.** 1DTE+ exit at ~60 minutes based on the t1h peak. `time_stop.py` and its tests exist locally and are untracked.
5. **Gate the universe before ranking**: |δ| ≥ 0.15 (probably ≥ 0.35), spread ≤ 0.08, `iv_premium` ≤ 1.0. These thresholds came from the data, not from judgment.

**Priority 3 — fix the scoring defects (post-window, in this order)**
6. `flow_norm` — replace per-scan max with an absolute or rolling reference
7. `leverage_norm` — absolute floor
8. Category multiplier inversion — or delete the multiplier layer entirely and ship `base_score`; the data currently favours deletion
9. Fix stored `delta` saturation, then re-run all delta-bucket attribution (it is currently wrong)
10. `iv_premium` as a scoring input rather than instrumentation
11. Ticker list into `config_hash`
12. Final t1h sweep at 16:14 to close the 114-mark/day gap

**Priority 4 — deferred / open questions**
13. Schwab API into the sources adapter (`schwab.py` is written, not wired)
14. OI-change instead of raw volume as a flow signal — raw option volume is direction-blind; on 0DTE, heavy volume on far-OTM strikes is crowding, not conviction
15. `pmset disablesleep` does not persist across reboot — the launchd agents survive, the machine staying awake does not
16. Log the two outstanding AAPL put trades
17. Drills 3.10 through 5 of the options curriculum

---

## 10. Handover risks — read before trusting anything above

**1. The GitHub repo is incomplete.** These files exist on the local machine, do real work, and are **not in version control**:

```
daily_report.py          verify_attribution.py    rebuild_journal.py
hourly_report.py         reconcile.py             time_stop.py
time_of_day.py           correctContract.py       scanner/journal_view.py
```

Plus, at last check, eleven modified-but-uncommitted files including `app.py`, `portfolio_store.py`, `pre_trade_check.py`, and five test files. **`app.py` contains the scoring path.** If any of those edits touched it, `config_hash` moved at an unknown time, and the frozen-window premise is void independently of every other problem.

**2. Never pool across `config_hash`.** Three engine versions exist (`dc2906741dbb2b15`, `1e191ea1832c2c9a`, `243ecda68cfc8618`) and `CHANGES.md` documents what changed between them. Pooling them produces numbers that look fine and mean nothing.

**3. Never pool 0DTE with 1DTE+.** They have opposite signs at every horizon.

**4. Never use SQLite `date()` on `ts_et`.** It is offset-aware ISO. A 4-hour shift silently regrouped post-20:00 scans into the following session and invalidated an entire report's population. Use `session_date_sql()`.

**5. Cluster per contract, not per snapshot.** The engine writes ~38 rows per contract per day against a single exit price.

**6. Open the database read-only.** WAL mode with `mark_runner` writing every 15 minutes. A GUI holding a write lock will block the marker and produce exactly the silent failure class this system has been bitten by repeatedly. `File → Open Database Read Only` in DB Browser. Never hand-edit a `flags` row.

**7. Short-horizon returns use ask-entry/bid-exit.** Mid-to-mark numbers ignore the spread, and on contracts quoted 6–12% wide a small positive mid-based return is a real-money loss. Both are in `v_outcomes`; know which one you are reading.

**8. `--min-mid 0.10` exists for a reason.** Do not set it to zero and then believe the percentages.

**9. The project's dominant failure mode is scope creep, not bugs.** The pattern is documented across every session: shipping a journal during a scope freeze, adding a paid data subscription mid-collection, investigating gamma squeezes and ORB setups while a measurement window ran, migrating storage that was not broken. Every rewrite during a window destroys the window. The single most valuable discipline in this codebase is **one scoring change per collection window**, and it has been violated more often than kept.

---

## Appendix — useful commands

```bash
# Schema
sqlite3 data/attribution.db ".schema flags"
sqlite3 data/attribution.db "PRAGMA table_info(flags);"

# Which configs have actually run, and for how long
sqlite3 data/attribution.db "
  SELECT config_hash, COUNT(*) n,
         MIN(substr(ts_et,1,10)) first, MAX(substr(ts_et,1,10)) last
  FROM runs GROUP BY 1 ORDER BY n DESC;"

# Days with usable short-horizon marks
sqlite3 data/attribution.db "
  SELECT substr(ts_et,1,10) d, COUNT(mark_t15m) marked
  FROM flags WHERE substr(ts_et,1,10) >= '2026-08-10'
  GROUP BY 1 HAVING marked > 0;"

# EOD report
python eod_report.py --db data/attribution.db

# Services
launchctl list | grep optiontrading
```
