# Lead Codex Mission: Next-Generation Atticus Legal Harness

You are the lead architect and implementation agent for the next-generation Atticus Legal Harness.

## Non-negotiable safety constraints

- Do not resume OpenClaw.
- Do not start OpenClaw legal workers.
- Do not start any autonomous legal swarm.
- Do not spend model calls on live legal work beyond this engineering/design task.
- Do not delete raw evidence.
- Do not destructively rewrite the archived or current Atticus legal workspace.
- Do not file, email, upload, contact anyone, or perform external legal actions.
- Do not use leaked proprietary source code. You may study public/open-source systems and public documentation only. If you encounter proprietary leaked source, do not inspect it, copy it, summarize it, or use it as implementation guidance.
- Do not integrate AgentHub or build a full dashboard yet. Monitoring UI is deferred. CLI/status foundations are allowed.

## Environment and paths

Current legacy workspace:

- `/home/alba/.openclaw/workspace-atticus-legal`

Archive already created:

- `/home/alba/archives/atticus_legal_20260425T155713Z/workspace-atticus-legal.tar.gz`
- `/home/alba/archives/atticus_legal_20260425T155713Z/manifest.sha256`
- `/home/alba/archives/atticus_legal_20260425T155713Z/run_status.json`
- `/home/alba/archives/atticus_legal_20260425T155713Z/ledger.sqlite`

New standalone project root:

- `/home/alba/atticus-harness`

Use `/home/alba/atticus-harness` as the implementation repo. Treat the old workspace and archive as reference/candidate import material only.

## Strategic goal

Build the foundation of a standalone Atticus Harness legal-firm operating system.

The design and implementation quality should aim to match and eventually surpass general-purpose agent systems such as OpenClaw and Hermes for this specific legal-work domain. OpenClaw should become one execution adapter, not the owner of harness state.

The system must be strong, auditable, evidence-first, cost-aware, restartable, and safe enough for high-stakes legal case work.

## Important architecture principle

The harness has two separate identities:

1. Legal memory system
   - Answers Omar's case questions without rerunning the harness.
   - Retrieves relevant sources, indexes, OCR, artifacts, and prior work.
   - Cites file paths/source IDs.
   - Labels confidence and trust status.
   - Distinguishes certified facts from candidate/pre-redesign material.
   - Offers follow-up work only when needed.
   - Never launches workers without explicit work-order approval.

2. Legal factory
   - Performs actual legal work only when explicitly commanded.
   - Schedules tasks only when dependencies and certifications are satisfied.
   - May intentionally under-fill capacity.
   - Workers write task-local outputs only.
   - Reducer is the only canonical writer.
   - Validation gates must pass before artifacts become trusted.
   - Human attention queue records unresolved blockers.

Read-only questions must not rerun everything.

## Lead agent behavior

You are not a solo coder. You are the lead architect of a team. Use subagents, parallel research tracks, or equivalent internal decomposition. If your environment supports subagents, spawn them. If not, simulate them with separate research files and explicit self-review passes.

Required internal expert tracks:

1. Harness architecture researcher
2. Legal workflow and evidence systems designer
3. Agent scheduler/orchestration engineer
4. Retrieval/query/case-memory engineer
5. Provenance/certification/database architect
6. DeepSeek V4 Pro/Flash provider optimization engineer
7. Migration/archive/import engineer
8. Test/security/code-quality reviewer

For each track, record findings in docs or design notes. Do not let research remain only in hidden reasoning.

## Research requirements

Research public/open-source designs and public documentation only. Study architectural lessons from:

- HKUDS/OpenHarness
- public OpenClaw architecture/patterns/docs if accessible
- public Hermes-style agent orchestration patterns if accessible
- LangGraph
- AutoGen
- CrewAI
- Semantic Kernel
- Temporal-style durable workflow orchestration
- event-sourced task systems
- RAG/evidence retrieval systems
- legal research/e-discovery workflow patterns
- multi-agent debate/council/reducer systems
- cost-aware LLM routing
- provider cache/prompt-prefix strategies
- DeepSeek V4 Pro and DeepSeek V4 Flash behavior, prompting, cache, cost, and provider quirks where publicly documented

Do not copy code from any external project. Extract architectural lessons and implement independently.

## Required standalone project structure

Create or refine a clean Python package under `/home/alba/atticus-harness`.

Target structure:

```text
atticus-harness/
  pyproject.toml
  README.md
  docs/
    research/
    architecture/
    plans/
    prompts/
  atticus/
    __init__.py
    cli.py
    config.py
    core/
      runs.py
      tasks.py
      events.py
      policies.py
    db/
      schema.py
      migrations/
      repo.py
    graph/
      sources.py
      artifacts.py
      dependencies.py
      certifications.py
      staleness.py
    retrieval/
      ask.py
      search.py
      rank.py
      citations.py
      trust.py
    scheduler/
      planner.py
      lease.py
      gates.py
      capacity.py
      retries.py
    context/
      packs.py
      compression.py
      cache_prefix.py
    workers/
      contracts.py
      launcher.py
      result_parser.py
    adapters/
      base.py
      openclaw.py
      codex_cli.py
      claude_code.py
      direct_openrouter.py
      local_stub.py
    validation/
      schemas.py
      evidence.py
      claims.py
      legal_citations.py
      canonical_write_guard.py
    reducer/
      reducer.py
      council.py
      dissent.py
      canonical_writer.py
    providers/
      deepseek.py
      openrouter.py
      policy.py
      cost.py
      cache.py
    migration/
      archive.py
      import_old_run.py
      classify_old_outputs.py
      salvage_indexes.py
    status/
      report.py
      health.py
      inspect.py
  tests/
```

It is acceptable to implement a strong foundation subset now, but the architecture should not paint us into a corner.

## Provider and model strategy

Optimize for DeepSeek V4 Flash and DeepSeek V4 Pro.

- Flash: cheap triage, indexing, extraction QA, classification, duplicate detection, preliminary retrieval summaries, file organization, simple structured extraction.
- Pro: serious legal reasoning, contradiction analysis, hostile review, synthesis, reducer decisions, high-risk answers.
- GPT-5.5/Codex: harness engineering, complex debugging, code review, critical migrations.

Requirements:

- No silent fallback.
- Every worker run records requested provider/model, actual provider/model, tokens, cache metrics where available, estimated/actual cost, and fallback policy result.
- If provider/model differs from request and fallback is not explicitly allowed, fail closed.
- Support stable prompt prefix/context-pack design for provider-side cache where useful.
- Keep provider policy independent from OpenClaw.

## Legal graph stages

Use these canonical stages:

- S0 source inventory
- S1 extraction/OCR/transcription
- S2 evidence registry
- S3 production/filing status
- S4 baseline chronology
- S5 issue/route map
- S6 authority/law map
- S7 hostile review
- S8 draft preparation
- S9 final quality gate

Every task/artifact should support:

- matter scope
- stage
- source dependencies
- artifact dependencies
- required certifications
- output schema
- validation gates
- staleness rules
- provider policy
- cost limits
- expected value
- human attention flags

## Ask/query mode requirements

Implement a read-only query foundation. It may be simple initially, but the policy must be correct.

Ask mode must:

- never launch workers
- never mutate canonical work products
- retrieve relevant candidate/certified sources/artifacts/index entries
- produce answers with citations/file paths
- label trust level and confidence
- distinguish certified vs candidate/pre-redesign content
- say when the answer is not safely supportable
- optionally propose a follow-up task, but not run it

Intent categories to support or document:

- READ_ONLY_QUERY
- STATUS_QUERY
- CONTROL_COMMAND
- WORK_ORDER
- LEGAL_DRAFT_REQUEST
- VALIDATION_REQUEST
- EXTERNAL_ACTION

Default policy:

- Questions retrieve.
- Tasks require explicit work-order mode.
- External actions are blocked.
- Ambiguous spend/rerun requests require explicit approval.

## Active legal factory requirements

Do not run it yet, but implement foundations:

- dependency-aware scheduler
- stage/certification gates
- lease model
- under-fill capacity if not enough safe tasks exist
- no worker canonical writes
- reducer-only canonical writes
- validation before trust/certification
- human attention queue for blocked/unresolved items

## Migration requirements

Use the archived run and current workspace as reference material only. Do not trust old downstream outputs automatically.

Salvage candidate foundation artifacts:

- source indexes
- OCR/text extracts
- manifests
- hashes
- production crosswalks
- duplicate reports
- evidence indexes
- source coverage reports
- chronology fragments when source-linked

Mark old legal analysis/drafts/strategy as:

- rough notes
- candidate
- unverified
- requires validation

Do not issue certifications automatically unless validation proves the foundation layer.

## Required docs deliverables

Produce at least these docs:

- `docs/research/harness-patterns.md`
- `docs/architecture/adr-001-standalone-harness.md`
- `docs/architecture/adr-002-openclaw-as-adapter.md`
- `docs/architecture/adr-003-legal-evidence-graph.md`
- `docs/architecture/adr-004-readonly-query-vs-active-work.md`
- `docs/architecture/adr-005-deepseek-provider-policy.md`
- `docs/plans/atticus-harness-implementation-plan.md`
- `docs/handoff.md`

## Required implementation deliverables

Implement the minimum strong foundation:

- project skeleton
- database schema
- CLI
- read-only status command
- read-only ask skeleton
- migration candidate import skeleton
- provider policy enforcement
- scheduler gates
- validation/canonical-write guard
- tests

CLI should support at least:

```bash
atticus init
atticus status --db <path>
atticus ask "question" --db <path>
atticus import-candidates --workspace <legacy_workspace> --db <path> --dry-run
atticus policy-check --provider openrouter --model deepseek/deepseek-v4-pro
```

Exact names can differ if documented, but commands should be usable.

## Testing requirements

Write tests proving:

1. read-only ask mode never launches workers
2. legacy queued tasks cannot bypass dependency/certification gates
3. provider fallback is blocked unless explicitly allowed
4. old indexes import as candidate artifacts, not certified artifacts
5. certifications require validation
6. non-reducer workers cannot write canonical files
7. scheduler under-fills capacity when only fewer tasks are safe
8. cost/provider metadata can be recorded
9. stale source hash marks dependent artifacts stale
10. status reports blocked reasons and run state correctly

Use a small fixture workspace. Do not run live OpenClaw workers.

## Engineering quality expectations

- Use clear typed Python.
- Prefer standard library and small dependencies unless justified.
- Keep DB schema inspectable.
- Use tests as contracts.
- Keep commands safe by default.
- Make dangerous actions explicit and blocked by default.
- Commit in logical stages if possible.
- Avoid fragile giant scripts. Use modules.
- Do not let implementation become merely a pile of docs.
- Do not overbuild dashboard/UI now.

## Suggested commit stages

1. research and ADRs
2. standalone project skeleton
3. database schema and graph primitives
4. provider policy and DeepSeek strategy
5. query mode skeleton
6. migration candidate import
7. scheduler/validation gates
8. tests and handoff

## Final response requirements

When finished, provide:

- Summary of research findings
- Architecture summary
- What was implemented
- What remains
- Exact test results
- Exact paths changed
- Safety confirmation that no live legal workers were started
- Any risks or follow-up recommendations

Remember: this is groundwork and harness preparation. Do not resume live legal work yet.
