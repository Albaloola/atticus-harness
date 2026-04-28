# Atticus Harness

Atticus Harness is a standalone, evidence-first legal AI control plane. It owns
durable legal memory, matter-scoped evidence, source snapshots, task state,
context packs, validation, provider policy, budgets, leases, reducer review,
audit events, and status reporting.

OpenClaw, Codex, Claude Code, OpenRouter, and other agents are execution
adapters at the edge. They are not the source of truth.

## Core Doctrine

- Evidence before argument.
- Context must be matter-scoped and inspectable.
- Model output is candidate material until validation and reducer acceptance.
- Workers may create candidate packets only.
- Reducers are the only canonical writers.
- Provider/model routing is explicit and fail-closed.
- External legal actions are blocked unless a future safe design explicitly
  supports them and the operator approves the exact action.
- Memory is an operational projection, not proof.

## Implemented Architecture

- SQLite durable ledger with append-only event chain, mutable projections, and
  rebuildable legal-memory search indexes.
- Legal evidence graph for sources, source snapshots, artifact versions,
  dependencies, extraction/OCR/transcription records, production mappings,
  chronology events, issues, claims, authorities, citation spans, validations,
  and certifications.
- Read-only query path: `ask`, `status`, `inspect`, `context`, `commands`,
  `tools`, `workflow list`, and `session` inspection.
- Active factory path: `schedule`, `lease`, `work-order`, `reduce`, `validate`,
  `certify`, budgets, provider policy, and human-attention queue.
- Strict `worker_result_packet.v2` validation with findings, citations,
  proposed artifacts, proposed tasks, uncertainties, contradictions, risk
  flags, redaction flags, and blocked external action requests.
- Dependency-aware S0-S9 scheduler with source, artifact, task, matter,
  certification, stale-input, provider, and budget gates.
- Deterministic context pack v2 section registry with fingerprints, token
  estimates, prompt-cache telemetry fields, evidence manifests, artifacts,
  authorities, memory index, validation gates, skills, tools, and required
  output schema.
- First-class model routing for OpenRouter-hosted models, explicit OpenRouter
  fallback pools, and exact Codex GPT-5.5 policy with requested/actual
  provider/model accounting.
- Bounded Codex CLI adapter with strict live gates and JSON candidate-packet
  output.
- Typed legal tool kernel with read-only and guarded mutating tools.
- Read-before-write draft artifact editing with content hashes and artifact
  versions.
- Markdown legal workflows for repeatable task graphs.
- Legal coordinator mode for self-contained, verifier-aware task planning.
- Typed legal memory taxonomy, reducer-gated memory extraction, and dry-run
  case memory consolidation.
- Session transcript persistence and internal lifecycle hooks.
- Candidate-only legacy migration with dry-run reports and validation tasks.

## Safety Defaults

- `ask` is read-only and never launches workers or mutates canonical state.
- Matter-scoped query/rebuild commands authorize against
  `ATTICUS_AUTHORIZED_MATTER` before accepting `--matter`.
- Commands that can mutate state default to dry-run or require explicit
  `--write`, `--write-context`, or equivalent.
- OpenClaw adapter launch is blocked in this package.
- External legal actions are policy-blocked: no emails, filings, uploads,
  service, court contact, party contact, counsel contact, or third-party
  messages.
- Legacy outputs import as `candidate`, `rough_note`, or rejected/noise. They
  are never certified automatically.
- Provider fallback fails closed unless configured through an explicit model
  pool.
- Prompt-bearing surfaces tell workers that output is candidate, not canonical,
  and require facts, law, procedure, inference, contradiction, risk, drafting
  notes, and uncertainty to remain distinct and citation-bound.
- Session resume is transcript-only and must not replay provider calls.
- Lifecycle hooks are internal Python checks. They block external legal action
  and cross-matter context, warn on stale evidence, and block final drafting
  where required hostile-review certification is missing.

## Repository And Workspaces

- Harness repo: `/home/alba/atticus-harness`
- Atticus OpenClaw workspace:
  `/home/alba/.openclaw/workspace-atticus-legal`
- Atticus workspace harness skill:
  `/home/alba/.openclaw/workspace-atticus-legal/.agents/skills/atticus-harness-mastery/SKILL.md`
- Scots legal humanizer skill:
  `/home/alba/.openclaw/workspace-atticus-legal/.agents/skills/scots-legal-humanizer/SKILL.md`

Do not modify the general OpenClaw workspace when updating Atticus-specific
agent memory or skills.

## Quick Start

Initialize a new database:

```bash
python -m atticus.cli init --db data/atticus.sqlite3
```

Inspect a database without live work:

```bash
python -m atticus.cli doctor --db data/atticus.sqlite3
python -m atticus.cli status --db data/atticus.sqlite3
python -m atticus.cli schedule --db data/atticus.sqlite3 --capacity 5 --dry-run
```

Build a dry-run work order:

```bash
python -m atticus.cli work-order --db data/atticus.sqlite3 --task-id TASK_ID --dry-run
python -m atticus.cli context --db data/atticus.sqlite3 --task-id TASK_ID --json
```

Discover available commands and tools:

```bash
python -m atticus.cli commands list --json
python -m atticus.cli command show run-free-loop --json
python -m atticus.cli tools list --db data/atticus.sqlite3 --json
```

## Matter Seeding

Seed or repair a matter from a local workspace and inventory. This is dry-run
unless `--write` is supplied:

```bash
python -m atticus.cli seed-matter \
  --db data/napier-accommodation-arrears.sqlite \
  --matter napier-accommodation-arrears \
  --workspace matters/napier-accommodation-arrears \
  --inventory matters/napier-accommodation-arrears/02-registers/file_inventory.csv \
  --provider openai-codex \
  --model gpt-5.5 \
  --no-fallback
```

Write after reviewing the JSON summary:

```bash
python -m atticus.cli seed-matter \
  --db data/napier-accommodation-arrears.sqlite \
  --matter napier-accommodation-arrears \
  --workspace matters/napier-accommodation-arrears \
  --inventory matters/napier-accommodation-arrears/02-registers/file_inventory.csv \
  --provider openai-codex \
  --model gpt-5.5 \
  --no-fallback \
  --write
```

The seeder does not read credentials, call providers, create leases, create
provider runs, or perform external actions.

## Local Extraction And OCR

Source extraction is a local, no-provider harness path. It repairs the durable
coverage tables from matter-local files and writes candidate extracted-text
artifacts under the matter workspace. It is dry-run unless `--write` is
supplied:

```bash
python -m atticus.cli extract-sources \
  --db data/napier-accommodation-arrears.sqlite \
  --matter napier-accommodation-arrears \
  --workspace matters/napier-accommodation-arrears
```

Target specific sources:

```bash
python -m atticus.cli extract-sources \
  --db data/napier-accommodation-arrears.sqlite \
  --matter napier-accommodation-arrears \
  --workspace matters/napier-accommodation-arrears \
  --source-id NAP-SRC-0051 \
  --source-id NAP-SRC-0052 \
  --write
```

The extractor supports local text extraction for DOCX, legacy DOC through
local conversion tools, PDFs through `pdftotext`, text/HTML files, and images
through existing OCR text or local `tesseract`. Missing files or unsupported
formats are reported as skipped/human-attention items instead of crashing. It
does not create leases, candidate worker outputs, provider runs, canonical
legal memory, or external actions.

## Model Routing

Model routing is first-class harness policy. It can be flat per task or richer
through model-policy files with profiles, pools, and route precedence.

Normal provider/model routes:

- Codex GPT-5.5: `provider="openai-codex"`, `model="gpt-5.5"` or alias
  `openai-codex/gpt-5.5`
- OpenRouter DeepSeek Flash:
  `provider="openrouter"`, `model="deepseek/deepseek-v4-flash"`
- OpenRouter DeepSeek Pro:
  `provider="openrouter"`, `model="deepseek/deepseek-v4-pro"`
- Other models: `provider="openrouter"`, `model="<openrouter model id>"`

Direct `provider="deepseek"` is legacy metadata only for this harness until a
direct adapter is explicitly implemented and tested. It is not selected by
normal policy surfaces.

Validate and resolve model policies:

```bash
python -m atticus.cli model-policy validate \
  --policy-file tests/fixtures/model_policies/all_codex_gpt55.json

python -m atticus.cli model-policy resolve \
  --policy-file tests/fixtures/model_policies/layered_openrouter_pool.json \
  --stage S7 \
  --layer hostile_review \
  --task-type citation_audit
```

Set all queued tasks for a matter:

```bash
python -m atticus.cli set-provider-policy \
  --db data/atticus.sqlite3 \
  --matter MATTER \
  --policy-file tests/fixtures/model_policies/layered_openrouter_pool.json \
  --write
```

Flat fallback is blocked. To use fallback, configure a model-routing pool.
There is no silent fallback to OpenRouter free models, DeepSeek, Codex, local
stub, or any other provider/model.

## OpenRouter Failover And Cache Telemetry

OpenRouter failover can be enabled per task through an explicit
`openrouter_failover` policy or via environment. The usable model list must be
explicit when live failover is intended:

```bash
ATTICUS_OPENROUTER_FAILOVER_ENABLED=1 \
ATTICUS_OPENROUTER_FAILOVER_MODELS="qwen/qwen3-coder:free,openai/gpt-oss-120b:free" \
python -m atticus.cli live-resume --db data/atticus.sqlite3 --probe --write-leases
```

The live gate validates every configured model, probes through the same
failover path, and records the final requested model in provider telemetry.

OpenRouter DeepSeek prompt caching is provider-side and automatic when the
selected endpoint supports it. The harness records returned cache usage into
`provider_runs.cache_hit_tokens` and `provider_runs.cache_miss_tokens` when
OpenRouter returns `usage.prompt_tokens_details.cached_tokens`.

OpenRouter response caching is a separate request-level feature. It is not
enabled silently because legal outputs must remain tied to explicit operator
and provider policy.

## Codex GPT-5.5 Runtime

Codex GPT-5.5 is exact and fail-closed. Live Codex execution requires all of:

- exact `openai-codex/gpt-5.5` policy
- fallback disabled
- active lease and matching worker ID
- `--allow-live`
- `ATTICUS_ENABLE_LIVE_CODEX=1`
- bounded timeout
- explicit Codex reasoning effort
- strict JSON candidate packet output
- no canonical writes from the worker
- explicit current operator approval for live spend

Bounded one-tick command after approval:

```bash
ATTICUS_ENABLE_LIVE_CODEX=1 python -m atticus.cli run-free-loop \
  --db data/napier-accommodation-arrears.sqlite \
  --output-dir matters/napier-accommodation-arrears/05-candidates \
  --capacity 1 \
  --max-ticks 1 \
  --runtime codex \
  --allow-live \
  --codex-timeout-seconds 180 \
  --codex-reasoning-effort low
```

Codex diagnostics are written under the task output directory. Treat them as
local sensitive material.

## Coordinator Mode

Coordinator mode creates self-contained legal task graphs from an operator goal.
It is local planning, not a provider call.

Dry-run plan:

```bash
python -m atticus.cli coordinator plan \
  --db data/atticus.sqlite3 \
  --matter MATTER \
  --goal "Draft a cited complaint about accommodation arrears handling"
```

Write queued tasks after review:

```bash
python -m atticus.cli coordinator create-tasks \
  --db data/atticus.sqlite3 \
  --matter MATTER \
  --goal "Draft a cited complaint about accommodation arrears handling" \
  --write
```

Coordinator write mode creates queued tasks only. It creates no leases, provider
runs, candidate outputs, canonical artifacts, or external actions.

Coordinator-created tasks persist task-specific instructions in
`tasks.instructions`; work orders and context packs include those instructions.
Drafting goals include evidence mapping, draft preparation, citation audit,
hostile review, privacy/redaction audit, and final quality gate tasks.

## Worker Packets And Reduction

Workers must return strict `worker_result_packet.v2` candidate packets.
Findings must reference defined citation IDs, and citations must target records
visible in the work-order context or matter-scoped legal graph.

Inspect candidate output:

```bash
python -m atticus.cli inspect --db data/atticus.sqlite3 --type candidate --id CANDIDATE_ID
```

Quarantine a valid but unsuitable candidate:

```bash
python -m atticus.cli reject-candidate \
  --db data/atticus.sqlite3 \
  --candidate-id CANDIDATE_ID \
  --reason "operator reviewed and rejected unsupported conclusions"

python -m atticus.cli reject-candidate \
  --db data/atticus.sqlite3 \
  --candidate-id CANDIDATE_ID \
  --reason "operator reviewed and rejected unsupported conclusions" \
  --write
```

Reduce through the reducer-only canonical path:

```bash
python -m atticus.cli lease \
  --db data/atticus.sqlite3 \
  --task-id TASK_ID \
  --worker-id atticus-reducer-manual \
  --write

python -m atticus.cli reduce \
  --db data/atticus.sqlite3 \
  --candidate-id CANDIDATE_ID \
  --lease-id LEASE_ID \
  --dry-run

python -m atticus.cli reduce \
  --db data/atticus.sqlite3 \
  --candidate-id CANDIDATE_ID \
  --lease-id LEASE_ID \
  --write
```

Reducer acceptance is savepoint-protected around canonical artifact writing,
reducer packet recording, candidate status changes, lease completion, and
proposed-task import.

## Verification And Workflows

Run independent verifier checks against candidates:

```bash
python -m atticus.cli verifier run \
  --db data/atticus.sqlite3 \
  --candidate-id CANDIDATE_ID \
  --type citation_audit \
  --json

python -m atticus.cli verifier run \
  --db data/atticus.sqlite3 \
  --candidate-id CANDIDATE_ID \
  --type hostile_opponent_review \
  --write \
  --json
```

Markdown workflows create task graphs and are dry-run by default:

```bash
python -m atticus.cli workflow list
python -m atticus.cli workflow show complaint-draft
python -m atticus.cli workflow run chronology-build --db data/atticus.sqlite3 --matter MATTER
python -m atticus.cli workflow run hostile-review --db data/atticus.sqlite3 --matter MATTER --write
```

Built-in workflows include chronology, complaint drafting, witness statement
preparation, bundle preparation, authority mapping, SAR/disclosure review,
contradiction detection, hostile review, pleading review, and court
correspondence drafting.

## Legal Memory

Typed legal memory is matter-scoped operational memory, not evidence.
Evidence, law, procedure, contradiction, authority, and risk memories require
source or validated-record references.

Inspect memory:

```bash
python -m atticus.cli memory list --db data/atticus.sqlite3 --matter MATTER
python -m atticus.cli memory show MEMORY_ID --db data/atticus.sqlite3 --matter MATTER
python -m atticus.cli memory export-index --db data/atticus.sqlite3 --matter MATTER
```

Mark memory stale:

```bash
python -m atticus.cli memory mark-stale \
  --db data/atticus.sqlite3 \
  --matter MATTER \
  --memory-id MEMORY_ID \
  --reason "newer evidence received" \
  --write
```

Reducer-gated memory extraction:

```bash
python -m atticus.cli memory extract-candidates \
  --db data/atticus.sqlite3 \
  --matter MATTER \
  --candidate-id REDUCED_ACCEPTED_CANDIDATE_ID

python -m atticus.cli memory extract-candidates \
  --db data/atticus.sqlite3 \
  --matter MATTER \
  --candidate-id REDUCED_ACCEPTED_CANDIDATE_ID \
  --write
```

Extraction only works from a `reduced` candidate with an accepted reducer
packet. Write mode creates `status='candidate'` memories only.

Dry-run case memory consolidation:

```bash
python -m atticus.cli memory consolidate --db data/atticus.sqlite3 --matter MATTER
python -m atticus.cli memory consolidate --db data/atticus.sqlite3 --matter MATTER --write
```

Consolidation reviews active, candidate, stale, duplicate, and contradictory
memories. Write mode creates review tasks; it does not silently activate,
delete, merge, certify, or overwrite memory.

## Sessions

Sessions persist sensitive matter-scoped transcripts without replaying provider
calls:

```bash
python -m atticus.cli session list --db data/atticus.sqlite3 --matter MATTER
python -m atticus.cli session show SESSION_ID --db data/atticus.sqlite3
python -m atticus.cli session resume SESSION_ID --db data/atticus.sqlite3
python -m atticus.cli session export SESSION_ID --db data/atticus.sqlite3
```

## Development And Verification

Run the full local verification set:

```bash
python -m pytest -q
python -m compileall -q atticus tests
git diff --check
git diff --cached --check
```

If available, run basedpyright:

```bash
basedpyright atticus tests --outputjson > /tmp/atticus-basedpyright.json
python - <<'PY'
import json
data = json.load(open('/tmp/atticus-basedpyright.json'))
print(data.get('summary', {}))
raise SystemExit(0 if data.get('summary', {}).get('errorCount', 0) == 0 else 1)
PY
```

Check no live provider or OpenClaw work is running:

```bash
ps -eo pid,ppid,stat,etime,cmd | grep -Ei '[c]odex exec|[a]tticus|[o]penclaw.*atticus' || true
```

Tests do not hit live provider APIs and do not start OpenClaw.
