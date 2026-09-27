# Build Prompt v2 — Interactive Picks Ledger Report (with historical backfill)

**Status: DRAFT for review.**
Supersedes v1. The §1 prerequisite gate in v1 is **resolved** — see §1 below.

Replaces the current `eod_<date>_<ticker>_<ts>.html` aggregate report with a
per-pick ledger, generated for **every session in range**, not just today.

---

## 0. Context for the implementer

The existing report answers *"how good is the scoring engine statistically?"*
over all flags. That question cannot be answered yet, and the report is not
inspectable — you cannot see a single decision the engine made.

This report answers a simpler question:

> **What did the scanner pick on a given day, and what happened to each pick afterward?**

It must be readable in 15 seconds and reviewable by eye. A single obviously-broken
row (e.g. 400C flagged with spot at 333) is proof of a bug. That is the point.

DB: `attribution.db`. Output convention: `report/eod_<date>_<ticker>_<ts>.{html,txt}`.
Keep it, one file pair per session.

---

## 1. Prerequisite — RESOLVED, no quote-capture writer needed

v1 assumed a dedicated quote-series table was required. It is not.

The `flags` table already **is** a sampled quote series: each flagged contract is
re-snapshotted on most scans with `ts_et, bid, ask, spot`. Verified on 2026-09-11:

- Top contracts hold **150–225 snapshots** spanning 09:50 → 16:16 (full session)
- Effective resolution **~0.39–0.58 snapshots/min**, i.e. one sample every
  **~1.7 to ~2.6 minutes**
- **No dropout bias**: contracts persist in the universe across the whole session
  including deep OTM strikes, so paths do not truncate where drawdowns live

Therefore: **backfill the ledger across 2026-08-10 → present (~26 sessions)
directly from `flags`.** Do not build a capture writer for this deliverable.

**Critical:** contracts must be keyed by `(ticker, side, strike, expiry)`.
Keying without `expiry` collapses distinct contracts — e.g. on 2026-09-11 the
strike 340 CALL appears at four expiries (09-14, 09-16, 09-18, 10-16)
simultaneously. Any grouping that omits expiry produces invalid paths.

---

## 2. Definitions — get these exactly right

Ambiguity here silently invalidates every number.

| Term | Definition |
|---|---|
| `contract_key` | `(ticker, side, strike, expiry)`. **Never** group without expiry. |
| `first_seen` | First timestamp this contract entered the top 10 on this date. **The single reference point for all P/L math.** Re-entries never reset it. |
| `entry_ask` | Ask at `first_seen`. All returns measured from this. |
| `MFE` | Highest **bid** in any snapshot after `first_seen`. Never mid. Never a high preceding `first_seen`. |
| `t_MFE` | Minutes from `first_seen` to the MFE snapshot. |
| `MAE` | Lowest **bid** between `first_seen` and `t_MFE` — drawdown endured before the peak. |
| `t_decay` | First snapshot after `t_MFE` where bid falls back below `entry_ask`. |
| `scans_profitable` | Count of snapshots where `bid > entry_ask`. **Reported in scans, not minutes.** |
| `max_win_streak_scans` | Longest **contiguous** run of snapshots where `bid > entry_ask`. **In scans.** |
| `t_first_profitable` | Minutes from `first_seen` until bid first exceeds `entry_ask`. |
| `moneyness` | `(strike - spot_at_flag) / spot_at_flag`, signed. Spot **at flag time**, never close. |
| `coverage_pct` | Snapshots present for this contract ÷ scans that occurred between `first_seen` and session end. |
| `median_scan_interval_sec` | Median gap between consecutive snapshots for this contract. |

**Non-negotiable:** every P/L figure is **ask-entry / bid-exit**. Mid-to-mid is
forbidden anywhere in this report. On a contract with an 8% spread, price must
move 8% before flat — mid math logs winners that could never be exited green.

---

## 3. Sampling limits — state these in the report, do not paper over them

1. **MFE is sampled.** Peaks between snapshots are missed. This biases MFE
   **downward** — conservative. Print a footer note so nobody later "corrects" it up.
2. **Streak resolution is ~2 minutes.** `max_win_streak_scans` has a boundary
   error of roughly ±1 scan. An exit window shorter than ~6 minutes is **not
   measurable** with this data. Always render it in scans with
   `median_scan_interval_sec` beside it — never convert to minutes and imply
   precision that does not exist.
3. **Low-coverage picks are not observations.** Any pick with `coverage_pct < 80`
   must be visually flagged and excluded from any aggregate.

---

## 4. Build order

**Phase 1 — 1DTE+ only.** This is the class where ~2-minute resolution is
adequate (moves unfold over 20–60 min) and where returns were survivable
(−2.7% vs −35% for 0DTE). If the ledger shows the engine selecting far-OTM,
low-delta contracts here, the bug is found without needing finer capture at all.

**Phase 2 — 0DTE.** Same report, with sampling caveats prominent. Expect
`max_win_streak_scans` to be near the resolution floor.

---

## 5. Report structure

### 5.1 Header
Date, ticker, generation time, DB path.
Summary line: `10 picks | 3 reached +30% MFE | median t_MFE 24m | median coverage 94% | median scan interval 138s`
Plus: `n_distinct_underlying_views` — count of distinct `(side, strike)` pairs in
the top 10. If 10 picks collapse to 3 views, the report must say so; four expiries
of the same strike is false diversification, not ten bets.

### 5.2 Overlay chart (top of page)
All 10 picks on one set of axes.
- **X:** minutes since `first_seen`, normalized — every pick starts at 0
- **Y:** P/L % = `(bid - entry_ask) / entry_ask`
- Emphasized zero line; one line per pick; legend = rank + strike + right + expiry
- Hover identifies; click selects (see 5.4)

Purpose: one glance shows whether the engine had a good day, and whether picks
moved together (regime) or independently (selection).

### 5.3 Ledger table
One row per pick, sortable, click-to-select.

**Chosen:** `rank` `time` `strike` `right` `expiry` `DTE` `spot@flag` `moneyness`
`delta` `IV` `spread%` `entry_ask` `vol` `OI` `score`

**Happened:** `MFE` `t_MFE` `MAE` `max_win_streak_scans` `scans_profitable`
`mark_close` `P/L% @ close` `mark_expiry`

**Quality:** `coverage_pct` `median_scan_interval_sec` `n_snapshots`

**Rank dynamics:** `first_seen_rank` `best_rank` `minutes_in_top10`

Inline **sparkline** per row (~200×40px): entry line, MFE dot, MAE dot, shaded
bands where `bid > entry_ask`. Ten must be scannable in 15 seconds — not full charts.

Conditional formatting on the known-hostile buckets: `spread% > 0.08`,
`|moneyness| > 0.03`, `delta < 0.15`, `coverage_pct < 80`.

### 5.4 Selected-pick detail panel
Click any row or overlay line → full chart below the table:
- Bid and ask series (two lines — the gap between them **is** the cost)
- Horizontal line at `entry_ask`; markers at MFE, MAE, `t_decay`
- Shaded bands where `bid > entry_ask`
- Snapshot points drawn as dots, so sampling density is visible
- P/L% toggle; annotation box with all ledger fields

Default selection on load: rank 1.

### 5.5 Data quality footer
Sealed stale / unavailable counts, late marks, F-03 rows above 1.0,
sampling caveats from §3.

---

## 6. Backfill mode

Add a CLI flag: `--backfill <start> <end>` (default `2026-08-10` → latest session).

- Emit one HTML + one TXT per session
- Plus an **index.html** listing every session: date, n picks, median MFE,
  n reaching +30% MFE, median coverage — linking to each report
- Idempotent: re-running overwrites cleanly, no duplicate rows

---

## 7. Explicitly NOT in this report

Return later, computed over picks only:
- Factor separation (currently runs over the flag universe, not picks)
- Engine-vs-control (control has 84 delta-NULL rows; population match unverified)
- Outcome bucket means by rank (n=13, n=17 — below the stated ~200 threshold)
- PAPER STRATEGY (n=1, CONFIRM_5 empty)

---

## 8. Housekeeping — do F-03 BEFORE backfilling

- **F-03 / F-S1-09:** 4 rows scored above 1.0, multiplier cap not applied.
  Uncapped scores perturb rank ordering, which determines top-10 membership —
  the exact selection this report is built on. Fix first or backfill twice.
- Delete the stale string `do not act on these until the window closes 2026-08-28`.
- Add `paired_flag_id` to the schema now (cheap to write, impossible to
  reconstruct later): when the engine flags a call and a put on the same
  underlying at the same scan, they count as **two picks** but should be linkable.

---

## 9. Technical constraints

- Single self-contained HTML per session, no external CDN — must open offline
- Inline SVG or vendored chart lib; no network fetch at view time
- Ledger embedded as inline JSON so interactivity needs no server
- 10 sparklines + table above the fold at laptop width
- Parallel `.txt` emission of the ledger table (sparklines omitted)

---

## 10. Acceptance criteria

1. Opening any session's file shows 10 picks and their outcomes without scrolling
   past the table.
2. Clicking a pick renders its bid/ask chart with entry, MFE, MAE marked and
   snapshot points visible.
3. Every P/L number is ask-entry / bid-exit. No mid anywhere.
4. `moneyness` uses spot at flag time.
5. Contracts are keyed including `expiry`; the 2026-09-11 340C case resolves to
   four separate rows, not one.
6. Backfill completes 2026-08-10 → present with a working index.
7. Streak columns render in scans with `median_scan_interval_sec` beside them.

---

## 11. Settled — do not re-litigate

| Question | Decision |
|---|---|
| Universe | Top 10 **by score at `first_seen`** — the live decision. Best-rank-during-day is hindsight. |
| Re-entry | **One pick**, anchored at first `first_seen`. |
| Quote resolution | ~1.7–2.6 min from `flags`. Not an assumption — measured. |
| After-hours | Series stops at the live-quote window (09:30–16:15 ET). Do not extend past it; marks already seal unquoteable windows as unavailable. |
| Call + put same scan | **Two picks**, linked via `paired_flag_id`. |

---

## 12. First thing to look at once it renders

The `moneyness` column, on both the ledger and the flag universe.

On 2026-09-11 with spot ≈ 333, the universe contained 400C, 360C, 355C, 350C,
345C and 300P. If contracts like those appear in the **top 10**, the scoring
engine has a moneyness problem and that is the highest-value finding available.

Also pull up **2026-09-11 337.5C** specifically. That contract is known to have
performed badly. Its ledger row is the single most informative line in the backfill.