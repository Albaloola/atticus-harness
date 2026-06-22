# Harness Patterns Research

Date: 2026-04-25

This research pass informed the implemented Atticus redesign. The result is not a generic multi-agent chat system; it is a durable legal workflow harness with agent adapters at the edge.

## Sources Reviewed

- OpenHarness: tools, skills, memory, permissions, hooks, plugins, background tasks, and multi-agent coordination. <https://github.com/HKUDS/OpenHarness>
- LangGraph durable execution and persistence: checkpointer-backed resumability, human-in-the-loop, deterministic replay, and side-effect isolation. <https://docs.langchain.com/oss/python/langgraph/durable-execution>
- Temporal workflows: event-history-backed durable execution, workflow/activity split, retries, timeouts, and replay constraints. <https://docs.temporal.io/workflow-execution>
- CrewAI flows and crews: stateful flows for control, crews for bounded collaboration. <https://docs.crewai.com/en/concepts/flows>
- Microsoft Agent Framework: workflows for predictable control, agents for dynamic reasoning, middleware/telemetry/session state. <https://learn.microsoft.com/en-us/agent-framework/overview/>
- OpenAI Agents SDK: manager patterns, handoffs, guardrails, and tracing. <https://openai.github.io/openai-agents-python/handoffs/>
- Anthropic Claude Code public docs: subagents with separate context windows and hooks around tool/agent lifecycle. <https://docs.anthropic.com/en/docs/claude-code/sub-agents>
- EDRM/e-discovery chain-of-custody and processing concepts: hash-first provenance, custody notes, processing lineage, and review stages. <https://edrm.net/edrm-model/>

## Patterns Adopted

- Atticus owns state. Execution runtimes are adapters only.
- Critical transitions append events; projection tables are optimized for status/search.
- Scheduler decides what may run; workers solve only bounded work orders.
- Workers produce candidate packets; reducers write canonical memory after validation.
- Legal blockers are not retryable failures. They create blocked tasks or human-attention records.
- Context packs have stable prefixes for cache efficiency and deterministic fingerprints for reproducibility.
- Provider calls record requested and actual provider/model, cache tokens, latency/retry fields, and cost estimates.
- Legal evidence uses source snapshots, artifact versions, dependency edges, citation spans, validation records, and certification records.
- Councils are reducer-governed flows: evidence, chronology, authority, hostile review, drafting, and final QA councils preserve votes/dissent but do not self-certify.

## Patterns Rejected

- Open-ended autonomous group chat as the legal scheduler.
- Worker self-certification.
- Prompt-only safety.
- Silent provider fallback.
- Treating old OpenClaw outputs as canonical merely because they exist.
- Launching live workers while importing or querying legacy memory.

## Implemented Consequences

- `events` is append-only with hash chaining.
- `tasks`, `leases`, `candidate_outputs`, `reducer_packets`, `validation_results`, `certifications`, `provider_runs`, `budgets`, and `human_attention` are first-class tables.
- `atticus ask` opens the database read-only and cannot call the worker launcher.
- `schedule`, `lease`, `work-order`, and `reduce` default to dry-run or require explicit write flags.
- `OpenClawAdapter.launch()` raises `AdapterBlocked`.
