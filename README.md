# Atticus Harness

Atticus Harness is a standalone, evidence-first legal AI control plane. It owns durable legal memory, task state, evidence provenance, validation, scheduling, provider policy, context packs, reducer review, budgets, and status reporting.

OpenClaw, Codex, Claude Code, direct OpenRouter, and other agents are execution adapters. They are not the source of truth.

## Implemented Architecture

- SQLite durable ledger with append-only event chain, mutable projections, and rebuildable legal-memory search indexes.
- Legal evidence graph tables for sources, source snapshots, artifact versions, dependencies, extraction/OCR/transcription records, production mappings, chronology events, issues, claims, legal authorities, citation spans, validations, and certifications.
- Read-only query path: `atticus ask`, `atticus status`, and `atticus inspect`.
- Active factory path: `schedule`, `lease`, `work-order`, `reduce`, `validate`, `certify`, budgets, provider policy, and human-attention queue.
- Worker/reducer boundary: workers can only create task-local candidate packets; reducers are the only canonical writers and require active leases plus passing validation.
- Dependency-aware S0-S9 scheduler with explicit source, artifact, task, matter, certification, stale-input, provider, and budget gates.
- Deterministic context packs with stable prefix sections, evidence/artifact bundles, token estimates, fingerprints, and cache accounting fields.
- Provider policy for DeepSeek/OpenRouter with fail-closed fallback and requested/actual model accounting.
- Candidate-only legacy migration with dry-run reports and validation tasks for imported material.
- Test suite for read-only ask, fallback failure, budget gates, stale propagation, reducer write authority, expired lease quarantine, deterministic context packs, migration trust, and OpenClaw non-launch safety.

## Safety Defaults

- `ask` is read-only and never launches workers or mutates canonical state.
- Matter-scoped query/rebuild commands authorize against the execution-context matter (`ATTICUS_AUTHORIZED_MATTER`, default `atticus`) before accepting `--matter`.
- Commands that could affect factory state default to dry-run or require explicit `--write` / `--write-context`.
- OpenClaw adapter launch is blocked in this package.
- External legal actions are policy-blocked: no emails, filings, uploads, court contact, party contact, or counsel contact.
- Legacy outputs import as `candidate`, `rough_note`, or rejected/noise. They are never certified automatically.
- Provider fallback fails closed unless explicitly allowed.
- OpenRouter free-model failover is opt-in (`openrouter_failover.enabled` or `ATTICUS_OPENROUTER_FAILOVER_ENABLED=1`) and rotates only across the configured ordered requested-model list, or the built-in free-model order when enabled without custom models; provider/model drift still fails closed.

## CLI

```bash
atticus init --db atticus.sqlite3
atticus status --db atticus.sqlite3
atticus inspect --db atticus.sqlite3 --type task --id task-1
atticus ask --db atticus.sqlite3 --matter atticus "What source indexes mention production status?"
atticus rebuild-search-index --db atticus.sqlite3 --matter atticus --write
ATTICUS_AUTHORIZED_MATTER=beta atticus ask --db atticus.sqlite3 --matter beta "What beta evidence is indexed?"
atticus import-candidates --workspace /home/alba/.openclaw/workspace-atticus-legal --db atticus.sqlite3 --dry-run
atticus validate --db atticus.sqlite3 --gate source_inventory --target-type matter --target-id atticus
atticus certify --db atticus.sqlite3 --subject-type matter --subject-id atticus --type source_inventory
atticus schedule --db atticus.sqlite3 --capacity 5 --dry-run
atticus lease --db atticus.sqlite3 --task-id task-1 --worker-id worker-1 --dry-run
atticus work-order --db atticus.sqlite3 --task-id task-1 --dry-run
atticus reduce --db atticus.sqlite3 --candidate-id cand-1 --lease-id lease-1 --dry-run
atticus budget --db atticus.sqlite3 --scope-type stage --scope-id S0 --limit 10 --write
atticus provider-policy --provider openrouter --model deepseek/deepseek-v4-pro
atticus human-attention --db atticus.sqlite3
atticus migrate-report --workspace /home/alba/.openclaw/workspace-atticus-legal --db atticus.sqlite3 --dry-run
atticus doctor --db atticus.sqlite3
```

OpenRouter failover can be enabled per task with `provider_policy.openrouter_failover` or via environment. If `ATTICUS_OPENROUTER_FAILOVER_MODELS` is omitted, Atticus uses the built-in free-model order from `OPENROUTER_FREE_MODEL_ORDER`:

```bash
ATTICUS_OPENROUTER_FAILOVER_ENABLED=1 \
ATTICUS_OPENROUTER_FAILOVER_MODELS="qwen/qwen3-coder:free,openai/gpt-oss-120b:free" \
atticus live-resume --db atticus.sqlite3 --probe --write-leases
```

The live gate validates every configured model, probes through the same failover path, and records the final requested model in provider telemetry.

## Development

```bash
python -m pytest -q
python -m compileall -q atticus tests
```

Tests do not hit live provider APIs and do not start OpenClaw.
