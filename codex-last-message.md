Implemented the standalone Atticus Harness foundation in `/home/alba/atticus-harness`.

**Research Findings**
- Public patterns point to Atticus owning legal state, with OpenClaw/Codex/etc. only as execution adapters.
- Durable workflows and event sourcing support auditability, replay, and restartable task state.
- Legal evidence needs hash-first provenance, trust labels, validation-backed certification, and stale dependency handling.
- DeepSeek V4 Flash/Pro policy is fail-closed with recorded requested/actual provider/model, cache metrics, token counts, and costs.

Sources used include [OpenHarness](https://github.com/HKUDS/OpenHarness), [OpenClaw harness docs](https://docs.openclaw.ai/plugins/sdk-agent-harness), [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution), [AWS event sourcing](https://docs.aws.amazon.com/prescriptive-guidance/latest/cloud-design-patterns/event-sourcing.html), [EDRM](https://edrm.net/wiki/edrm-stages-standards/), [DeepSeek pricing](https://api-docs.deepseek.com/quick_start/pricing), [DeepSeek caching](https://api-docs.deepseek.com/guides/kv_cache/), and [OpenRouter routing](https://openrouter.ai/docs/guides/routing/provider-selection).

**Architecture**
- Legal memory and legal factory are separated.
- `ask` and `status` are read-only.
- Scheduler gates enforce dependencies, certifications, and stale checks.
- Workers are blocked by default and may only produce task-local outputs later.
- Reducer-only canonical writes are enforced.
- Legacy imports become candidate/rough-note artifacts, not certified facts.

**Implemented**
- Python package skeleton under `atticus/`.
- SQLite schema/repository layer.
- CLI: `init`, `status`, `ask`, `import-candidates`, `policy-check`.
- Read-only ask skeleton with citations/trust/confidence labels.
- Candidate migration importer with dry-run.
- DeepSeek/OpenRouter provider policy and cost model.
- Scheduler gates, lease stub, capacity helper.
- Validation-backed certification helper.
- Canonical write guard.
- Required docs and ADRs.
- Contract tests.

**Test Results**
- `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q`
- Result: `11 passed in 0.05s`

CLI smoke checks also passed for `init`, `status`, `ask`, `import-candidates --dry-run`, and `policy-check`.

**Changed Paths**
- `.gitignore`
- `README.md`
- `pyproject.toml`
- `atticus/` new package, including `cli.py`, `db/`, `core/`, `graph/`, `retrieval/`, `scheduler/`, `providers/`, `migration/`, `validation/`, `reducer/`, `workers/`, `adapters/`, `context/`, `status/`
- `docs/research/harness-patterns.md`
- `docs/architecture/adr-001-standalone-harness.md`
- `docs/architecture/adr-002-openclaw-as-adapter.md`
- `docs/architecture/adr-003-legal-evidence-graph.md`
- `docs/architecture/adr-004-readonly-query-vs-active-work.md`
- `docs/architecture/adr-005-deepseek-provider-policy.md`
- `docs/plans/atticus-harness-implementation-plan.md`
- `docs/handoff.md`
- `tests/test_foundation_contracts.py`

**Safety Confirmation**
No OpenClaw resume, no OpenClaw legal workers, no autonomous legal swarm, no live legal work, no filings/emails/uploads/external actions, and no destructive changes to the legacy workspace or archive.

**Remaining**
The current version is a strong foundation, not the full factory. Next work should add immutable source snapshots/artifact versions, FTS/citation spans, real provider request builders with fallback disabled, richer importers, and explicit work-order execution gates before any worker path is enabled.