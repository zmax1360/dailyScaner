# Options Trading Scanner Evolution Roadmap

## Purpose

This document defines the staged plan for evolving `dailyScaner` from a deterministic options-flow scanner into a reliable options intelligence platform that can support a future AI trading agent.

The project should evolve incrementally. Each phase is reviewed, implemented, tested, and validated before the next phase begins.

## Guiding Principles

1. **Do not rewrite the existing scanner.** Preserve working behavior unless a measured change is intentionally approved.
2. **Separate deterministic computation from AI reasoning.** The AI agent should consume structured market intelligence rather than replace quantitative calculations.
3. **Measure before tuning.** Existing scoring multipliers and signal rules should not be retuned based only on visual inspection.
4. **Make every recommendation traceable.** A signal must be linked to its inputs, engine/config version, and later outcome.
5. **A no-trade decision is valid.** The system must be able to reject opportunities when data quality or risk conditions are insufficient.
6. **Use focused changes.** Prefer small PRs with clear scope over a large rewrite.

# Target Architecture

```text
                           Market Data
                               |
                               v
                      +------------------+
                      | Acquisition      |
                      | / Data Quality   |
                      +--------+---------+
                               |
                               v
                      +------------------+
                      | Features /       |
                      | Signals          |
                      +--------+---------+
                               |
                               v
                      +------------------+
                      | Scoring /        |
                      | Strategies       |
                      +--------+---------+
                               |
                +--------------+--------------+
                |                             |
                v                             v
       +------------------+          +------------------+
       | Historical       |          | Current          |
       | Outcomes         |          | Decision State   |
       +--------+---------+          +--------+---------+
                |                             |
                +--------------+--------------+
                               v
                      +------------------+
                      | AI Agent Layer  |
                      +--------+---------+
                               |
                               v
                      +------------------+
                      | Risk / Execution |
                      | Gate             |
                      +------------------+
```

# Phase 0 — Baseline Review and Specification

## Goal

Create a precise description of the current system before changing behavior.

## Scope

Review and document:

- `dailyScaner.py`
- `best_value.py`
- `strategy_engine.py`
- `zero_dte_gex.py`
- `pov_leakage.py`
- `volume_analysis.py`
- `cost_distribution.py`
- `attribution.py`
- `scheduler.py`
- `data_adapter.py`
- `snapshot_store.py`
- `app.py`
- `telegram_bot.py`
- tests and fixtures

## Deliverables

- `docs/architecture.md`
- `docs/scoring_spec.md`
- `docs/data_contract.md`
- this roadmap
- baseline test results

## Review Gate

Do not intentionally change scanner behavior until the current behavior and data contracts are documented.

# Phase 1 — Stabilize the Core Engine

## Goal

Reduce coupling between the scanner, UI, Telegram, and core calculations.

## Planned Structure

```text
core/
    scanner/
    signals/
    scoring/
    strategies/
    outcomes/

interfaces/
    streamlit/
    telegram/
```

Introduce a service layer such as:

```text
scan_service.py
```

with responsibilities such as:

- run scan
- build current market snapshot
- build signal snapshot
- rank contracts
- expose a stable interface to Streamlit, Telegram, and the future agent

## Requirements

- No duplicate scoring logic.
- `best_value.py` remains the single scoring implementation.
- UI code should become presentation/orchestration, not the system's core business logic.
- Preserve existing CLI behavior while refactoring internals.

## Review Gate

- Existing scanner output remains compatible.
- Existing scheduler behavior remains compatible.
- Existing tests pass.
- New service-layer tests pass.

# Phase 2 — Canonical Data Contract and Versioning

## Goal

Make the scanner output an explicit, versioned contract.

## Canonical Snapshot

```json
{
  "schema_version": "1.0",
  "ticker": "AAPL",
  "timestamp": "...",
  "spot": 0,
  "market": {},
  "signals": {},
  "options": {},
  "strategy": {},
  "scoring": {},
  "quality": {},
  "engine": {}
}
```

## Required Metadata

Every scored run should identify:

- engine version
- config hash/version
- feature version
- market-data source
- timestamp

## Review Gate

- Schema documented.
- Backward compatibility strategy defined.
- Existing archives can still be consumed.
- Schema validation tests exist.

# Phase 3 — Signal and Scoring Quality Review

## Goal

Measure the predictive usefulness of the current signals before changing their weights.

## Signals to Evaluate

Examples include:

- VWAP reclaim
- POV urgency
- 0DTE gamma squeeze/cascade
- cost distribution / support
- Blue Sky
- news alignment
- daily bias
- macro alignment
- opening range
- options flow / dVol

## Measurements

For each signal and signal combination, evaluate:

- sample count
- win rate
- average return
- median return
- maximum favorable excursion (MFE)
- maximum adverse excursion (MAE)
- performance by ticker
- performance by DTE
- performance by market regime
- performance by Value Score bucket

## Important Constraint

Do **not** retune scoring multipliers until the current implementation has been measured.

## Review Gate

Produce a signal quality report and explicitly identify:

- signals worth keeping
- signals requiring more data
- signals with weak evidence
- possible interactions between signals

# Phase 4 — Signal Event Store

## Goal

Turn every actionable recommendation into a uniquely identifiable event.

## Signal Record

```json
{
  "signal_id": "uuid",
  "ticker": "AAPL",
  "created_at": "...",
  "contract": {
    "side": "CALL",
    "strike": 230,
    "expiry": "..."
  },
  "spot": 227.41,
  "value_score": 91.4,
  "signals": [],
  "strategy": "Bull Call Spread",
  "engine_version": "1.2",
  "config_hash": "..."
}
```

## Review Gate

- Every recommendation has a stable identifier.
- Duplicate events are prevented or explicitly represented.
- The original snapshot is immutable.
- Signal records contain enough data to reproduce the decision.

# Phase 5 — Outcome Tracking

## Goal

Measure what happened after every signal.

## Initial Horizons

Track at least:

- +5 minutes
- +15 minutes
- +30 minutes
- +60 minutes
- end of day

Later add expiration-based outcomes where appropriate.

## Metrics

Capture:

- underlying return
- option return
- maximum favorable excursion
- maximum adverse excursion
- time to target
- time to stop
- realized maximum gain/loss

## Design Requirement

Outcome tracking must be independent from the LLM and must be deterministic.

## Review Gate

At least one end-to-end path must exist:

```text
signal -> outcome record -> aggregate statistics
```

# Phase 6 — Historical Intelligence Layer

## Goal

Provide historical context for the current market state.

The system should be able to answer questions such as:

- How did similar signals perform historically?
- How does this setup perform for this ticker?
- How does this setup perform by DTE?
- How does this setup perform in the current market regime?

## Example Output

```json
{
  "pattern": "BULLISH + VWAP_RECLAIM + POV",
  "samples": 183,
  "win_rate": 0.67,
  "avg_return": 0.084,
  "median_return": 0.041,
  "avg_mfe": 0.121,
  "avg_mae": -0.047
}
```

## Review Gate

Historical statistics must be reproducible from stored signal/outcome records and must identify the engine/data versions used to create the sample.

# Phase 7 — Deterministic Risk Engine

## Goal

Introduce a risk gate that operates independently of the AI agent.

## Candidate Checks

- minimum open interest
- minimum volume
- bid/ask spread limits
- premium limits
- spread width limits
- earnings/catalyst restrictions
- 0DTE restrictions
- maximum position risk
- portfolio exposure
- data-quality requirements

## Decision Contract

```text
ALLOW
REJECT
```

with explicit reasons.

## Hard Rule

The AI agent cannot override the risk engine.

## Review Gate

Risk decisions are deterministic, logged, and test-covered.

# Phase 8 — AI Agent Layer

## Goal

Add the AI reasoning layer on top of deterministic market intelligence.

## Agent Input

The agent should receive structured state containing:

- market state
- technical state
- options flow
- volatility state
- signals
- top-ranked contracts
- available strategies
- historical performance
- risk constraints
- data quality warnings

## Agent Responsibilities

### Understand

Explain what the market is doing.

### Compare

Identify agreeing and conflicting signals.

### Select

Choose the preferred strategy from the available deterministic candidates.

### Explain

Provide the reasons and evidence for the recommendation.

### Reject

Explain why there is no acceptable trade when the evidence or risk does not support one.

## Non-Responsibilities

The agent should not:

- invent market data
- recalculate critical quantitative values without access to the canonical engine
- bypass risk rules
- silently change scanner thresholds

## Review Gate

Agent output must be grounded in structured scanner data and risk-engine results.

# Phase 9 — Interface Unification

## Goal

Make Streamlit, Telegram, and the future agent use the same application/service interfaces.

Target:

```text
                scan_service
                     |
       +-------------+-------------+
       |             |             |
   Streamlit      Telegram       Agent
```

No interface should reimplement scoring or strategy logic.

## Review Gate

A change to scoring should affect all interfaces consistently.

# Phase 10 — Replay, Testing, and Production Hardening

## Replay Testing

Historical snapshots should be replayable through the scoring engine so changes can be tested deterministically.

```text
historical snapshot
        |
        v
scanner / signal engine
        |
        v
score + recommendation
```

## Test Areas

```text
tests/
    test_scoring.py
    test_signals.py
    test_strategy.py
    test_data_quality.py
    test_scheduler.py
    test_outcomes.py
    test_schema.py
    test_risk.py
    test_replay.py
```

## Production Hardening

Add or strengthen:

- structured logging
- health checks
- data-source health
- stale-data alerts
- scheduler health
- scan failure/refusal monitoring
- schema migration strategy
- operational documentation

## Review Gate

The system should have clear observability for:

```text
healthy
refused
failed
stale
degraded
```

# PR Execution Plan

Work should be implemented as focused pull requests.

| PR | Scope | Behavior Change |
|---|---|---|
| PR-1 | Baseline documentation + data contracts + test baseline | No |
| PR-2 | Core/service separation | Minimal / compatibility-preserving |
| PR-3 | Signal/scoring instrumentation | No scoring retune |
| PR-4 | Signal event store | No |
| PR-5 | Outcome tracker | No |
| PR-6 | Historical intelligence | No |
| PR-7 | Risk engine | Adds deterministic gates |
| PR-8 | Agent interface | Adds agent-facing APIs/contracts |
| PR-9 | Streamlit/Telegram integration cleanup | Minimal |
| PR-10 | Replay + production hardening | No scoring retune unless separately approved |

# Required Review Process

Each phase follows this sequence:

```text
1. Inspect current implementation
        |
        v
2. Write/update implementation plan
        |
        v
3. Implement focused change
        |
        v
4. Add/update tests
        |
        v
5. Run replay/regression checks where applicable
        |
        v
6. Review PR
        |
        v
7. Merge only after approval
```

No phase should silently expand its scope.

# Current Starting Point

The repository already contains several strong foundations:

- acquisition and scoring are separated
- `best_value.py` is the shared scoring engine
- scheduler success/refusal/failure states are explicit
- scoring configuration is centralized
- attribution/versioning infrastructure exists
- archive JSON is already used as a data contract

The first implementation milestone is therefore **PR-1: Baseline Review and Specification**, not a rewrite of the scanner.

# Definition of Success

The long-term result should be a system where:

```text
Market data
    -> deterministic features
    -> validated signals
    -> scored opportunities
    -> historical evidence
    -> risk validation
    -> agent reasoning
    -> explainable decision
```

Every recommendation should answer:

1. What is happening?
2. What signals support the view?
3. What contract/strategy is being considered?
4. What happened historically in similar setups?
5. What are the risks?
6. Why trade or why not trade?

That is the standard this roadmap is intended to achieve.
