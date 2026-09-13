# Roadmap (gate record)

Do not intentionally change scanner behavior until the current behavior and data contracts are documented.

This file is a gate record. It does not rank work and does not add new proposals.

---

## Deliverables

| deliverable | status |
|---|---|
| Baseline test results (`docs/baseline_tests.md` + captured suite logs) | complete (Session 1) |
| Scoring spec (`docs/scoring_spec.md`) | complete (Session 2) |
| Data contract (`docs/data_contract.md`) | complete (Session 3) |
| Architecture + this roadmap (`docs/architecture.md`, `docs/roadmap.md`) | complete (Session 4) |

`instruction/ROADMAP.md` Phase 0 listed the same set (`instruction/ROADMAP.md:87-93`).

---

## Blocked until documented

Proposed and left unstarted in `instruction/ROADMAP.md` (Phases 1–10). Copied as proposals that existed before this pack; not ordered.

- Introduce `core/` + `interfaces/` layout and a `scan_service.py` that runs scans, builds snapshots, ranks contracts, and is the interface for Streamlit, Telegram, and a future agent (`instruction/ROADMAP.md:99-132`).
- Make UI presentation/orchestration only; keep `best_value.py` as the single scoring implementation; remove duplicate scoring (`:134-139`).
- Versioned canonical snapshot schema (`schema_version`, market/signals/options/strategy/scoring/quality/engine) plus engine/config/feature/source/timestamp metadata (`:148-180`).
- Measure current signals (VWAP reclaim, POV, 0DTE GEX, cost distribution, Blue Sky, news, daily bias, macro, ORB, dVol) before any weight change (`:189-227`).
- Signal event store with stable `signal_id` and immutable original snapshot (`:238-270`).
- Outcome tracking at +5m / +15m / +30m / +60m / EOD (and later expiry), independent of an LLM (`:272-312`).
- Historical intelligence answers from stored signal/outcome records (`:314-343`).
- Deterministic risk engine (`ALLOW`/`REJECT`) that an AI agent cannot override (`:345-379`).
- AI agent layer that consumes structured state and cannot replace quantitative calculation (`:381+`, `:11-12`).
- One `scan_service` behind Streamlit, Telegram, and the agent so no interface reimplements scoring (`:437-457`).
- Replay of historical snapshots through the scoring engine (`:459-463`).

Defects logged in `docs/findings.md` are observations, not approved work.

---

## Coverage

| file | read | skimmed | not opened |
|---|---|---|---|
| `instruction/ROADMAP.md` | 1–97 (Review Gate), 99–180, 189–343, 345–379, 381–401, 437–463 | Phase headings 7–10 | 402–436, 464–598 |
| `instruction/CURSOR_DOCUMENT_BASELINE.md` | 1–54, 267–332 | — | Sessions 1–3 bodies already executed |
| `docs/findings.md` | all (pre-append) | — | — |
