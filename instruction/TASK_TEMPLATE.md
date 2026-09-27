# Cursor task template

Standing rules load automatically from `AGENTS.md` and `.cursor/rules/`. Don't paste them here.
Copy this file to `instruction/tasks/YYYY-MM-DD-<slug>.md`, fill it in, and attach it to the Agent chat.

---

## Goal
<!-- One sentence. What is true when this is done that isn't true now? -->

## Why
<!-- The defect, finding id (docs/findings.md) or observation that motivates it. -->

## In scope
<!-- Exact files. The agent must not edit anything else. -->
-

## Out of scope
<!-- Name the tempting adjacent work explicitly. -->
- Scoring math / `config.SCORING` (unless this task says otherwise)
-

## Read first
<!-- The relevant spec section, e.g. docs/scoring_spec.md §7, docs/data_contract.md §5 -->
-

## Acceptance
- [ ] `pytest` exits 0; every strict xfail still xfails
- [ ] `config_hash(SCORING)` before/after reported (must match unless scoring is in scope)
- [ ] <!-- task-specific check with an exact command -->

## Report back
Files changed, commands run with real output, anything found but not fixed (append to
`docs/findings.md`), and any durable rule worth adding to `.cursor/rules/` — as a proposed diff.
