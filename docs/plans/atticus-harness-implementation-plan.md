# Atticus Harness Implementation Plan

Status: Current implementation snapshot, 2026-04-25

## Completed In This Pass

- Expanded SQLite schema to version 2 with event sourcing, evidence graph, leases, worker attempts, candidate outputs, reducer packets, council tables, context packs, provider runs, budgets, human attention, and migration reports.
- Preserved existing CLI and added `inspect`, `validate`, `certify`, `schedule`, `lease`, `work-order`, `reduce`, `budget`, `provider-policy`, `human-attention`, `migrate-report`, and `doctor`.
- Implemented S0-S9 stage foundation gating.
- Implemented fenced leases with expiry and late-output quarantine.
- Implemented deterministic context packs and work-order generation.
- Implemented provider fallback recording and budget hard stops.
- Implemented reducer-only canonical artifact writing.
- Implemented candidate-only migration classification and dry-run reports.
- Added validation gates for source inventory, hashes, extraction coverage, production mappings, chronology citations, claim support, authority citation format, stale dependencies, reducer packet schema, and canonical write authorization.
- Added tests for safety and factory contracts.

## Remaining Engineering Milestones

1. Add real adapter execution behind explicit enablement.
2. Add task-local filesystem layout for candidate packets and raw logs.
3. Add FTS5 search over certified/candidate memory.
4. Add richer archive-ledger import from old `ledger.sqlite`.
5. Add idempotent reducer consumed-task records and reducer lock recovery.
6. Add file inventory scans for every harness-visible file.
7. Add richer legal artifact schemas for claims, chronology, authorities, and drafts.
8. Add a provider mock transport and real request builders with no-test-network guarantees.
9. Add matter creation/import CLI.
10. Add dashboard only after CLI/status is stable.

## Safety Gates To Keep

- No external legal actions.
- No live workers unless explicitly enabled later.
- No old artifact certification during migration.
- No provider fallback unless explicitly allowed.
- No canonical writes from workers.
