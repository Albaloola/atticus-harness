# Codex Prompt: Finish Atticus Model Routing, Cleanliness, and No-Issue Verification

You are Codex working in the Atticus harness repository.

Repository path:

```bash
LOCAL_PATH_REDACTED/atticus-harness
```

Date of prompt: 2026-04-28

## Mission

Finish the Atticus harness model-selection work properly. The user must be able to choose, per run and per layer/role/stage, which model is used. They must also be able to run everything on one model, or configure a pool of models that loops/falls back with an adjustable loop size. After the work, the codebase must be demonstrably green: tests, compile checks, diff checks, type checks where available, and no hidden live-spend or external-action behavior.

This is not a quick hack. Make model selection a first-class, validated, fail-closed harness feature.

## Hard safety constraints

Do not violate these:

1. Do not run live legal/provider spending unless explicitly required by an existing test using a fake client. No real OpenRouter, DeepSeek, Codex, OpenClaw, email, filing, contact, upload, or messaging calls.
2. Do not start OpenClaw.
3. Do not send or file anything externally.
4. Do not reset, discard, or overwrite dirty work. Preserve the current worktree.
5. Do not commit secrets. Do not print API keys.
6. Do not fake support for a provider/runtime. If a runtime adapter does not exist, the code must fail closed with a clear reason.
7. Workers produce candidate packets only. Reducer remains the single canonical writer.
8. Provider/model fallback is allowed only inside an explicitly configured pool. There must be no silent fallback to Codex, OpenRouter free models, DeepSeek, or any other provider/model.
9. A GPT-5.5 single-model Codex path must remain exact and fail closed on drift unless the user explicitly configures a pool that includes alternatives.

## Current checkpoint to preserve

The current branch/status at prompt creation was:

```text
## main...origin/main [ahead 1]
 M atticus/adapters/base.py
 M atticus/adapters/claude_code.py
 M atticus/adapters/codex_cli.py
 M atticus/adapters/direct_openrouter.py
 M atticus/adapters/local_stub.py
 M atticus/adapters/openclaw.py
 M atticus/cli.py
 M atticus/context/packs.py
 M atticus/core/events.py
 M atticus/core/tasks.py
 M atticus/db/repo.py
 M atticus/graph/certifications.py
 M atticus/graph/dependencies.py
 M atticus/graph/evidence.py
 M atticus/graph/staleness.py
 M atticus/migration/reconcile.py
 M atticus/migration/report.py
 M atticus/providers/budget.py
 M atticus/providers/deepseek.py
 M atticus/providers/live_readiness.py
 M atticus/providers/openrouter.py
 M atticus/providers/openrouter_failover.py
 M atticus/providers/policy.py
 M atticus/reducer/canonical_writer.py
 M atticus/reducer/council.py
 M atticus/reducer/dissent.py
 M atticus/reducer/reducer.py
 M atticus/retrieval/ask.py
 M atticus/retrieval/index.py
 M atticus/retrieval/rank.py
 M atticus/retrieval/search.py
 M atticus/retrieval/trust.py
 M atticus/scheduler/gates.py
 M atticus/scheduler/lease.py
 M atticus/scheduler/live_orchestrator.py
 M atticus/scheduler/planner.py
 M atticus/status/inspect.py
 M atticus/status/report.py
 M atticus/validation/claims.py
 M atticus/validation/evidence.py
 M atticus/validation/gates.py
 M atticus/workers/contracts.py
 M atticus/workers/outputs.py
 M atticus/workers/result_parser.py
 M atticus/workers/runtime.py
 M atticus/workers/work_order.py
 M tests/test_cli_live_resume.py
 M tests/test_factory_contracts.py
 M tests/test_foundation_contracts.py
 M tests/test_live_readiness.py
 M tests/test_migration_reconcile.py
 M tests/test_openrouter_failover.py
 M tests/test_worker_runtime.py
 A typings/json/__init__.pyi
 A typings/sqlite3/__init__.pyi
?? atticus/matter_seed.py
?? atticus/scheduler/free_loop.py
?? data/
?? docs/prompts/2026-04-28-codex-atticus-napier-gpt55.md
?? matters/
?? scripts/
?? tests/test_free_loop.py
?? tests/test_matter_seed.py
```

Recent commits:

```text
c04b9c8 (HEAD -> main) fix: complete OpenRouter failover retry semantics
e45a0a8 (origin/main) feat: harden Atticus live and matter safety
4b4559e feat: add rebuildable retrieval index
e005a0f fix: close Atticus live-resume safety gaps
9334496 fix: harden live OpenRouter resume gates
```

The CLI currently exposes:

```text
init,status,inspect,ask,rebuild-search-index,import-candidates,seed-matter,validate,certify,schedule,lease,work-order,run-local,reduce,budget,provider-policy,set-provider-policy,provider-probe,live-resume,run-free-loop,reconcile-foundation,policy-check,human-attention,migrate-report,doctor
```

Important current behavior:

- `seed-matter` and `set-provider-policy` exist.
- `provider_policy_json` is the current per-task policy storage point.
- `TaskSpec.provider_policy` is already a dict, so richer policy can be introduced without immediately requiring a schema migration.
- `atticus/providers/policy.py` currently validates flat `{provider, model, allow_fallback, estimated_cost_usd}` policies.
- User correction: all models except GPT-5.5 are provided through OpenRouter for this harness. Do not design around direct DeepSeek execution as a normal route.
- Current intended model syntaxes:
  - OpenRouter DeepSeek route: `provider="openrouter"`, `model="deepseek/deepseek-v4-flash"`
  - OpenRouter DeepSeek route: `provider="openrouter"`, `model="deepseek/deepseek-v4-pro"`
  - OpenRouter free/other routes: `provider="openrouter"`, `model="<openrouter model id>"`
  - Codex GPT-5.5 only: `provider="openai-codex"`, `model="gpt-5.5"` or alias `openai-codex/gpt-5.5`
  - Direct `provider="deepseek"` constants may exist as legacy/cost metadata, but they must not be selected by default or treated as live-executable unless a future direct adapter is explicitly implemented and tested.
  - OpenRouter free models are listed in `OPENROUTER_FREE_MODEL_ORDER`.
- `atticus/providers/openrouter_failover.py` already has OpenRouter-only loop/fallback machinery with configurable model list, max failed cycles, cooldown, timeout, backoff, jitter, and logging.
- `atticus/scheduler/free_loop.py` currently imports proposed tasks but `_provider_policy(...)` forces OpenRouter non-free models back to the default free model. That is not acceptable for the new user requirement.
- `atticus/workers/runtime.py` currently has:
  - `execute_local_work_order(...)`
  - `execute_openrouter_work_order(...)`
  - `execute_codex_work_order(...)`, which intentionally fails closed because the Codex CLI adapter is not implemented.
- `atticus/adapters/codex_cli.py` is only a placeholder.

The Napier sister-case DB was already seeded and verified no-live:

```text
LOCAL_PATH_REDACTED/atticus-harness/data/napier-accommodation-arrears.sqlite
```

Known no-live runnable task:

```text
napier-accommodation-arrears-foundation-source-inventory
```

Verified prior dry-run policy for that task:

```json
{
  "provider": "openai-codex",
  "model": "gpt-5.5",
  "allow_fallback": false,
  "estimated_cost_usd": 0.0
}
```

Do not assume live Codex execution works. It currently should fail closed until a real bounded adapter exists.

## User requirement to implement

The user wants Atticus to support all of these as normal configuration, not one-off hacks:

1. All layers run on one chosen model.
   - Example: everything on `openai-codex/gpt-5.5`.
   - Example: everything on OpenRouter DeepSeek Pro: `provider="openrouter"`, `model="deepseek/deepseek-v4-pro"`.
   - Example: everything on a single OpenRouter free model.

2. Each layer/role/stage can use a different model.
   - Examples of layers/roles to consider:
     - planner/scheduler
     - worker
     - subagent/default spawned worker
     - reducer
     - critic/hostile review
     - verifier/validation
     - council roles if applicable
   - Examples of legal stages to support:
     - S0 source inventory
     - S1 extraction/OCR/transcription
     - S2 evidence registry
     - S3 production status
     - S4 chronology
     - S5 issue-route mapping
     - S6 authority mapping
     - S7 hostile review
     - S8 draft preparation
     - S9 final quality gate

3. Subagent-spawned tasks must get an intentional model policy.
   - They should inherit from the parent task or use an explicit `subagent_default` route.
   - They must not silently fall back to the hard-coded OpenRouter free default.
   - If a worker proposes a provider/model in `proposed_tasks`, validate it against the active run/matter policy. If it is not allowed, reject or rewrite it to the configured default and record why.

4. Model pools with fallback/loop behavior must be configurable.
   - A pool is an ordered list of model profiles.
   - The loop size/cycle count must be adjustable.
   - If the pool has one model, the loop should still work as a one-model loop, subject to the configured cycle/cooldown rules.
   - OpenRouter free pool behavior should preserve the existing semantics: cycle through the ordered usable list, wrap to the top, and after `max_failed_cycles` full failed cycles cool down before retrying.
   - Pools should normally be OpenRouter-only. A cross-provider pool involving GPT-5.5/Codex is allowed only if every provider/runtime in that pool has a real safe adapter and the policy explicitly opts into cross-provider fallback. Otherwise fail closed with a clear error.

5. Different models may require different syntax and settings.
   - Encode that difference in validated model profiles, not in scattered string hacks.
   - The policy layer should understand provider, model, runtime/adapter, optional temperature/max-tokens/timeout, cost estimate, and fallback/pool settings.
   - Unknown provider/model combinations must fail closed.

## Recommended implementation shape

You may choose the exact file names, but prefer a small, testable policy module such as:

```text
atticus/providers/model_policy.py
```

Suggested data model:

```python
@dataclass(frozen=True)
class ModelProfile:
    profile_id: str
    provider: str
    model: str
    runtime: str
    allow_fallback: bool = False
    estimated_cost_usd: float = 0.0
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_seconds: float | None = None
    capabilities: tuple[str, ...] = ()

@dataclass(frozen=True)
class ModelPool:
    pool_id: str
    profile_ids: tuple[str, ...]
    strategy: str = "fallback_loop"
    max_failed_cycles: int = 5
    cooldown_seconds: float = 300.0
    per_model_timeout_seconds: float | None = None
    backoff_seconds: float | None = None
    jitter_seconds: float | None = None

@dataclass(frozen=True)
class ModelRoutingPolicy:
    profiles: dict[str, ModelProfile]
    pools: dict[str, ModelPool]
    default: str
    layers: dict[str, str]
    stages: dict[str, str]
    task_types: dict[str, str]
    task_ids: dict[str, str]
```

The route target can be either a `profile_id` or a `pool_id`.

Suggested resolution precedence:

1. exact task id
2. task type
3. explicit layer/role
4. legal stage
5. matter default if you add it
6. global default

Backward compatibility is mandatory:

- Existing flat task policy JSON must still work:

```json
{
  "provider": "openai-codex",
  "model": "gpt-5.5",
  "allow_fallback": false,
  "estimated_cost_usd": 0.0
}
```

- Convert it internally into a single-profile routing policy when needed.
- Do not break `provider-policy`, `policy-check`, `set-provider-policy`, `work-order`, `run-free-loop`, or existing tests.

## Suggested policy-file example to support

Create fixtures/tests around a shape like this. Adjust names if you choose a cleaner schema, but preserve the capabilities.

```json
{
  "version": 1,
  "profiles": {
    "gpt55_codex": {
      "provider": "openai-codex",
      "model": "gpt-5.5",
      "runtime": "codex",
      "allow_fallback": false,
      "estimated_cost_usd": 0.0,
      "capabilities": ["legal_reasoning", "coding_agent"]
    },
    "deepseek_flash_or": {
      "provider": "openrouter",
      "model": "deepseek/deepseek-v4-flash",
      "runtime": "openrouter",
      "allow_fallback": false,
      "estimated_cost_usd": 0.01,
      "capabilities": ["triage", "indexing", "structured_extraction"]
    },
    "deepseek_pro_or": {
      "provider": "openrouter",
      "model": "deepseek/deepseek-v4-pro",
      "runtime": "openrouter",
      "allow_fallback": false,
      "estimated_cost_usd": 0.03,
      "capabilities": ["legal_reasoning", "hostile_review", "synthesis"]
    },
    "qwen_free": {
      "provider": "openrouter",
      "model": "qwen/qwen3-coder:free",
      "runtime": "openrouter",
      "allow_fallback": false,
      "estimated_cost_usd": 0.0
    }
  },
  "pools": {
    "free_loop": {
      "strategy": "fallback_loop",
      "profiles": ["qwen_free"],
      "max_failed_cycles": 5,
      "cooldown_seconds": 300.0,
      "per_model_timeout_seconds": 120.0,
      "backoff_seconds": 0.25,
      "jitter_seconds": 0.25
    }
  },
  "routes": {
    "default": "gpt55_codex",
    "layers": {
      "worker": "deepseek_flash_or",
      "subagent": "deepseek_flash_or",
      "reducer": "deepseek_pro_or",
      "hostile_review": "deepseek_pro_or",
      "verifier": "deepseek_pro_or"
    },
    "stages": {
      "S0": "deepseek_flash_or",
      "S1": "deepseek_flash_or",
      "S6": "deepseek_pro_or",
      "S7": "deepseek_pro_or",
      "S8": "gpt55_codex"
    },
    "task_types": {
      "source_inventory": "deepseek_flash_or"
    }
  }
}
```

Also add a simpler all-one-model file, for example:

```json
{
  "version": 1,
  "profiles": {
    "all": {
      "provider": "openai-codex",
      "model": "gpt-5.5",
      "runtime": "codex",
      "allow_fallback": false,
      "estimated_cost_usd": 0.0
    }
  },
  "routes": {
    "default": "all"
  }
}
```

## CLI/API expectations

Add the smallest clean command surface that supports the requirements.

Acceptable options include either:

1. Extend existing commands:
   - `set-provider-policy --policy-file <json> --matter <matter> [--stage S0] [--task-type source_inventory] --write`
   - `run-free-loop --model-policy-file <json> ...`

or:

2. Add new explicit commands:
   - `model-policy validate --policy-file <json>`
   - `model-policy resolve --policy-file <json> --task-id ... --stage ... --layer worker`
   - `set-model-policy --db ... --matter ... --policy-file ... --write`

Choose the cleaner option for this codebase, but ensure:

- There is a no-write dry-run mode.
- The chosen policy is persisted/audited when it changes queued tasks.
- `work-order --dry-run` exposes the resolved provider/model/pool in a way User can inspect.
- `provider_runs` records requested profile/pool/provider/model and actual provider/model.
- Unsupported runtime/provider combinations produce a controlled fail-closed error, not a crash.

## Runtime expectations

Do not implement broad live provider execution by accident.

Required runtime behavior:

1. OpenRouter single model still works through the existing OpenRouter path.
2. OpenRouter pool/fallback should reuse or generalize `atticus/providers/openrouter_failover.py` rather than duplicating it.
3. DeepSeek V4 Pro/Flash should be routed through OpenRouter model IDs, not a direct DeepSeek runtime. If `provider="deepseek"` exists as legacy metadata, do not select it in generated policies and do not treat it as live-executable for this requirement.
4. Codex GPT-5.5 support must remain fail-closed unless you implement the bounded Codex CLI adapter fully.
5. If you implement Codex CLI execution, it must be tightly bounded:
   - active lease required
   - explicit opt-in env var required, such as `ATTICUS_ENABLE_LIVE_CODEX=1`
   - exact model passed to Codex CLI, such as `gpt-5.5`
   - work-order JSON input
   - candidate-packet JSON output
   - no external actions
   - no repository-wide destructive operations
   - output path sanitized under task-local output dir
   - provider run and attempt telemetry recorded
   - reducer remains the only canonical writer
   - tests use fake subprocess/client, not live Codex

If full Codex execution is too much for this pass, do not fake it. Implement the model-routing/pool policy first and leave Codex runtime fail-closed with a clear, tested message.

## TDD requirements

Use strict TDD for behavior changes:

1. Write a failing test for the behavior.
2. Run the targeted test and observe the expected failure.
3. Implement the minimum code.
4. Run the targeted test and observe it passing.
5. Refactor only after green.
6. Run broader tests.

Do not add production behavior without tests.

Add tests for at least these cases:

1. Legacy flat provider policy remains valid and unchanged.
2. `openai-codex/gpt-5.5` canonicalizes to provider `openai-codex`, model `gpt-5.5`.
3. Unknown provider/model is rejected.
4. All-one-model policy resolves every stage/layer/subagent route to the same profile.
5. Layer-specific policy resolves worker/reducer/subagent differently.
6. Stage-specific policy resolves S0 differently from S7/S8.
7. Task-specific policy overrides stage/layer/default.
8. OpenRouter DeepSeek syntax is accepted for `deepseek/deepseek-v4-flash` and `deepseek/deepseek-v4-pro`.
9. Direct DeepSeek syntax is not used for this requirement. If legacy direct DeepSeek constants remain in the code, tests must prove generated policies prefer OpenRouter IDs and direct DeepSeek runtime is not silently selected.
10. A one-model pool loops or resolves as a pool without switching to a hard-coded default.
11. A multi-model pool respects ordered model list and adjustable `max_failed_cycles`/cooldown settings.
12. Pool fallback is limited to explicitly declared pool members.
13. Codex fallback remains disallowed for single GPT-5.5 policy.
14. Cross-provider pools involving Codex fail closed unless the policy explicitly opts into cross-provider fallback and every selected runtime has a safe adapter.
15. Proposed subagent tasks inherit the active parent/subagent policy and are not forced to `DEFAULT_FREE_MODEL`.
16. Proposed task policy outside the allowed run/matter policy is rejected or normalized with an audit reason.
17. `set-provider-policy` or the new model-policy command has dry-run and write tests.
18. `work-order --dry-run` includes the resolved model policy enough for audit.
19. `run-free-loop` does not make provider calls when capacity is 0.
20. No live provider/client/subprocess is invoked in tests.

## Cleanliness and verification requirements

Start by inspecting current state:

```bash
cd LOCAL_PATH_REDACTED/atticus-harness
git status --short --branch
git diff --name-status
git diff --cached --name-status
git log --oneline -5 --decorate
python -m atticus.cli --help
```

After implementation, run this full verification set:

```bash
cd LOCAL_PATH_REDACTED/atticus-harness
python -m pytest -q
python -m compileall -q atticus tests
git diff --check
git diff --cached --check
```

If `basedpyright` is available, run:

```bash
basedpyright atticus tests --outputjson > /tmp/atticus-basedpyright-model-routing.json
python - <<'PY'
import json
p='/tmp/atticus-basedpyright-model-routing.json'
data=json.load(open(p))
print(data.get('summary', {}))
raise SystemExit(0 if data.get('summary', {}).get('errorCount', 0) == 0 else 1)
PY
```

Run no-live CLI smokes against the Napier DB:

```bash
python -m atticus.cli doctor --db data/napier-accommodation-arrears.sqlite
python -m atticus.cli schedule --db data/napier-accommodation-arrears.sqlite --capacity 5 --dry-run
python -m atticus.cli work-order --db data/napier-accommodation-arrears.sqlite --task-id napier-accommodation-arrears-foundation-source-inventory --dry-run
```

Add model-policy CLI smoke commands based on the command surface you implement, for example:

```bash
python -m atticus.cli model-policy validate --policy-file <fixture-or-temp-json>
python -m atticus.cli model-policy resolve --policy-file <fixture-or-temp-json> --stage S0 --layer worker --task-type source_inventory
```

If you do not add a `model-policy` command, provide equivalent smoke commands using the extended existing commands.

Also check that no live work was started:

```bash
ps -eo pid,ppid,stat,etime,cmd | grep -Ei '[c]odex exec|[a]tticus|[o]penclaw.*atticus' || true
```

Do not use this process check as permission to kill unrelated user processes. Report anything suspicious.

## Git cleanliness expectations

The repository is dirty before this task. Do not pretend it is clean.

Your goal is:

1. Preserve all existing work.
2. Make source/tests/docs internally consistent.
3. Ensure generated/case data is either intentionally tracked, intentionally ignored, or explicitly reported as untracked.
4. Avoid adding `data/` or `matters/` wholesale to a commit unless the repository already treats them as fixtures or the user explicitly asked for that.
5. If all verification passes, commit source/test/docs changes that are safe to commit.
6. If you cannot safely commit because of mixed generated/case data, leave a precise final report listing:
   - what changed
   - what remains untracked
   - what should not be committed
   - exact tests/checks run
   - exact blockers

Do not run `git reset`, `git checkout --`, or destructive cleanup commands.

## Final deliverable required from Codex

Your final response must include:

1. Summary of implemented model routing/pool behavior.
2. Exact files changed.
3. Exact CLI commands now supported for:
   - all-on-one-model
   - per-layer/per-stage model selection
   - pool/fallback loop with adjustable cycle size
4. Whether Codex live execution remains fail-closed or is fully implemented.
5. Whether all non-GPT-5.5 models are routed through OpenRouter, and whether any legacy direct DeepSeek path remains fail-closed/not selected.
6. Exact verification commands run and their outputs.
7. Current `git status --short --branch`.
8. Whether a commit was created, with commit hash if yes.
9. Any remaining risks or limitations.

Remember: the user wants flexible model choice, but the harness must remain evidence-first, reducer-single-writer, and fail-closed. Make flexibility safe and auditable, not implicit.
