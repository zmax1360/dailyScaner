# dailyScaner — agent instructions

Read by Cursor, GitHub Copilot (VS Code) and other agents. Path-specific rules live in
`.cursor/rules/*.mdc` (source) and are mirrored to `.github/instructions/` by `scripts/sync_agent_rules.py`.

Options-flow scanner for AAPL/NVDA: scans chains on a schedule, scores contracts ("Best Value"),
logs every flag to SQLite, marks outcomes later, reports. Python 3.11+, Streamlit UI, launchd services (macOS).

## Where knowledge lives

- `docs/scoring_spec.md`, `docs/data_contract.md`, `docs/architecture.md` — evidence-based descriptions of
  **current** behaviour with `file:line` refs. Read the relevant one before changing that area.
- `docs/findings.md` — defects observed. **Observations, not approved work.** Don't fix them unless asked.
- `instruction/HANDOVER_dailyScaner.md` — context and history. A dated snapshot; parts are stale.
- `instruction/CURSOR_*.md` — historical one-off task prompts. Not current rules.
- If code and a doc disagree: trust the code, report the discrepancy, don't silently edit the doc.

## Hard rules (every task)

- **Layer separation:** acquisition may touch the network; scoring may not; UI never recomputes scores.
  Only `sources/yahoo.py` and `news_service.py` may import `yfinance` (enforced by `tests/test_import_graph.py`).
  Scanner → UI/bot contract is archive JSON `archive/{TICKER}_{YYYYMMDD}_{HHMM}.json`.
- **Missing data → NaN and exclude. Never default.** No `0` for stale volume, no `delta = 0.5`,
  no `fillna(1.0)`. A default is a fabricated measurement.
- **Do not change scoring math** (`best_value.py`, `strategy_engine.py`, `config.SCORING`) unless the task
  explicitly says so. If you think it's needed, stop and explain. Any change moves `config_hash`:
  record it in `CHANGES.md` with the new hash and engine tag.
- **One scoring change per collection window.** Scope creep is this project's main failure mode.
  No new signal modules or multipliers. Log unrelated issues in `docs/findings.md`; don't fix them.
- **Tests:** never weaken, skip, or delete a test to make it pass; never remove an `xfail(strict=True)`.
  If a test encodes a wrong expectation, report it — don't edit the assertion.
- **Evidence:** report actual command output. If you didn't run it, say so.
  Never pipe pytest through `tee` and trust the exit code — `tee` returns 0 when pytest fails.
- **Datetimes:** timezone-aware only (ET). No naive datetimes (`tests/test_no_naive_datetime.py`).
- **Import side effects:** no network calls or `sys.exit` at module import in new code.
- **Secrets:** never commit `.env` or API keys (Telegram, Finnhub, Massive, Schwab). Check `git diff` first.

## Setup and tests

- Install: `pip install -r requirements.txt` (points at `pyproject.toml`, the single dependency list).
  Add new dependencies to `pyproject.toml` only. `docs/baseline_pip_freeze.txt` is a historical snapshot.
- Run: `pytest` (testpaths = `tests`). Must pass on a clean clone with no `.env` and no `data/`.
- Done means `pytest` exits 0: no failures, and every strict xfail still xfails.

## Analysis rules (attribution data and reports)

- Segment by `config_hash`, never by date range. Never pool engine versions.
- Never pool 0DTE with 1DTE+ — opposite signs at every horizon.
- Cluster per contract, not per snapshot (~38 rows per contract per day).
- `ts_et` is offset-aware ISO: never SQLite `date()`/`time()`/`strftime()` on it.
  Use `session_date_sql()` / `et_clock_sql()` from `eod_report.py`.
- Short-horizon returns: know whether you're reading ask-entry/bid-exit or mid-to-mark.

## Working style

- Small, single-purpose changes. One task per session; state the files in scope before editing.
- Prefer existing patterns and the `sources/` adapter (yahoo | massive | fixture) over new data paths.

## Keeping this memory current

- When you discover a durable rule (a convention, a guard and why it exists, a trap), **propose** an edit
  to this file or the matching `.cursor/rules/*.mdc` in the same change. Show it in the diff; never
  update rules silently. After editing `.cursor/rules/`, run `python scripts/sync_agent_rules.py`.
- Rules hold durable facts only. Current status, open defects and numbers go in `docs/` or the handover.

