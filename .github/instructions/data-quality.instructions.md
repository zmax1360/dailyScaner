---
applyTo: "sources/**,chain_quality.py,dailyScaner.py,data_adapter.py,schwab.py,news_service.py,snapshot_store.py,volume_analysis.py,weekly.py"
description: Data acquisition and quality-guard rules
---
<!-- GENERATED from .cursor/rules/data-quality.mdc by scripts/sync_agent_rules.py — do not edit -->
- All market data goes through `sources/` (`MarketDataSource`). `yfinance` imports are allowed only in
  `sources/yahoo.py` and `news_service.py`. `dashboard.py` is a known legacy exception — don't copy it.
- The guards below each exist because of a real failure. Never delete or loosen one without being asked:
  chain-quality gate (abort if >20% of top 30 unusable), rollover detector (volume decrease in-session),
  stale-volume check vs prior EOD archive (before 11:00 ET), non-zero exit on abort.
  Abort paths are listed in `docs/architecture.md` §3.
- Stale or missing values → NaN plus a flag (e.g. `stale_volume = True`). Never 0.
- Rollover semantics differ per source (`tests/test_source_aware_rollover.py`). Changing source must
  not change scoring math.
- Massive is paginated with a near-ATM window and a strike cap; respect the page cap.
- Tests must be network-free: use `sources/fixture.py` and `tests/golden/`.
