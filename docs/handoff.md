# Handoff

Date: 2026-04-26

## Operating Rule: Use The Agent Team

The next Codex instance must not work as a lone coder.

It should operate as a lead engineer coordinating a specialist agent team. For any non-trivial task, it must delegate bounded, parallel subtasks to agents before or during implementation.

Use agents for:

- codebase exploration
- architecture review
- implementation slices
- test design
- bug reproduction
- security/safety review
- documentation review
- regression checking
- prompt-quality review
- legal workflow reliability review

Rules:

- Do not delegate vague work.
- Each agent gets a concrete, self-contained task.
- Agents should have clear ownership boundaries.
- Worker agents editing code must be told not to overwrite others' changes.
- The lead agent must integrate and review all agent outputs.
- The lead agent remains accountable for final correctness.
- Do not use agents to bypass tests or safety rules.
- Do not run live provider calls through agents unless Omer explicitly approves the exact live run.
- Do not let agents touch `/home/alba/.openclaw/workspace/`.
- Agents may inspect or edit `/home/alba/atticus-harness` and `/home/alba/.openclaw/workspace-atticus-legal/` only as required by the task.

Default pattern:

1. Lead inspects the current state locally.
2. Lead spawns specialist agents for independent subtasks.
3. Lead continues non-overlapping work while agents run.
4. Lead reviews agent outputs critically.
5. Lead integrates changes.
6. Lead runs tests and safety checks.
7. Lead reports honestly.

The expected posture is not "I will do everything myself."
The expected posture is "I will lead the team and verify the result."

## What This Repo Now Is

Atticus Harness is a safe standalone legal AI factory control plane. It has durable state, evidence graph records, validation/certification, scheduling, leases, context packs, provider policy, budgets, worker candidate packets, reducer-only canonical writes, migration reporting, and CLI status UX.

## Current Safety State

- OpenClaw is not started by the harness.
- Live legal workers are blocked unless the OpenRouter-only live gates pass.
- A local-only `run-local` path exists for safe harness exercising; it uses `local_stub`, requires an active lease, writes only task-local JSON, records candidate output, and never writes canonical artifacts.
- A direct OpenRouter worker path exists, but it requires `ATTICUS_ENABLE_LIVE_OPENROUTER=1`, `OPENROUTER_API_KEY`, OpenRouter-only provider policy, fallback disabled, active leases, budget gates, and reducer-only candidate handoff.
- OpenRouter free-model failover is opt-in through `provider_policy.openrouter_failover.enabled` or `ATTICUS_OPENROUTER_FAILOVER_ENABLED=1`. When enabled, readiness validates every configured model (or the built-in free-model order if no custom list is supplied), probes through the same failover-aware client, and records final requested-model telemetry while still failing closed on provider/model drift.
- Live resume is planning-only: `provider-probe` and `live-resume` may inspect readiness and write leases, but they do not launch workers. `live-resume` requires either `--probe` or object-shaped `--probe-result-json`, and lease writing requires literal `ok: true` plus provider metadata and a model that matches either the task's single model or one of its configured failover models.
- External legal actions are blocked by policy.
- Matter-scoped `ask` and `rebuild-search-index` require `--matter` to match `ATTICUS_AUTHORIZED_MATTER` from the execution context; the default authorized matter is `atticus`.
- Legacy workspace/archive imports are candidate-only or rough-note-only.
- Provider fallback fails closed. OpenRouter responses must report provider/model metadata and valid usage token scalars before accounting; malformed post-dispatch responses still record provider/budget telemetry, fail the lease and attempt, block the task, and commit the audit.
- Worker outputs need valid leases; late outputs are quarantined.
- Budget gates are checked before local or OpenRouter execution and provider/budget telemetry is recorded. Pre-dispatch failures do not create provider spend records.
- Scheduler gate metadata now fails closed on corrupted-but-valid JSON shapes; readiness reports these tasks as blocked instead of crashing or leasing them.
- Legacy foundation reconciliation must certify source inventory, extraction coverage, evidence registry, production mapping, and chronology citations before later-stage live work is resumed.

## Verification Commands

```bash
python -m pytest -q
python -m compileall -q atticus tests
```

CLI smoke set:

```bash
atticus init --db /tmp/atticus-smoke.sqlite3
atticus status --db /tmp/atticus-smoke.sqlite3
atticus ask --db /tmp/atticus-smoke.sqlite3 --matter atticus "source inventory status"
atticus rebuild-search-index --db /tmp/atticus-smoke.sqlite3 --matter atticus --write
ATTICUS_AUTHORIZED_MATTER=beta atticus ask --db /tmp/atticus-smoke.sqlite3 --matter beta "beta source inventory status"
atticus import-candidates --workspace /home/alba/.openclaw/workspace-atticus-legal --db /tmp/atticus-smoke.sqlite3 --dry-run
atticus provider-policy --provider openrouter --model deepseek/deepseek-v4-pro
atticus budget --db /tmp/atticus-smoke.sqlite3 --scope-type matter --scope-id atticus
atticus validate --db /tmp/atticus-smoke.sqlite3 --gate source_inventory --target-type matter --target-id atticus
atticus reconcile-foundation --db /tmp/atticus-smoke.sqlite3 --dry-run
atticus schedule --db /tmp/atticus-smoke.sqlite3 --dry-run
atticus provider-probe --model deepseek/deepseek-v4-pro
ATTICUS_ENABLE_LIVE_OPENROUTER=1 atticus live-resume --db /tmp/atticus-smoke.sqlite3 --capacity 15 --probe --model deepseek/deepseek-v4-pro
atticus lease --db /tmp/atticus-smoke.sqlite3 --task-id <task-id> --worker-id atticus-local --write
atticus run-local --db /tmp/atticus-smoke.sqlite3 --task-id <task-id> --lease-id <lease-id> --worker-id atticus-local --output-dir /tmp/atticus-worker-output --write
atticus migrate-report --workspace /home/alba/.openclaw/workspace-atticus-legal --db /tmp/atticus-smoke.sqlite3 --dry-run
atticus doctor --db /tmp/atticus-smoke.sqlite3
```

## Next Best Work

1. Run `reconcile-foundation --write` on the migrated legacy DB, then inspect any failed gate details before live resume.
2. Use `provider-probe` or `live-resume --probe` immediately before writing live leases; do not reuse stale probe JSON across model/provider changes.
3. Add archive ledger importer for old `ledger.sqlite`.
4. Add file inventory scans.
5. Add FTS5-backed persistent memory indexes on top of the current citation-aware lexical search.
