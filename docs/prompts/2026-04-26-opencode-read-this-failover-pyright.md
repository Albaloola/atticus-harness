# OpenCode task: finish Atticus failover and strict error cleanup

Repo: `LOCAL_PATH_REDACTED/atticus-harness`

You are working on User's Atticus legal harness. This is safety-sensitive legal automation.

## Hard safety rules

- Do not run live OpenRouter calls.
- Do not launch OpenClaw workers.
- Do not enable cron jobs.
- Do not start live legal work.
- Use only unit tests, mocked provider responses, and local CLI smoke tests.
- Preserve reducer-single-writer semantics. Providers/workers may produce candidate packets only, never direct canonical legal artifacts.
- Preserve evidence-first gates, certification dependencies, lease checks, budget checks, and provider provenance checks.
- Do not relax OpenRouter provider/model metadata validation.
- Do not introduce Codex fallback or any non-OpenRouter fallback for live legal work.
- Do not hardcode API keys or secrets.
- Do not reset or discard work.

## Current baseline

Before this task, these were true:

- Branch: `main`
- HEAD: `e45a0a8 feat: harden Atticus live and matter safety`
- Runtime tests passed: `python -m pytest -q` gave `158 passed`.
- Compile passed: `python -m compileall -q atticus tests`.
- Whitespace checks passed: `git diff --check && git diff --cached --check`.
- Fresh DB smoke passed for `init`, `status`, and `doctor`.
- `basedpyright atticus tests` still had strict type errors.

## Required task A: fix failover semantics

File to inspect first:

- `atticus/providers/openrouter_failover.py`
- `tests/test_openrouter_failover.py`

The current failover implementation raises `OpenRouter failover exhausted` after `max_failed_cycles`. That is not the behavior User asked for.

Implement User's requested behavior exactly:

1. Use the ordered model list.
2. Try the current model.
3. If it succeeds, return the response and keep that model as preferred for the next request.
4. If it fails with a recoverable error, advance to the next model.
5. When the end of the list is reached, wrap to the first model.
6. Count one failed cycle only when every model in the list failed during that pass.
7. After `max_failed_cycles` complete failed cycles, default 5, wait `cooldown_seconds`, default 300 seconds.
8. After cooldown, reset to the first model and continue retrying from the top.
9. Do not raise exhaustion just because 5 cycles failed. The loop should continue after the cooldown.
10. Add an optional explicit guard for tests/callers, for example `max_total_attempts: int | None = None`. Default should allow continuous retry. If the guard is set and exceeded, raise a clear controlled error.
11. Hard errors must still stop immediately. Do not rotate forever on auth/config/input/context-length/schema errors.

Recoverable failures should include rate limits, HTTP 429, provider overload/unavailable, timeout, connection reset/network errors, 5xx, malformed/empty recoverable model responses, and OpenRouter provider unavailable errors.

Hard failures should include invalid API key, invalid request schema, missing messages/prompt, context length exceeded due to input being too large, permission/auth errors unrelated to rate limits, missing provider/model metadata where the existing safety policy treats that as provenance failure, and other unrecoverable caller/config problems.

Update or add tests covering:

- first model succeeds
- first model 429s, second succeeds
- wraparound from end to first
- after full-list failures equal to `max_failed_cycles`, cooldown runs, pointer resets to first model, and retry continues
- success after cooldown
- optional bounded guard raises only when explicitly configured
- hard errors stop immediately
- successful model remains preferred for next request

## Required task B: clean basedpyright errors

Run:

```bash
basedpyright atticus tests --outputjson > /tmp/basedpyright-atticus.json || true
python - <<'PY'
import json
j=json.load(open('/tmp/basedpyright-atticus.json'))
print(j['summary'])
for d in j['generalDiagnostics']:
    if d['severity'] == 'error':
        r=d['range']['start']
        print(f"{d['file']}:{r['line']+1}:{r['character']+1} {d.get('rule')} {d['message']}")
PY
```

Fix all diagnostics where `severity == error`. Warnings may remain.

Known previous errors included:

- `atticus/db/repo.py`: `int | None` passed to `int(...)` around lines 523 and 675.
- `atticus/graph/certifications.py`: raw `dict` missing type arguments.
- `atticus/graph/dependencies.py`: raw `dict` missing type arguments.
- `atticus/migration/report.py`: values typed as `object` used as if they have `artifact_type`, `trust_status`, `confidence`, and `matched_rule`.
- `atticus/reducer/council.py`: raw `dict` missing type arguments.
- `atticus/reducer/dissent.py`: raw `dict` missing type arguments.
- `atticus/retrieval/trust.py`: raw `dict` missing type arguments.
- `atticus/status/inspect.py`: raw `dict` missing type arguments.
- `atticus/status/report.py`: raw `dict` missing type arguments.
- `atticus/validation/claims.py`: raw `dict` missing type arguments.
- `atticus/validation/evidence.py`: raw `dict` missing type arguments.
- `atticus/workers/outputs.py`: raw `dict` missing type arguments.
- `tests/test_factory_contracts.py`: raw `dict` missing type arguments.
- `tests/test_foundation_contracts.py`: one call missing `conn`, `lease_id`, `task_id`.
- `tests/test_worker_runtime.py`: raw `dict` missing type arguments.

Use minimal, correct code/test edits. Do not silence basedpyright globally just to hide errors.

## Verification required

Run all commands below before finishing:

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

If all verification passes, commit:

```bash
git add atticus tests docs
 git commit -m "fix: complete OpenRouter failover retry semantics"
```

## Final report

Report:

1. Files changed.
2. Exact failover semantics after the fix.
3. Whether basedpyright errors are zero.
4. Test/verification command results.
5. Commit hash.
6. Remaining risks, especially any remaining basedpyright warnings.
