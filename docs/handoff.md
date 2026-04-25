# Handoff

Date: 2026-04-25

## What This Repo Now Is

Atticus Harness is a safe standalone legal AI factory control plane. It has durable state, evidence graph records, validation/certification, scheduling, leases, context packs, provider policy, budgets, worker candidate packets, reducer-only canonical writes, migration reporting, and CLI status UX.

## Current Safety State

- OpenClaw is not started by the harness.
- Live legal workers are blocked by adapter/launcher boundaries.
- External legal actions are blocked by policy.
- Legacy workspace/archive imports are candidate-only or rough-note-only.
- Provider fallback fails closed.
- Worker outputs need valid leases; late outputs are quarantined.

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
atticus migrate-report --workspace /home/alba/.openclaw/workspace-atticus-legal --db /tmp/atticus-smoke.sqlite3 --dry-run
atticus doctor --db /tmp/atticus-smoke.sqlite3
```

## Next Best Work

1. Add file inventory scans.
2. Add archive ledger importer for old `ledger.sqlite`.
3. Add real provider request builders behind mocks.
4. Add adapter enablement flags and task-local output directories.
5. Add FTS5 memory search and richer citation views.
