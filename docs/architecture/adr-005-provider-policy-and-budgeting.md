# ADR-005: Provider Policy And Budgeting

Status: Implemented

## Decision

Provider/model fallback is fail-closed. Requested and actual provider/model values must be recorded for provider-backed work. Budgets are hard gates.

## Implementation

- `atticus/providers/deepseek.py` defines allowed direct DeepSeek models, OpenRouter DeepSeek V4 Flash/Pro models, and the opt-in OpenRouter free-model failover order with cost constants.
- `atticus/providers/policy.py` checks provider/model compatibility and records blocked mismatches when a DB is supplied.
- `provider_runs` records requested/actual provider and model, cache hit/miss tokens, output tokens, cost estimate, latency, retries, fallback policy result, and raw usage JSON.
- `budgets` and `budget_entries` support matter, stage, task, and run scopes.
- Scheduler checks task estimated cost, task cost limits, and configured budgets before treating work as runnable.

## Model Routing Policy

- DeepSeek V4 Flash: triage, indexing, extraction QA, classification, duplicate detection, preliminary summaries.
- DeepSeek V4 Pro: legal reasoning, synthesis, hostile review, reducer decisions, high-risk answers.
- Direct DeepSeek and OpenRouter routes are allowed only when explicitly requested and recognized.
- OpenRouter free-model failover is ordered requested-model rotation, not silent provider fallback. It is opt-in through `provider_policy.openrouter_failover.enabled` or `ATTICUS_OPENROUTER_FAILOVER_ENABLED=1`; every configured model must be recognized, and final provider telemetry records the model that was actually requested for the successful attempt.

## Consequences

- Silent fallback is a safety failure, not a convenience feature.
- Budget violations create blocked tasks and human-attention records.
- Status reporting can show spend and remaining budget.
