# OpenCode mission: finish Atticus failover semantics and clean strict diagnostics

Repo: `LOCAL_PATH_REDACTED/atticus-harness`

You are working on User's Atticus legal harness. This is safety-sensitive legal automation. Do not run live OpenRouter calls, do not launch OpenClaw workers, do not enable cron jobs, and do not start live legal work. Use only unit tests, mocked provider responses, and local CLI smoke tests.

## Current verified state before this mission

- Branch: `main`
- HEAD: `e45a0a8 feat: harden Atticus live and matter safety`
- Worktree was clean before this prompt was written.
- `python -m pytest -q` passed with `158 passed`.
- `python -m compileall -q atticus tests` passed.
- `git diff --check && git diff --cached --check` passed.
- Fresh DB CLI smoke passed for `init`, `status`, and `doctor`.
- `basedpyright atticus tests` still reports errors, mostly strict typing issues.

## Non-negotiable invariants

1. Preserve reducer-single-writer semantics. Workers and providers must produce candidate packets only, never direct canonical legal artifacts.
2. Preserve evidence-first gates, certification dependencies, lease checks, budget checks, and provider provenance checks.
3. Do not relax OpenRouter provider/model metadata validation.
4. Do not introduce Codex fallback or any non-OpenRouter fallback for live legal work.
5. Do not hardcode API keys or secrets.
6. Do not spend OpenRouter credits in tests. Use fake/mocked responses.
7. Do not discard, reset, or rewrite history unless explicitly required. Make a normal commit at the end if all verification passes.

## Required fixes

### A. Fix failover behavior to match User's exact requested semantics

Current implementation in `atticus/providers/openrouter_failover.py` has bounded exhaustion behavior: after `max_failed_cycles`, it raises `OpenRouter failover exhausted`.

User requested different behavior:

- Use the ordered model list.
- If a model fails, errors, times out, is rate limited, returns provider unavailable, 5xx, malformed/empty recoverable response, etc., switch to the next model.
- When the end of the list is reached, start again from the top.
- If the full list fails 5 times in a row, wait 5 minutes and then try again from the beginning.
- Continue this loop after cooldown rather than raising exhaustion immediately.
- Keep a safe bound available for tests/callers so unit tests never hang forever. A good design is an optional `max_total_attempts` or similar test/caller guard defaulting to `None` for production continuous retry. If a guard is configured and exceeded, then raise a clear controlled error.

Implement this cleanly and idiomatically.

Update tests in `tests/test_openrouter_failover.py` to cover:

1. First model succeeds.
2. First model 429s, second succeeds.
3. End-of-list wraparound.
4. After `max_failed_cycles` complete failed cycles, cooldown runs, pointer resets to first model, and retry continues.
5. A model succeeds after cooldown.
6. Optional bounded test guard raises only when explicitly configured.
7. Hard errors still stop immediately and do not rotate forever.
8. Successful model remains preferred for next request.

Do not weaken hard-error classification. Auth/config/input/context-length errors should still fail closed.

### B. Clean strict basedpyright errors, not necessarily all warnings

Run:

```bash
basedpyright atticus tests --outputjson > /tmp/basedpyright-atticus.json || true
```

Fix all current `severity == error` diagnostics. Warnings may remain if they are broad strictness warnings, but errors should be zero.

Known errors from the previous inspection included:

- `atticus/db/repo.py`: `int | None` passed to `int(...)` at lines around 523 and 675.
- `atticus/graph/certifications.py`: raw `dict` missing type arguments.
- `atticus/graph/dependencies.py`: raw `dict` missing type arguments.
- `atticus/migration/report.py`: object values used as if they have `artifact_type`, `trust_status`, etc.
- `atticus/reducer/council.py`: raw `dict` missing type arguments.
- `atticus/reducer/dissent.py`: raw `dict` missing type arguments.
- `atticus/retrieval/trust.py`: raw `dict` missing type arguments.
- `atticus/status/inspect.py`: raw `dict` missing type arguments.
- `atticus/status/report.py`: raw `dict` missing type arguments.
- `atticus/validation/claims.py`: raw `dict` missing type arguments.
- `atticus/validation/evidence.py`: raw `dict` missing type arguments.
- `atticus/workers/outputs.py`: raw `dict` missing type arguments.
- `tests/test_factory_contracts.py`: raw `dict` missing type arguments.
- `tests/test_foundation_contracts.py`: call missing `conn`, `lease_id`, `task_id`.
- `tests/test_worker_runtime.py`: raw `dict` missing type arguments.

Fix the actual code/tests with minimal edits. Do not silence basedpyright globally just to hide problems.

## Verification required

Run all of these before finishing:

```bash
python -m pytest -q
python -m compileall -q atticus tests
git diff --check && git diff --cached --check
basedpyright atticus tests --outputjson > /tmp/basedpyright-atticus.json || true
python - <<'PY'
import json
j=json.load(open('/tmp/basedpyright-atticus.json'))
print(j['summary'])
assert j['summary']['errorCount'] == 0, j['summary']
PY
rm -f /tmp/atticus-check.sqlite
python -m atticus.cli init --db /tmp/atticus-check.sqlite
python -m atticus.cli status --db /tmp/atticus-check.sqlite
python -m atticus.cli doctor --db /tmp/atticus-check.sqlite
```

If verification passes, commit with a concise message such as:

```bash
git add atticus tests docs
 git commit -m "fix: complete OpenRouter failover retry semantics"
```

## Final report required

Report:

1. Files changed.
2. Exact failover semantics after the fix.
3. Whether basedpyright errors are zero.
4. Test/verification commands and results.
5. Commit hash.
6. Remaining risks, especially any remaining basedpyright warnings.
