# Codex Mission: Atticus Safety Hardening Before Live Resume

You are Codex working in `LOCAL_PATH_REDACTED/atticus-harness`.

This repo is the standalone Atticus legal harness. It is intended to be the durable control plane for legal AI work. OpenClaw, Codex, Claude Code, and direct OpenRouter are execution adapters only. They are not the source of truth.

## Current verified state

Jake inspected the repo on 2026-04-26.

Current commits:

```text
9334496 fix: harden live OpenRouter resume gates
fda1f0c feat: add safe local harness runtime
aa8c1b8 chore: baseline atticus harness foundation
```

Current worktree before this prompt was written:

```text
## main
?? docs/prompts/2026-04-26-opencode-atticus-live-hardening.md
```

Verification immediately before this prompt:

```bash
cd LOCAL_PATH_REDACTED/atticus-harness
python -m pytest -q
# 107 passed
python -m compileall -q atticus tests
git diff --check
git diff --cached --check
# passed
```

This prompt itself may now be an additional untracked file under `docs/prompts/`. Preserve both prompt files unless you deliberately replace this one with a better handoff.

## Non-negotiable safety constraints

- Do not start OpenClaw.
- Do not start Atticus legal workers.
- Do not start any autonomous legal swarm.
- Do not run live OpenRouter calls.
- Do not spend API money.
- Use fake clients, monkeypatching, local fixtures, and unit tests for provider/runtime behaviour.
- Do not file, email, upload, contact anyone, or perform external legal actions.
- Do not delete raw evidence or destructively rewrite the legacy workspace.
- Do not treat old legal outputs as certified. Legacy material is candidate-only until gates certify it.
- Preserve reducer-single-writer semantics: workers create candidates only, reducer writes canonical artifacts only.
- Preserve capacity discipline: target 15 agents only when 15 tasks are currently unblocked. Otherwise under-fill.
- Do not reset, discard, rebase, or overwrite existing work unless you can prove it is wrong and preserve the intent in a safer form.

## Mission

Finish a focused safety-hardening pass before any live Atticus resume.

The harness is much improved and tests are green, but Jake found these remaining readiness blockers and concerns:

1. Canonical writer bypass: `atticus/reducer/canonical_writer.py` can write canonical text with only `writer_role`, and `canonical_write_guard.py` allows `canonical_writer` as a reducer role. It does not require a DB connection, active reducer lease, task ID, or validation context.
2. Provider probe spend gate: `provider-probe` and `live-resume --probe` can hit OpenRouter with only `OPENROUTER_API_KEY`. They do not require `ATTICUS_ENABLE_LIVE_OPENROUTER=1` or a separate explicit probe-spend opt-in.
3. Stale active leases can block future acquisition because `acquire_lease()` checks for active leases before expiring old ones. `expire_leases()` exists but is not integrated into live-resume/acquire paths.
4. Some post-dispatch failure telemetry paths still default missing actual provider/model metadata to requested values, which can hide provenance drift in malformed-response cases.
5. Add regression tests for all of the above.

Do not expand scope into new legal features. This is a safety hardening pass only.

## Required fixes

### Fix 1: Enforce reducer-only canonical writes

Files to inspect first:

```text
atticus/validation/canonical_write_guard.py
atticus/reducer/canonical_writer.py
atticus/reducer/reducer.py
atticus/validation/gates.py
tests/test_foundation_contracts.py
tests/test_reducer_council.py
tests/test_worker_runtime.py
```

Requirements:

- Remove the role-only bypass. A canonical write must require an active reducer-authorized context.
- Do not allow `writer_role="canonical_writer"` as a standalone bypass unless it is backed by the same active reducer lease/context checks as `writer_role="reducer"`.
- Prefer a strict API shape for `write_canonical_text()` that requires at least:
  - `conn`
  - `lease_id`
  - `task_id` or `reducer_task_id`
  - reducer role
- `assert_canonical_write_allowed()` must fail closed if canonical write code does not provide DB and lease context.
- Non-reducer workers must never be able to write canonical files.
- Existing reducer tests must still pass, or be adjusted to the safer API.
- Add tests that prove direct role-only `write_canonical_text(writer_role="canonical_writer", ...)` is rejected.

Acceptance criteria:

- There is no code path where a caller can write a canonical file by passing only a role string.
- Tests cover both rejection and the intended reducer-authorized path.

### Fix 2: Make provider probe spending explicitly opt-in

Files to inspect first:

```text
atticus/cli.py
atticus/providers/live_readiness.py
tests/test_cli_live_resume.py
tests/test_live_readiness.py
```

Problem:

`provider-probe` and `live-resume --probe` make a real OpenRouter request if a key exists. This is intentionally tiny, but it is still spending. Current live worker execution requires `ATTICUS_ENABLE_LIVE_OPENROUTER=1`, while probe does not.

Requirements:

Choose one safe design and implement it consistently:

Option A, preferred:

- Require `ATTICUS_ENABLE_LIVE_OPENROUTER=1` for `provider-probe` and `live-resume --probe` too.
- If the env var is missing, return nonzero and do not call the client.

Option B, acceptable only if documented and tested:

- Introduce a separate explicit probe-spend opt-in, for example `ATTICUS_ENABLE_OPENROUTER_PROBE=1` or `--allow-provider-spend`.
- The default must not spend.
- `live-resume --probe` must also require that explicit opt-in.

Acceptance criteria:

- A missing opt-in cannot result in a real provider call.
- Tests use fake clients or monkeypatches and assert no call is made without opt-in.
- CLI returns nonzero and prints clear JSON for blocked probe attempts.
- `live-resume --probe-result-json` can still use preverified JSON without making a call, but it must still enforce literal `ok is True` and matching provider/model.

### Fix 3: Expire stale active leases before acquiring new leases

Files to inspect first:

```text
atticus/scheduler/lease.py
atticus/scheduler/live_orchestrator.py
atticus/cli.py
tests/test_worker_runtime.py
tests/test_live_readiness.py
```

Requirements:

- Before `acquire_lease()` rejects an active lease for the same task, expire any active leases whose `expires_at` is in the past.
- Ensure task status is put back to queued only when that is safe and matches existing `expire_leases()` semantics.
- Live resume should not be blocked by expired leases.
- Avoid broad cleanup side effects that mutate unrelated tasks unexpectedly. If you expire globally, document it and test it. If you expire only for the task being acquired, make that explicit.

Acceptance criteria:

- Regression test: create a task with an expired active lease, call `acquire_lease()`, and assert the old lease is expired and a new active lease is acquired.
- Regression test: live resume can lease a task after its stale lease is expired.
- No active lease capacity leaks after failure.

### Fix 4: Do not default missing actual provider/model metadata to requested metadata

Files to inspect first:

```text
atticus/workers/runtime.py
atticus/providers/openrouter.py
tests/test_live_readiness.py
```

Problem:

Some post-dispatch failure telemetry paths use defaults like:

```python
actual_provider=str(response.get("provider") or "openrouter")
actual_model=str(response.get("model") or requested.model)
```

That can hide the fact that actual provider/model metadata was missing.

Requirements:

- If actual provider/model metadata is missing after dispatch, record telemetry as explicit missing values, for example `missing`, not as the requested provider/model.
- Keep the existing rule: post-dispatch failures must still record provider run telemetry and budget charges using the estimate.
- Do not weaken `OpenRouterClient.chat_json()`. It should continue rejecting missing metadata.
- Fake-client tests should exercise malformed responses through `execute_openrouter_work_order()`.

Acceptance criteria:

- Regression tests assert provider run rows show missing provenance explicitly for malformed post-dispatch metadata cases.
- No missing metadata is silently defaulted to requested values in failure telemetry.

## Required verification loop

Run targeted tests while developing:

```bash
cd LOCAL_PATH_REDACTED/atticus-harness
python -m pytest -q tests/test_live_readiness.py tests/test_cli_live_resume.py tests/test_worker_runtime.py tests/test_foundation_contracts.py tests/test_reducer_council.py
```

Then run the full verification loop:

```bash
python -m pytest -q
python -m compileall -q atticus tests
git diff --check
git diff --cached --check
```

Also inspect the final diff:

```bash
git diff --stat
git diff -- atticus/validation/canonical_write_guard.py atticus/reducer/canonical_writer.py atticus/cli.py atticus/providers/live_readiness.py atticus/scheduler/lease.py atticus/scheduler/live_orchestrator.py atticus/workers/runtime.py
git diff -- tests/test_live_readiness.py tests/test_cli_live_resume.py tests/test_worker_runtime.py tests/test_foundation_contracts.py tests/test_reducer_council.py
```

## Independent hostile review before committing

After tests pass, do a fresh hostile review of your own diff. Specifically check:

- any canonical write path that accepts only a role string
- any worker path that can write canonical artifacts
- any probe path that can spend without explicit opt-in
- any `live-resume --probe` path that can spend without explicit opt-in
- any path after `acquire_lease()` that can raise without failing or requeueing the lease/task when appropriate
- any expired active lease that blocks capacity forever
- any provider call dispatched before policy, budget, env opt-in, and cost checks
- any provider response failure after dispatch that skips provider run or budget telemetry
- any missing provider/model metadata defaulted to requested values
- any truthy probe `ok` accepted instead of literal `True`
- any live readiness path that can crash on corrupted but JSON-valid task metadata

If you find a blocker, fix it and repeat the verification loop. Iterations are not limited.

## Commit requirements

When done:

1. Commit the completed hardening pass in one logical commit.
2. Do not commit raw evidence, archives, caches, pyc files, or unrelated generated material.
3. Preserve existing untracked prompt files unless intentionally committing them is useful and appropriate.
4. Leave the repo clean except for intentionally untracked local prompt files if you choose not to commit them.
5. Final message must include:
   - commit hash
   - exact tests run and results
   - safety fixes summary
   - any remaining risks
   - whether live Atticus resume is still blocked

Suggested commit message:

```text
fix: close Atticus live-resume safety gaps
```

## Suggested command to run this mission

From User's machine:

```bash
cd LOCAL_PATH_REDACTED/atticus-harness && codex exec --full-auto "$(cat docs/prompts/2026-04-26-codex-atticus-safety-hardening.md)"
```

If using a background/long-running Codex invocation, also write the final message to a file:

```bash
cd LOCAL_PATH_REDACTED/atticus-harness && codex exec --full-auto --output-last-message codex-last-message.md "$(cat docs/prompts/2026-04-26-codex-atticus-safety-hardening.md)"
```
