# Attribution Layer — Build Checklist

**Rule for this whole document:** a box is ticked when the **command passes**, not when the
code looks right. Every step below has a paste-able verification. If you can't run the
verification, the step isn't done.

**Scope freeze:** no engine changes, no new signal modules, no multiplier retuning until
Step 7 is logging daily. If you catch yourself editing `best_value.py` scoring math, stop.

---

## Step 0 — Make the suite runnable (30 min)

- [x] `git checkout -b attribution` — do not work on main
- [x] Move `test_spread_gate.py` and `test_dailyScaner_regressions.py` into `tests/`
- [x] `pip freeze > requirements.txt`, then fix the pins in `pyproject.toml` to match
- [x] Add `optionlab` to deps (undeclared today — it breaks collection on a clean checkout)
- [x] Set `--cov-fail-under` to whatever the first run actually reports (`fail_under = 8`)
- [x] Add `data/attribution.db*` to `.gitignore` (WAL creates `-wal` and `-shm` too)

**Verify:**
```bash
python -m pytest -q                    # expect: all pass (xfail suite not landed yet)
python -m pytest --collect-only -q | tail -1   # count must include spread_gate tests
```

---

## Step 1 — Land `attribution.py` unmodified (15 min)

- [x] Land `attribution.py` in repo root (implemented from VERIFY/REVIEW specs — no prior copy existed)
- [x] Confirm `data/` is created on first write, not committed

**Verify:**
```bash
SCANNER_DB=/tmp/smoke.db python -c "
from attribution import _db
with _db('/tmp/smoke.db') as c:
    print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type IN ('table','view')\")])
    print('journal:', c.execute('PRAGMA journal_mode').fetchone()[0])
"
```
Expect tables `runs`, `flags`, view `v_outcomes`, and `journal: wal`.
**If journal is not `wal`, stop** — concurrent reads during a scan will lock.

---

## Step 2 — Extract config to one dict (45 min) · fixes F-24

- [x] Create `config.py` with every hardcoded literal that shapes a score:
      `w_lev 0.4`, `w_flow 0.6`, `min_volume 500`, `MIN_OI_FOR_MAGNET 500`,
      and all 14 multipliers (`0.2, 0.3, 0.5, 0.8, 1.2, 1.25, 1.3, 1.5, …`)
- [x] `best_value.py` reads from it — no bare numeric literals left in the scoring path
- [x] Pass the same dict to `log_run(cfg=...)`

**Verify:**
```bash
# No magic numbers left in the multiplier block
sed -n '215,290p' best_value.py | grep -nE '\*= *[0-9]' && echo "FAIL: literals remain" || echo OK
python -c "from config import SCORING; from attribution import config_hash; print(config_hash(SCORING))"
```
Record that hash somewhere. It is the fingerprint of "engine v1".

**engine v1 config_hash:** `ba70b8bb4d6dda86`

---

## Step 3 — Expose which multipliers fired (1 h)

Today `calculate_best_value` applies multipliers in place and discards the breakdown. Without
this you learn *that* rank 1 won, never *why*.

- [x] Add a `mults: dict[idx, dict[str, float]]` accumulator beside the existing `tags` dict
- [x] Record every multiplier at the point it is applied — key + value
- [x] Attach as a `_multipliers` column on the returned frame

**Verify:** `tests/test_attribution.py::test_multipliers_are_recorded` (product of factors
reproduces `Value_Score` after `round(4)`).

---

## Step 4 — Define the control programmatically (45 min)

- [x] Write `build_control_rows(chain_df, spot, expiry)` → nearest-to-ATM strike, both sides,
      **selected by rule, never by hand**
- [x] Same expiry as the modal flagged expiry that run
- [x] Runs whether or not the engine flagged anything

**Verify:** `test_control_is_deterministic`, `test_control_is_independent_of_engine_output`.

---

## Step 5 — Wire `log_run` into the scan path (30 min)

- [x] One call, immediately after scoring in `dailyScaner._log_scan_attribution`
- [x] Passes `cfg`, both biases, `control_rows`, and `engine_sha` (`git rev-parse --short HEAD`)
- [x] **Never inside a Streamlit render** — the scan path, so scheduler runs log too
- [x] Wrapped so a logging failure cannot kill a scan (log the error, keep scanning)

**Verify:**
```bash
python dailyScaner.py AAPL
sqlite3 data/attribution.db "
  SELECT r.ticker, r.n_scored, r.config_hash,
         COUNT(*) rows, SUM(f.is_control) ctrl,
         SUM(f.multipliers='{}') empty_mults
  FROM runs r JOIN flags f USING(run_id) GROUP BY r.run_id;"
```
Expect: `rows = n_scored + ctrl`, `ctrl >= 1`, `empty_mults = 0`.
**`empty_mults > 0` means Step 3 silently regressed.**

---

## Step 6 — Marker cron (2 h)

- [x] `fetch_option_mid(ticker, side, strike, expiry) -> float | None` — returns `None` on
      any failure, never raises, never guesses
- [x] `mark_runner.py` loops `due_for_marking("t1h")` and `("t1d")`
- [x] launchd plist, every 15 min (`com.optiontrading.mark-runner.plist`) — loaded into `~/Library/LaunchAgents/`
- [x] Expiry marks: `--expiry-only` / outside window expiry-only; t1h/t1d gated to 09:30–16:15 ET (`--force` bypass)

**Verify:**
```bash
python mark_runner.py --dry-run     # prints what it would mark, writes nothing
python mark_runner.py
sqlite3 data/attribution.db "
  SELECT COUNT(*) total,
         SUM(mark_t1h IS NOT NULL) marked_1h,
         SUM(mark_t1d IS NOT NULL) marked_1d FROM flags;"

# Idempotency — run twice, marks must not move
sqlite3 data/attribution.db "SELECT SUM(mark_t1h) FROM flags;" > /tmp/a
python mark_runner.py
sqlite3 data/attribution.db "SELECT SUM(mark_t1h) FROM flags;" > /tmp/b
diff /tmp/a /tmp/b && echo "OK: marks immutable"
```

---

## Step 7 — Daily health check (30 min)

Silent breakage is the failure mode that costs you a month. Add a check that runs daily and
alerts via the Telegram bot you already have:

- [x] Rows written today > 0
- [x] `empty_mults = 0`
- [x] Rows overdue for a mark by >4h = 0
- [x] Only one distinct `config_hash` in the last 7 days (an unnoticed retune corrupts the sample)
- [x] Daily launchd at 16:30 (`com.optiontrading.health-check.plist`) — loaded into `~/Library/LaunchAgents/`

**Verify:**
```bash
python health_check.py --alert-on-fail
```

---

## Ops status (2026-07-26)

| Item | Value |
|---|---|
| Branch | `attribution` |
| engine v1 `config_hash` | `ba70b8bb4d6dda86` |
| Default DB | `data/attribution.db` (gitignored) |
| LaunchAgents | `com.optiontrading.mark-runner`, `com.optiontrading.health-check` |
| Next | Let scheduler write rows Mon–Fri; after ~3–4 weeks run Step 8 + fresh-session `VERIFY_PROMPT.md` |

### Re-verification log (2026-07-26 15:20 ET)

| Step | Item | Result | Evidence |
|---|---|---|---|
| 0 | branch `attribution` | PASS | `git branch --show-current` |
| 0 | tests in `tests/` + suite green | PASS | 44 collected (7+15+19+3); **44 passed** |
| 0 | `requirements.txt` / `pyproject.toml` / `optionlab` | PASS | files present; pins match |
| 0 | `data/attribution.db*` gitignored | PASS | `.gitignore` |
| 1 | WAL schema `runs`/`flags`/`v_outcomes` | PASS | `journal: wal` |
| 2 | `config.py` + no bare `*= N` in scoring | PASS | `OK: no bare *= literals` |
| 2 | `config_hash` | PASS | `ba70b8bb4d6dda86` |
| 3 | `_multipliers` reproduce score | PASS | `test_multipliers_are_recorded` + DB `nlev`/`nflow`/`base_score` (`test_db_row_multipliers_reproduce_score`) |
| 5b | alert on log failure (C1) | PASS | `alert_attribution_failure` from `_log_scan_attribution` except |
| 4 | control rows rule-based / deterministic | PASS | control tests green |
| 5 | scan-path `log_run` reconcile | PASS | `AAPL\|59\|…\|61\|2\|0\|OK` |
| 5 | not in Streamlit render | PASS | wired only in `dailyScaner._log_scan_attribution` |
| 6 | `mark_runner --dry-run` | PASS | outside window → expiry-only |
| 6 | marks immutable (unit + SUM diff) | PASS | `test_marks_are_immutable`; `diff /tmp/a /tmp/b` OK |
| 6 | LaunchAgent loaded | PASS | `gui/…/com.optiontrading.mark-runner` |
| 7 | `health_check.py` all PASS | PASS | 6/6 checks |
| 7 | LaunchAgent loaded | PASS | `gui/…/com.optiontrading.health-check` |
| 8 | go/no-go sample size | **OPEN** | `ret_t1d` marked = 0; controls = 2 (need weeks) |

---

## Step 8 — Go/no-go (after 3–4 weeks, ~600+ rows)

- [ ] `SELECT COUNT(*) FROM v_outcomes WHERE ret_t1d IS NOT NULL;` ≥ 400
- [ ] At least 20 control rows
- [ ] Run the verdict query:

```sql
SELECT rank_bucket, COUNT(*) n,
       ROUND(AVG(ret_t1d), 4) avg_ret,
       ROUND(SUM(CASE WHEN ret_t1d > 0 THEN 1.0 ELSE 0 END)/COUNT(*), 3) win_rate
FROM v_outcomes WHERE ret_t1d IS NOT NULL
GROUP BY rank_bucket ORDER BY rank_bucket;
```

**Read it honestly:**

| Result | Meaning |
|---|---|
| `01-03` ≈ `21+` | Score does not discriminate. The ranking is noise. |
| All buckets ≈ `CONTROL` | No edge at all. The universe filter is doing nothing. |
| Buckets > `CONTROL`, flat across ranks | Universe selection has edge, ranking does not. Fix the top-30 filter, not the multipliers. |
| Monotonic decline `01-03` → `21+` | The score works. **Now** go fix F-01/F-05 and re-measure. |

- [ ] Write the numbers down before interpreting them. Decide the threshold *first*.

---

## Anti-regression rules

1. **Never delete a flag row.** Bad data gets a `notes` column, not a `DELETE`.
2. **Never backfill a mark by hand.** A hand-typed mark is your judgment, which is what
   this whole exercise exists to remove.
3. **Changing any multiplier changes `config_hash`** — segment the analysis, don't pool it.
4. **No new signal modules until Step 8.** Six modules already contribute unmeasured
   multipliers. Adding a seventh makes the answer harder to find, not better.
