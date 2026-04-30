# Codex Prompt: Atticus Napier Matter GPT-5.5 Wiring

You are Codex working in the local git repository at:

```bash
/home/alba/atticus-harness
```

## Mission

Continue the Atticus harness wiring for Omer's Napier sister-case so the harness can support a first-class per-run model selection path, specifically provider `openai-codex` with model `gpt-5.5`, fallback disabled, and no accidental OpenRouter/free-model/DeepSeek fallback.

The goal is not a one-off SQL hack. Omer should be able to choose any supported model for a run or matter through a normal harness surface.

## Current state from Jake's inspection

Before changing anything, verify these facts yourself because the worktree is dirty and may have changed.

Repo state seen on 2026-04-28:

```text
branch: main...origin/main [ahead 1]
HEAD: c04b9c8 fix: complete OpenRouter failover retry semantics
origin/main: e45a0a8 feat: harden Atticus live and matter safety
```

Dirty worktree seen on 2026-04-28:

```text
Modified many tracked files under atticus/ and tests/
Added tracked-ish typing stubs:
  typings/json/__init__.pyi
  typings/sqlite3/__init__.pyi
Untracked:
  atticus/scheduler/free_loop.py
  data/
  matters/
  scripts/
  tests/test_free_loop.py
```

Do not reset, checkout, clean, delete, or overwrite dirty work. Do not run broad formatting that rewrites unrelated files. Make surgical edits only.

Matter paths:

```text
Matter workspace:
  /home/alba/atticus-harness/matters/napier-accommodation-arrears

Matter DB:
  /home/alba/atticus-harness/data/napier-accommodation-arrears.sqlite

Rich local inventory:
  /home/alba/atticus-harness/matters/napier-accommodation-arrears/02-registers/file_inventory.csv

Sparse source register:
  /home/alba/atticus-harness/matters/napier-accommodation-arrears/02-registers/source_register.csv

Existing extracted text directory:
  /home/alba/atticus-harness/matters/napier-accommodation-arrears/03-working/extracted-text/
```

Prior DB inspection reported:

```text
matters: 1
sources: 0
source_snapshots: 0
artifacts: 0
tasks: 0
runs: 1
leases: 0
candidate_outputs: 0
provider_runs: 0
human_attention: 0
validation_results: 0
certifications: 0
```

The existing matter row appeared to be default `atticus`, not a proper `napier-accommodation-arrears` scoped matter. The DB was therefore not runnable even though the matter folder has substantial files.

Relevant current code seams:

```text
atticus/cli.py
  Has provider-policy/policy-check, live-resume, run-free-loop, run-local.
  run-free-loop accepts runtime choices: openrouter, local, codex.

atticus/scheduler/free_loop.py
  New/untracked module.
  run_free_loop_once can call execute_codex_work_order when runtime == codex.

atticus/workers/runtime.py
  execute_codex_work_order currently fail-closes with:
  "Codex provider is policy-configurable, but live Codex CLI execution adapter is not implemented"

atticus/providers/deepseek.py
  CODEX_MODELS currently includes:
  gpt-5.5
  openai-codex/gpt-5.5

atticus/providers/policy.py
  check_provider_policy canonicalizes openai-codex/gpt-5.5 to gpt-5.5.
  Codex drift fails closed.
```

## Non-negotiable safety rules

1. Do not perform any live legal, filing, email, upload, contact, message, court, or external action.
2. Do not start a live GPT-5.5/Codex run unless all explicit live gates are implemented, tests pass, no-live readiness passes, and Omer or Jake explicitly approves the live spend.
3. Do not silently fall back from `openai-codex/gpt-5.5` to OpenRouter, free models, DeepSeek, local stub, or any other provider/model.
4. Preserve reducer-single-writer semantics. Workers may create candidate packets only. Canonical artifacts are written only through the reducer/canonical path.
5. Do not fake a run. If the Codex CLI adapter is unsafe or incomplete, keep it fail-closed with clear tests and report the blocker.
6. Do not reset or discard existing dirty work.
7. Do not store credentials, print credentials, or embed secrets in code, tests, prompts, logs, or commits.
8. Treat case data as local sensitive material. Do not transmit it anywhere except through an explicitly approved live provider path.
9. Run tests before and after changes. Use TDD for new behavior.

## Desired end state

The harness should support these normal operations:

1. Seed or repair a matter DB from a local matter workspace/inventory so it becomes scheduler-runnable.
2. Set or seed per-matter/per-run/per-task provider policy to:

```json
{
  "provider": "openai-codex",
  "model": "gpt-5.5",
  "allow_fallback": false,
  "estimated_cost_usd": 0.0
}
```

3. Enforce exact provider/model policy with fail-closed drift behavior.
4. Keep OpenRouter free failover disabled for this GPT-5.5 path.
5. Either:
   - implement a bounded, tested Codex CLI worker adapter, or
   - keep the Codex runtime blocked with clear tests and an operator-facing reason.
6. Provide exact no-live readiness commands and, if not actually run, the exact bounded live command Omer or Jake can run later.

## Required first steps

Run these inspections first and save useful output in your final report. Do not assume the prior context is still accurate.

```bash
cd /home/alba/atticus-harness

git status --short --branch
git log --oneline -5 --decorate

git diff --name-status
git diff --cached --name-status

python - <<'PY'
from pathlib import Path
import sqlite3, json

db = Path('data/napier-accommodation-arrears.sqlite')
print('db_exists', db.exists(), db)
if db.exists():
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    tables = [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]
    print('tables', tables)
    for name in ['matters','sources','source_snapshots','artifacts','tasks','runs','leases','candidate_outputs','provider_runs','human_attention','validation_results','certifications']:
        if name in tables:
            print(name, con.execute(f'select count(*) from {name}').fetchone()[0])
    if 'matters' in tables:
        print('matters_rows', [dict(r) for r in con.execute('select * from matters order by matter_scope')])
PY

python -m atticus.cli doctor --db data/napier-accommodation-arrears.sqlite || true
python -m atticus.cli schedule --db data/napier-accommodation-arrears.sqlite --capacity 5 --dry-run || true
```

Also inspect these files before editing:

```text
atticus/db/repo.py
atticus/core/tasks.py
atticus/core/policies.py
atticus/scheduler/gates.py
atticus/scheduler/planner.py
atticus/scheduler/free_loop.py
atticus/workers/runtime.py
atticus/adapters/codex_cli.py
atticus/adapters/base.py
atticus/providers/policy.py
atticus/providers/deepseek.py
atticus/workers/work_order.py
atticus/workers/outputs.py
atticus/workers/contracts.py
```

## Implementation tasks

### Task 1: Add tests for Napier matter seeding or repair

Write failing tests first. Use temporary DBs and temporary CSV inventories, not the real case data, for tests.

Acceptance criteria:

- Idempotent matter seeding creates or updates a matter row with scope `napier-accommodation-arrears`.
- Imports sources from `file_inventory.csv` or an equivalent inventory fixture.
- Creates source snapshots or tracked files if that is the native schema path.
- Adds at least one safe queued foundation task when the DB has zero tasks.
- All created tasks use matter_scope `napier-accommodation-arrears`.
- The seeding path does not create leases, candidate outputs, provider runs, or external actions.
- Running the seeder twice does not duplicate rows.

Suggested test file:

```text
tests/test_matter_seed.py
```

If an existing test module is a better fit, use it, but keep the test targeted.

### Task 2: Implement a native, reusable seeding surface

Prefer a first-class harness command or reusable module, not one-off manual SQL.

Acceptable shapes include one of these:

```bash
python -m atticus.cli seed-matter --db data/napier-accommodation-arrears.sqlite --matter napier-accommodation-arrears --workspace matters/napier-accommodation-arrears --inventory matters/napier-accommodation-arrears/02-registers/file_inventory.csv --provider openai-codex --model gpt-5.5 --write
```

or:

```bash
python -m atticus.cli matter-seed --db ... --matter ... --workspace ... --inventory ... --provider ... --model ... --write
```

You may choose better names if they fit the repo.

Requirements:

- Dry-run by default.
- `--write` required to mutate DB.
- Explicit `--matter` required.
- Explicit workspace/inventory input.
- Idempotent inserts or upserts.
- Does not read credentials.
- Does not call any provider.
- Emits JSON summary with counts created/updated/skipped.
- Preserves source hashes and paths from the inventory where available.
- If inventory rows point to missing files, report them as skipped or attention items, not crashes.

### Task 3: Add tests for first-class provider/model override

Write failing tests for normal per-run or per-matter model policy setting.

Acceptance criteria:

- Can set all queued tasks for a matter to provider `openai-codex`, model `gpt-5.5`, `allow_fallback=false` without editing global defaults.
- Alias `openai-codex/gpt-5.5` canonicalizes safely to `gpt-5.5` where policy expects it.
- Unknown provider/model is rejected before any live run.
- Drift from actual provider/model fails closed.
- `allow_fallback=true` for Codex path is rejected.
- No OpenRouter/free/DeepSeek fallback is introduced.

Suggested command surface:

```bash
python -m atticus.cli set-provider-policy --db <db> --matter <matter> --provider openai-codex --model gpt-5.5 --no-fallback --write
```

or integrate this into seeding if that is cleaner, but Omer's requirement is broader than seeding. He should be able to run the harness on any supported model he chooses.

### Task 4: Implement per-run/per-matter model policy minimally and safely

Implement only what the tests demand.

Requirements:

- Prefer task/provider policy fields already in the schema.
- Do not mutate global default failover lists to force GPT-5.5.
- Do not remove existing OpenRouter free failover support for other modes.
- Keep GPT-5.5 path isolated from free failover.
- Surface dry-run and write modes.
- Print JSON summaries.

### Task 5: Decide and test the Codex CLI adapter boundary

The current `execute_codex_work_order` fail-closes. You may either implement a bounded adapter or explicitly preserve fail-closed behavior. Do not leave an ambiguous half-implementation.

If implementing the adapter, tests must monkeypatch subprocess or adapter calls. Do not make live Codex calls in tests.

Adapter requirements if implemented:

- Active lease required.
- Worker ID must match the lease.
- Provider policy must be exactly `openai-codex` and model `gpt-5.5` or approved alias.
- Fallback disabled required.
- Explicit live env gate required, for example `ATTICUS_ENABLE_LIVE_CODEX=1`.
- CLI/runtime flag required, for example `allow_live=True` or `--allow-live`.
- Timeout required.
- Output directory must be sanitized through existing path helpers.
- Work order JSON in, candidate packet JSON out.
- Candidate packet parsed and passed through `record_worker_result`.
- If candidate output is quarantined, fail lease and task, record attempt failure, and do not mark success.
- Provider telemetry recorded after dispatch if a real Codex call occurred, including requested/actual provider/model and fallback result.
- No canonical artifacts written by the worker.
- No external legal actions.
- Exact Codex model flag must be verified from local `codex exec --help`; do not guess if the CLI changed. Likely model usage is `codex exec -m gpt-5.5`, but verify.

If preserving fail-closed behavior, tests must prove:

- `runtime=codex` blocks safely.
- The lease is failed or cleaned up.
- The task is blocked with a clear reason.
- No provider_runs are created.
- No candidate outputs are created.
- No OpenRouter/free fallback is attempted.

### Task 6: Wire free loop or CLI runtime only after tests

If Codex execution is implemented, update `run-free-loop --runtime codex` or an equivalent command to pass the Codex live gate and output directory safely.

If Codex execution remains blocked, ensure the CLI and final report make that explicit.

Do not run a live GPT-5.5 worker from this prompt without explicit approval.

## Verification commands

Run targeted tests first, then full verification.

```bash
cd /home/alba/atticus-harness

python -m pytest -q tests/test_worker_runtime.py tests/test_openrouter_failover.py tests/test_free_loop.py
python -m pytest -q tests/test_matter_seed.py || true
python -m pytest -q
python -m compileall -q atticus tests
git diff --check && git diff --cached --check
```

If pyright/basedpyright is part of the repo's expected gate and available, run it too:

```bash
basedpyright atticus tests --outputjson > /tmp/atticus-basedpyright.json || true
python - <<'PY'
import json
p='/tmp/atticus-basedpyright.json'
try:
    data=json.load(open(p))
    print(data.get('summary'))
except Exception as e:
    print('basedpyright summary unavailable', e)
PY
```

After seeding with `--write`, run no-live readiness:

```bash
python -m atticus.cli doctor --db data/napier-accommodation-arrears.sqlite
python -m atticus.cli schedule --db data/napier-accommodation-arrears.sqlite --capacity 5 --dry-run
python -m atticus.cli provider-policy --provider openai-codex --model gpt-5.5
python -m atticus.cli provider-policy --provider openai-codex --model openai-codex/gpt-5.5
python -m atticus.cli provider-policy --provider openai-codex --model gpt-5.5 --actual-provider openrouter --actual-model 'qwen/qwen3-coder:free' || true
```

Check no accidental live spend before any approved live run:

```bash
python - <<'PY'
from pathlib import Path
import sqlite3

db=Path('data/napier-accommodation-arrears.sqlite')
con=sqlite3.connect(db)
for name in ['tasks','leases','candidate_outputs','provider_runs','human_attention']:
    print(name, con.execute(f'select count(*) from {name}').fetchone()[0])
print('active_leases', con.execute("select count(*) from leases where status='active'").fetchone()[0])
if con.execute("select name from sqlite_master where type='table' and name='provider_runs'").fetchone():
    rows=con.execute('select requested_provider, requested_model, actual_provider, actual_model, fallback_allowed, fallback_policy_result from provider_runs order by created_at desc limit 10').fetchall()
    print('recent_provider_runs', rows)
PY
```

## Final report required

Your final response must include:

1. Files changed.
2. Tests run, exact commands, pass/fail results.
3. Whether the Napier DB is now runnable.
4. Counts for matters, sources, source_snapshots, artifacts, tasks, leases, candidate_outputs, provider_runs, human_attention.
5. Whether provider policy is pinned to `openai-codex/gpt-5.5` with fallback disabled.
6. Whether the Codex runtime is implemented or intentionally fail-closed.
7. Any live-spend risk remaining.
8. Exact command Omer or Jake should run next, but only as a command to run later unless live execution has been explicitly approved.

## Hard stop conditions

Stop and report without further changes if any of these happen:

- You cannot isolate your changes from unrelated dirty work.
- Tests reveal existing dirty work is too broad to safely modify.
- The DB schema differs from what the code expects and repair would be invasive.
- The Codex CLI cannot provide a bounded JSON-output worker contract.
- The only way to proceed would be a live provider call without explicit approval.
- Any command would expose secrets or case material outside the local machine.

## Reminder

This is a legal-harness safety task. Favor correctness, auditability, and fail-closed behavior over speed. If the right answer is "policy is wired but live Codex execution is not safe yet," say that clearly and preserve the block.