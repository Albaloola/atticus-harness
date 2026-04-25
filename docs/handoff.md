# Handoff

Date: 2026-04-25

## What This Repo Now Is

Atticus Harness is a safe standalone legal AI factory control plane. It has durable state, evidence graph records, validation/certification, scheduling, leases, context packs, provider policy, budgets, worker candidate packets, reducer-only canonical writes, migration reporting, and CLI status UX.

## Current Safety State

- OpenClaw is not started by the harness.
- Live legal workers are blocked by adapter/launcher boundaries.
- A local-only `run-local` path exists for safe harness exercising; it uses `local_stub`, requires an active lease, writes only task-local JSON, records candidate output, and never writes canonical artifacts.
- External legal actions are blocked by policy.
- Legacy workspace/archive imports are candidate-only or rough-note-only.
- Provider fallback fails closed.
- Worker outputs need valid leases; late outputs are quarantined.
- Budget gates are checked before local execution and provider/budget telemetry is recorded.

## Verification Commands

```bash
python -m pytest -q
python -m compileall -q atticus tests
```

CLI smoke set:

```bash
atticus init --db /tmp/atticus-smoke.sqlite3
atticus status --db /tmp/atticus-smoke.sqlite3
atticus ask --db /tmp/atticus-smoke.sqlite3 "source inventory status"
atticus import-candidates --workspace /home/alba/.openclaw/workspace-atticus-legal --db /tmp/atticus-smoke.sqlite3 --dry-run
atticus provider-policy --provider openrouter --model deepseek/deepseek-v4-pro
atticus budget --db /tmp/atticus-smoke.sqlite3 --scope-type matter --scope-id atticus
atticus validate --db /tmp/atticus-smoke.sqlite3 --gate source_inventory --target-type matter --target-id atticus
atticus schedule --db /tmp/atticus-smoke.sqlite3 --dry-run
atticus lease --db /tmp/atticus-smoke.sqlite3 --task-id <task-id> --worker-id atticus-local --write
atticus run-local --db /tmp/atticus-smoke.sqlite3 --task-id <task-id> --lease-id <lease-id> --worker-id atticus-local --output-dir /tmp/atticus-worker-output --write
atticus migrate-report --workspace /home/alba/.openclaw/workspace-atticus-legal --db /tmp/atticus-smoke.sqlite3 --dry-run
atticus doctor --db /tmp/atticus-smoke.sqlite3
```

## Next Best Work

1. Add real provider request builders behind mocks, keeping OpenRouter spend gated behind explicit flags.
2. Add archive ledger importer for old `ledger.sqlite`.
3. Add file inventory scans.
4. Add FTS5-backed persistent memory indexes on top of the current citation-aware lexical search.
5. Add live adapter enablement flags only after local execution, provider policy, and budget gates pass end-to-end review.
