---
applyTo: "attribution.py,scanner/**,mark_runner.py,eod_report.py,eod_settlement.py,health_check.py,portfolio_store.py,time_stop.py,notify_delivery.py,**/*.sql"
description: Attribution database, marker and reporting rules
---
<!-- GENERATED from .cursor/rules/attribution.mdc by scripts/sync_agent_rules.py — do not edit -->
- Schema, `v_outcomes`, mark semantics, timezone and versioning contracts: `docs/data_contract.md`.
- **Never delete or rewrite a `flags` row.** Bad data gets a `notes` value. Migrations are additive
  `ALTER TABLE` only (`_FLAG_MIGRATE_COLS` / `_RUN_MIGRATE_COLS` in `attribution.py`).
- **Never backfill a mark by hand.** Marks come from `mark_runner.py` only.
- `mark_runner.py` must stay idempotent (running twice must not move marks) and keep its wall-clock cap.
- Open the DB read-only for analysis (WAL mode; the marker writes every 15 min). A write lock silently breaks marking.
- Query `v_outcomes`, not raw `flags`, for returns. Segment by `config_hash`; split 0DTE vs 1DTE+;
  cluster per contract; group days with `session_date_sql()`, never `date(ts_et)`.
- Naming trap: `runs.engine_sha` is the git short SHA (`attribution.engine_sha()`), but the archive field
  `best_value.engine_sha` is actually `config_hash(SCORING)`. Don't join or compare them.
- Respect `--min-mid 0.10` in reports; don't lower it and trust the percentages.
- Notifications only fire when the scan exited 0. Don't add a notify path that bypasses that.
