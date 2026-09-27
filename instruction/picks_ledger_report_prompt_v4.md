# Build Prompt v4 — Picks Ledger: correctness fixes before backfill

**Status: BLOCKING. Do not run the historical backfill until §1–§3 are fixed.**
Supersedes v3. v3's §1 contains a factual error — corrected in §1 below.

Context: v3 was implemented and run for session 2026-09-14 (1DTE+, 10 picks).
The report rendered correctly and is working as designed. It surfaced three
defects — two in the pipeline, one in v3's own premise. Fix all three, then
re-run the single session, then backfill.

---

## 1. CORRECTION to v3 §1 — the "no dropout bias" claim is FALSE

v3 §1 asserted that contracts persist in the flag universe all session and that
paths therefore do not truncate. **That assertion was wrong.** It was derived from
a query that sorted `ORDER BY snaps DESC LIMIT 20`, which by construction can only
return contracts that persisted. Survivorship bias.

**Measured reality, session 2026-09-14, top-10 picks:**

| rank | contract | snapshots | coverage | last seen |
|---|---|---|---|---|
| 1 | 335P 09-16 | 152 | 100% | 16:14 |
| 2 | 335P 09-25 | 2 | 1.3% | 09:46 |
| 2 | 340P 09-18 | **1** | 0.7% | 09:45 |
| 3 | 345P 09-18 | 2 | 1.3% | 09:46 |
| 4 | 340P 10-16 | 2 | 1.3% | 09:46 |
| 5 | 327.5P 09-18 | 145 | 95% | 16:14 |
| 6 | 335P 10-16 | 2 | 1.3% | 09:46 |
| 7 | 330P 09-25 | 2 | 1.3% | 09:46 |
| 8 | 322.5P 09-18 | 106 | 70% | 14:46 |
| 2 | 325P 09-18 | 150 | 100% | 117s |

**Median coverage 1.3%. Only 3 of 10 picks are usable observations.**

A pick with 1 snapshot reports `mfe_pct == pnl_close == −spread_pct`. That is not
a measurement — it is the entry quote echoed back. It currently renders as data.

### 1a. Diagnose (do this first, report back)
Across **all** sessions in range, for **all** top-10 picks, produce:
- coverage distribution (min / p25 / median / p75 / max), per session and pooled
- dropout rate vs `spread_pct`, `volume`, `oi`, `moneyness`, `dte`
- whether dropout correlates with the contract moving **against** the pick

If contracts stop being flagged when they stop trading, the surviving sample is
the liquid subset and **every aggregate in every report is optimistic**. This
determines whether historical backfill is worth running at all. **Stop and report.**

### 1b. Fix at source — pinning (required, not optional)
Once a contract enters the top 10 on a session, **keep snapshotting it on every
subsequent scan until session end**, whether or not it re-qualifies for flagging.

- Small change to the existing scan loop; reuse the existing quote path
- Do not build a separate 1-minute companion process — that is a second thing to
  run, fail, and reconcile
- Pinned snapshots must be marked (`pinned=1`) so they are distinguishable from
  qualifying flags in any later analysis

Pinning fixes *future* sessions. It cannot repair history. §1a decides what, if
anything, history is good for.

### 1c. Enforce the exclusion rule that already exists
v3 §3.3 says picks with `coverage_pct < 80` are not observations. Enforce it:
- Exclude from every aggregate and from the header summary line
- Render the row greyed with an explicit `INSUFFICIENT DATA` badge, not a number
- Header must print `n_usable / n_picks` (2026-09-14 would read `3 / 10`)

---

## 2. Session date resolves in UTC — off-by-one after 20:00 ET

Observed: `generated: 2026-09-14T22:35:24-04:00`. That is 02:35 UTC on 09-15.
Any run after 20:00 ET resolves the default session to **tomorrow**, finds no
flags, and emits an empty report while the day's data sits untouched in the DB.

**Fix:**
- Derive session date from ET-localized now:
  `datetime.now(ZoneInfo("America/New_York")).date()`
- Better: define session date as the **trading day the scan window belongs to**,
  not a wall-clock date. A scan at 16:14 ET and a report generated at 22:35 ET
  belong to the same session.
- Add explicit `--session <YYYY-MM-DD>` so output never depends on run time
- Audit every other `date.today()` / `utcnow()` in the report and mark paths
- Regression test: generate for session S with a mocked clock at 23:30 ET and
  assert the output contains session S, not S+1

---

## 3. `first_seen_rank` is not unique — rank 2 appears three times

Observed in the 2026-09-14 output: rank 2 assigned to 335P 09-25, 340P 09-18,
and 325P 09-18. No rank 9 or 10 present.

Rank is what **defines the pick set**. If it is unstable, the top 10 is not the
top 10 and every downstream number is describing the wrong contracts.

**Diagnose:** is `first_seen_rank` (a) not unique within a scan, (b) re-derived
per contract rather than read from the live selection event, or (c) recomputed
at report time from score instead of captured at decision time?

**Fix:** rank must come from the live selection event, unique within
`(session, scan_ts)`. Add an assertion at load: ranks in a scan form a contiguous
1..N with no duplicates. Fail loudly rather than emit a report.

Cross-check against v3 §2's implementation rule: *preserve the live decision, do
not recreate a hindsight ranking.* A duplicated rank is evidence that rule is
not being honoured.

---

## 4. Carried forward from the first run (v3 shipped without these)

These were found in the 40-pick run and are still absent from the spec.

### 4a. MAE is arithmetically floored — redefine it
22 of 40 rows had `mae_pct` **exactly** equal to `−spread_pct`. At t=0 the bid
*is* `entry_bid`, so MAE can never be better than `−spread`. That is arithmetic,
not drawdown, and it is useless for stop placement — which was the reason to
collect it.

- Measure `MAE` against **`entry_bid`**, not `entry_ask`
- Report `spread_cost` as its own column
- These are two different losses and must not be fused

### 4b. Quote sanity gate — pre-scoring
Rank 1 on 2026-09-14 had a **40.6% spread** (bid 2.94 / ask 4.95) on a contract
with 1,986 volume. Rank 5 had **99.7%**. Those are stale quotes ranked as
tradeable contracts.

- Reject `spread_pct > 0.25` before scoring — treat as a bad quote, not a wide market
- Treat the first 5 minutes after the open as suspect; flag rows in that window
- Log rejections with reason so the gate is auditable

### 4c. `n_never_profitable` in the header
18 of 40 picks never once had a bid above `entry_ask` for the whole session —
45% were never exitable at any profit. Single most important number the run
produced; currently not displayed. Add it to §5.1.

### 4d. Benchmark series on the overlay chart
Six picks shared `t_mfe = 20.6` **exactly**; three shared ~144; three shared ~244.
Contracts peaking at identical wall-clock moments is **regime, not selection**.

Add a benchmark line to §5.2: a random same-DTE, same-side contract from the
flag universe, sampled over the same window. Without it, "the engine picked well"
and "the market moved" are indistinguishable — and that is the question this
report exists to answer.

### 4e. §12 — better moneyness example
Replace the 2026-09-11 universe strikes with the confirmed case: on 2026-09-14,
**rank 9 was a $250 PUT with spot at 332.59** — moneyness −25.2%, delta −0.00,
ask $0.04, spread 50%. That contract cannot pay. It was *picked*, not merely
flagged, which makes it stronger evidence than any universe example.

---

## 5. Spec hygiene in v3 (fix while you are in here)

- Title says "Build Prompt v2 / Supersedes v1"; filename says v3. Make the
  document self-consistent or the next reader argues with the wrong version.
- §4 has **two different Phase 1 definitions** — the new Phase 0/1/2 block and
  the older "Phase 1 — 1DTE+" paragraphs. Merge to:
  Phase 0 = inspect & plan (stop for approval) · Phase 1 = ledger, 1DTE+ only ·
  Phase 2 = 0DTE.
- §5.1 and acceptance criterion 1 both hardcode "10 picks". The first
  implementation emitted **40** (union across 17 scan times); the second emitted
  10. Settle it explicitly in §11 — union across session, or top 10 at one
  decision time — and make header text and acceptance criteria follow.

---

## 6. Order of work

1. §3 rank uniqueness — pick set is wrong until this is right
2. §2 session date — cheap, and blocks correct daily operation
3. §1a coverage diagnosis — **stop and report before proceeding**
4. §1b pinning + §1c exclusion enforcement
5. §4a–4e report/scoring fixes
6. Re-run single session 2026-09-14 and review
7. Only then: F-03 fix (v3 §8), then historical backfill

---

## 7. Acceptance criteria (additive to v3 §10)

12. Ranks within a scan are unique and contiguous; violation fails the build loudly.
13. Report generated at 23:30 ET for session S contains session S.
14. Picks with `coverage_pct < 80` render as `INSUFFICIENT DATA`, are excluded
    from all aggregates, and the header prints `n_usable / n_picks`.
15. `MAE` is measured from `entry_bid`; `spread_cost` is a separate column.
16. No contract with `spread_pct > 0.25` at flag time appears in the pick set.
17. Header includes `n_never_profitable`.
18. Overlay chart includes a benchmark series.
19. Pinned snapshots are marked and distinguishable from qualifying flags.

---

## 8. What this run already proved

The report is working. It rendered honestly, printed `median coverage 1%`, and
exposed three defects in a single session — including one in its own specification.
That is the design goal from v1 §0: *a single obviously-broken row is proof of a bug.*

Do not weaken the quality columns to make the output look better. The thin data
was always there; the report is the first thing that made it visible.
