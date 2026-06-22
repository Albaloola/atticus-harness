# OpenCode Mission: Finish Atticus Live-Resume Hardening

You are OpenCode working in `LOCAL_PATH_REDACTED/atticus-harness`.

This prompt restores the track from the previous Atticus harness session. Continue from the current worktree exactly as it is. Do not reset, discard, rebase, or overwrite existing staged or unstaged work unless you can prove a change is wrong and you preserve the intent in a safer form.

## Current track summary

The repo is the standalone Atticus legal harness. It is meant to be the durable control plane for legal AI work, with OpenClaw treated as a possible execution adapter, not the owner of state.

Recent baseline commits:

```text
fda1f0c feat: add safe local harness runtime
aa8c1b8 chore: baseline atticus harness foundation
```

Current dirty worktree is an in-progress hardening pass for safe live OpenRouter resume. The pass already adds or modifies these areas:

```text
atticus/adapters/direct_openrouter.py
atticus/cli.py
atticus/migration/import_old_run.py
atticus/migration/reconcile.py
atticus/providers/live_readiness.py
atticus/providers/openrouter.py
atticus/scheduler/live_orchestrator.py
atticus/validation/gates.py
atticus/workers/runtime.py
docs/handoff.md
tests/test_cli_live_resume.py
tests/test_live_readiness.py
tests/test_migration_reconcile.py
tests/test_worker_runtime.py
```

Jake verified immediately before writing this prompt:

```bash
cd LOCAL_PATH_REDACTED/atticus-harness
python -m pytest -q
# 74 passed in 0.40s
python -m compileall -q atticus tests
# passed
git diff --check && git diff --cached --check
# passed
```

There are both staged and unstaged changes. Treat that as intentional history from the previous session. First inspect both layers with:

```bash
git status --short --branch
git diff --cached --stat
git diff --stat
git diff --cached --name-status
git diff --name-status
```

## Non-negotiable safety constraints

- Do not start OpenClaw.
- Do not start Atticus legal workers.
- Do not start any autonomous legal swarm.
- Do not run live OpenRouter calls or spend API money for legal work.
- Use fake clients and local test fixtures for provider/runtime tests.
- Do not file, email, upload, contact anyone, or perform external legal actions.
- Do not delete raw evidence or destructively rewrite the legacy workspace.
- Do not treat old legal outputs as certified. Legacy material is candidate-only until gates certify it.
- Preserve reducer-single-writer semantics: workers create candidates only, reducer writes canonical artifacts only.
- Preserve capacity discipline: target 15 agents only when 15 tasks are currently unblocked, otherwise under-fill.

## Architecture invariants to preserve

1. Live OpenRouter work must be fail-closed:
   - provider must be `openrouter`
   - model must be approved by OpenRouter policy: DeepSeek V4 Pro/Flash for single-model work, or an opt-in configured free-model failover list where every model is recognized
   - fallback must be disabled
   - `OPENROUTER_API_KEY` must exist
   - live runtime also requires `ATTICUS_ENABLE_LIVE_OPENROUTER=1`
   - a provider probe must succeed before live leases are written
   - probe success must be literal boolean `ok is True`, not truthy strings, numbers, or objects
   - reported provider and model metadata must be present and must match the final requested model for that attempt, including failover-selected models
2. Readiness reporting is separate from execution:
   - `live_readiness_report` and `atticus live-resume` can inspect and write leases, but must not launch workers
   - worker execution stays in a separate explicit runtime path
3. After any lease is acquired, every failure path must clean up capacity:
   - fail the lease
   - block or requeue the task as appropriate
   - commit the audit before raising if the outer DB context would roll back
4. If a provider call was dispatched, post-call validation failures must still record spending telemetry:
   - provider run row
   - budget entry or reservation/charge using preflight estimate
   - failed lease
   - failed attempt
   - blocked task with explicit reason
5. Foundation reconciliation must run before live resume:
   - source inventory
   - extraction/OCR/transcription coverage
   - evidence registry
   - production mapping
   - chronology citations
   - downstream tasks freeze if foundation fails
   - downstream tasks unfreeze only if the reconciliation-owned block is the only blocker

## Immediate objective

Finish the current live-resume hardening pass until it is ready to commit. Do not stop at green tests if code inspection still finds unsafe edge cases.

The current code already covers many pieces, including:

- `atticus/providers/live_readiness.py`
- `atticus/scheduler/live_orchestrator.py`
- `atticus/workers/runtime.py`
- `atticus/migration/reconcile.py`
- CLI commands: `provider-probe`, `live-resume`, `reconcile-foundation`
- tests for probe failures, metadata, no leases on failed probe, mixed-model filtering, under-fill, partial lease rollback, foundation reconciliation, and many lease-cleanup cases

Your job is to review, finish missing hardening, simplify repeated error paths where safe, and leave a verified commit.

## Known gaps to investigate and fix

### Gap 1: validate OpenRouter usage token scalar values

`OpenRouterClient.chat_json` currently ensures `usage` is a dict, but the runtime uses conversions like:

```python
int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
```

This can still accept or crash on bad scalars. Add validation for token fields before token accounting.

Requirements:

- Accept only integer token counts or numeric strings that represent whole non-negative integers if you deliberately choose to support strings.
- Reject booleans.
- Reject lists, dicts, objects, None for present fields, NaN, Infinity, negative values, and fractional numeric values.
- Reject malformed `total_tokens` if present too, even if input/output tokens are otherwise present.
- If malformed usage is detected after a provider call was dispatched, do not lose telemetry. Record provider run, charge configured budgets using the estimated cost, fail the lease, fail the worker attempt, block the task, commit, then raise a controlled `WorkerExecutionBlocked`.
- Add regression tests with fake OpenRouter clients for malformed usage shapes and scalar values. Include at least bool, list/dict, negative, fractional, NaN/Infinity where representable, and non-integer string if strings are supported or rejected.

### Gap 2: validate gate metadata shapes, not just JSON syntax

`atticus/scheduler/gates.py:evaluate_task_gates` currently loads JSON and iterates directly. It should reject corrupted-but-valid JSON shapes safely.

Examples to block without throwing uncaught exceptions:

- `source_dependencies_json = {}` or `123` or `false`
- `artifact_dependencies_json = {}` or `123` or `false`
- `task_dependencies_json = {}` or `123` or `false`
- `required_certifications_json = {}` or `123` or `false`
- `required_certifications_json = [123]`
- `required_certifications_json = [{"subject_type":"matter"}]` with missing subject or cert type should produce explicit malformed requirement reasons

Implement helper validation in `scheduler/gates.py` so `evaluate_task_gates` returns `GateResult(allowed=False, reasons=[...])` for corrupted valid JSON rather than depending on callers to catch exceptions.

Update `live_readiness_report` tests so these cases land in `blocked_tasks`, write no leases, and do not crash.

### Gap 3: make post-dispatch failure handling less duplicated and more consistent

`execute_openrouter_work_order` has repeated blocks that record provider runs, charge budgets, fail leases, update attempts, and block tasks. Consider extracting small helpers, but do not overbuild.

Acceptance criteria:

- The resulting code is easier to audit.
- Every post-dispatch failure path still records provider/budget telemetry.
- Every pre-dispatch failure path does not record provider spending.
- Existing tests remain meaningful and new tests cover the extracted behavior.

### Gap 4: ensure CLI behavior is safe and clear

Review CLI paths in `atticus/cli.py`:

- `provider-probe` should return nonzero on failed probe.
- `live-resume` should require `--probe` or `--probe-result-json`.
- malformed `--probe-result-json` should not write leases.
- non-object probe JSON such as `true` should not write leases.
- `reconcile-foundation --write` should return nonzero unless foundation is actually ready.

Add or adjust tests in `tests/test_cli_live_resume.py` if any behavior is missing.

### Gap 5: update docs and handoff after code is final

Update `docs/handoff.md` so it accurately reflects the final runtime safety state and the next best work. Keep it concise and operational.

If useful, add a short `docs/prompts/` or `docs/plans/` note only if it helps future handoff. Do not create a documentation pile.

## Required verification loop

Run these repeatedly until they pass after your final changes:

```bash
cd LOCAL_PATH_REDACTED/atticus-harness
python -m pytest -q
python -m compileall -q atticus tests
git diff --check
git diff --cached --check
```

Also run targeted tests while developing, for example:

```bash
python -m pytest -q tests/test_live_readiness.py tests/test_cli_live_resume.py tests/test_worker_runtime.py
python -m pytest -q tests/test_migration_reconcile.py
```

Before declaring done, inspect the final diff:

```bash
git diff --stat
git diff --cached --stat
git diff -- atticus/providers/live_readiness.py atticus/scheduler/live_orchestrator.py atticus/workers/runtime.py atticus/scheduler/gates.py atticus/providers/openrouter.py
git diff -- tests/test_live_readiness.py tests/test_worker_runtime.py tests/test_cli_live_resume.py
```

## Independent review requirement

After tests pass, do a fresh review pass as if you were hostile QA. Specifically check:

- any path after `acquire_lease` that can raise without failing or requeueing the lease/task
- any provider call dispatched before policy, budget, env opt-in, and cost checks
- any provider response failure after dispatch that skips provider run or budget telemetry
- any missing provider/model metadata defaulted to requested values
- any truthy probe `ok` accepted instead of literal `True`
- any live readiness path that can crash on corrupted but JSON-valid task metadata
- any worker writing canonical artifacts before reducer
- any command that can accidentally launch workers when it should only prepare leases

If you find a blocker, fix it and repeat the verification loop. Iterations are not limited. Continue until both tests and the hostile review are clean.

## Final deliverable

When done:

1. Commit the completed hardening pass in a single logical commit, unless the work naturally splits into two clean commits.
2. Do not commit raw evidence, archives, caches, pyc files, or unrelated generated material.
3. Leave the repo clean except for intentionally untracked local files, if any.
4. Final response must include:
   - commit hash or note that no commit was made and why
   - summary of safety fixes
   - tests run with exact results
   - remaining risks and next best work

Suggested commit message:

```text
fix: harden live OpenRouter resume gates
```

## Important context for legal domain

This is a legal case harness for User's redacted legal context matter and related Atticus work. Legal correctness and source provenance matter more than throughput. The goal is not to make 15 agents run. The goal is to run only the safe, currently unblocked work, and to keep legacy or stale outputs out of the trusted path until evidence-first gates certify them.
