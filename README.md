<div align="center">

# Atticus Harness

### Evidence-first legal AI control plane for matter-scoped work, safe model routing, resumable case operations, and reducer-gated legal outputs.

[![Python](https://img.shields.io/badge/Python-3.11%2B-111827?style=for-the-badge&labelColor=0b1020)](#development-and-verification)
[![SQLite](https://img.shields.io/badge/SQLite-durable%20ledger-1f6feb?style=for-the-badge&labelColor=0b1020)](#durable-data-model)
[![Model Policy](https://img.shields.io/badge/Model%20Routing-fail%20closed-b91c1c?style=for-the-badge&labelColor=0b1020)](#smart-model-routing)
[![Legal Safety](https://img.shields.io/badge/Legal%20Safety-reducer%20gated-047857?style=for-the-badge&labelColor=0b1020)](#safety-doctrine)
[![Cache](https://img.shields.io/badge/Cache-audited%20not%20proof-7c3aed?style=for-the-badge&labelColor=0b1020)](#cache-and-context-observability)

<sub>
Atticus is not a solicitor, does not perform external legal actions, and treats model output as candidate material until validation plus reducer acceptance.
</sub>

</div>

---

## Quick Install

```bash
git clone https://github.com/example-user/atticus-harness.git && cd atticus-harness
python -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[dev]"
python -m pytest -q && python -m compileall -q atticus tests && git diff --check
```

Optionally install local extraction tools: `sqlite3 poppler-utils tesseract-ocr libreoffice pandoc`.

Create a new database:

```bash
python -m atticus.cli init --db data/atticus.sqlite3
python -m atticus.cli doctor --db data/atticus.sqlite3 --schema --json
```

Live provider work requires `OPENROUTER_API_KEY` + `ATTICUS_ENABLE_LIVE_OPENROUTER=1` (or `ATTICUS_ENABLE_LIVE_CODEX=1` for Codex). Anthropic is reserved and disabled by default.

---

## Prime Directive (AI Agent)

1. Read this README and ADRs under `docs/architecture/` before changing the system.
2. Treat `LOCAL_PATH_REDACTED/atticus-harness` as the source tree and SQLite DBs under `data/` as sensitive matter ledgers.
3. Never commit or upload API keys, OAuth tokens, `.env` files, provider transcripts, or raw private case evidence.
4. Prefer dry-run commands first. Use `--write` only when mutation is safe and intended.
5. Never weaken reducer-only canonical writes, matter isolation, citation validation, human gates, no-silent-fallback policy, or loop guards.
6. If a loop blocks, inspect `error_logs`, `human_attention`, `orchestrator_events`, `leases`, task blocked reasons, and provider runs. Do not just rerun.
7. If a model output is malformed, repair the prompt or task contract rather than training the harness to accept sloppy packets.

### Agent Bootstrap

```bash
cd LOCAL_PATH_REDACTED/atticus-harness
python -m atticus.cli --help
python -m atticus.cli doctor --db data/<DB>.sqlite --schema --json
python -m atticus.cli status --db data/<DB>.sqlite --matter <MATTER>
python -m atticus.cli schedule --db data/<DB>.sqlite --matter <MATTER> --capacity 15 --dry-run
```

One bounded live tick:

```bash
ATTICUS_ENABLE_LIVE_OPENROUTER=1 python -m atticus.cli run-free-loop \
  --db data/<DB>.sqlite --matter <MATTER> \
  --output-dir matters/<MATTER>/05-candidates \
  --capacity 15 --max-ticks 1 --runtime openrouter --allow-live
```

---

## Architecture Summary

Atticus is a standalone legal operations harness. The database and event stream sit at the center; provider runtimes and agent adapters sit at the outside. The harness decides what context is visible, what model is allowed, whether a task can run, and whether any output may become canonical.

| Area | Modules | Purpose |
| --- | --- | --- |
| CLI and commands | `atticus/cli.py`, `atticus/commands/` | Operator entry points, read/write/live/dry-run visibility |
| Durable store | `atticus/db/`, `atticus/core/` | SQLite schema, matters, runs, tasks, policies, event stream, matter profiles |
| Evidence graph | `atticus/graph/` | Sources, snapshots, artifacts, dependencies, issues, claims, authorities, staleness |
| Extraction | `atticus/extraction/local.py` | Local text/OCR coverage without live provider calls |
| Context | `atticus/context/` | Deterministic context pack sections, diagnostics, compression, cache-safe prefixes |
| Scheduling | `atticus/scheduler/` | Dependency-aware task selection, leases, gates, capacity, supervisor loop |
| Providers | `atticus/providers/`, `atticus/adapters/` | Policy validation, smart decisioning, OpenRouter, Codex, Anthropic reserved |
| Agents | `atticus/agents/` | Coordinator, orchestrator, subagents, cache-safe context sharing |
| Reducer | `atticus/reducer/` | Packet review, dissent/council support, canonical writer |
| Retrieval and memory | `atticus/retrieval/`, `atticus/memory/` | Read-only ask/search, work reuse, typed legal memory |
| Work persistence | `atticus/work_runs.py` | Resume tokens, work step ledger, reuse records, stale invalidation |
| Monitor | `atticus/monitor/` | Interactive curses TUI, monitor state, action dispatch, JSON dump |
| Tool system | `atticus/tools/base.py` | `build_tool()`, permission modes, deferral, defaults |
| Cost tracker | `atticus/cost_tracker.py` | Per-model usage, session accumulation, cost persistence |
| Memdir | `atticus/memdir/` | File-based persistent memory with types, relevance, age |
| Migration runner | `atticus/migration_runner.py` | Version-tracked sync migrations, savepoint-protected |
| Permissions | `atticus/core/permissions.py` | Mode rules (allow/deny/ask), bypass, permission context |
| Progress | `atticus/providers/progress.py` | Per-task progress events, event tracker singleton |
| Task lifecycle | `atticus/task_lifecycle.py` | Type-prefixed task IDs, terminal status guards |
| Vim bindings | `atticus/tui/vim_bindings.py` | NORMAL/INSERT/VISUAL state machine for TUI |

The lifecycle: seed matter sources, extract local text/OCR, plan legal work, build deterministic context packs, route through smart model policy, execute bounded worker ticks, validate candidates (citation target + support integrity), reduce accepted packets through canonical writer, record durable work runs for resume/reuse.

---

## Safety Doctrine

Non-negotiable invariants:

- Evidence comes before argument. Context must be matter-scoped and inspectable.
- Model output is candidate material until validation and reducer acceptance. Workers create candidate packets only. Reducers are the only canonical writers.
- Provider/model routing is explicit, deterministic, and fail-closed.
- Memory is operational orientation, not evidence. Cache hits save cost, not legal verification.
- External legal actions are blocked unless a future safe design and explicit operator approval authorize the action.

---

## Smart Model Routing

Atticus model selection is deterministic and auditable. The decision layer uses task metadata, risk level, stage, contradictions, uncertainty, authority needs, drafting finality, evidence volume, requested capabilities, and operator override fields. Routine S0-S4 tasks route to DeepSeek Flash; high-risk S5-S9 work routes to DeepSeek Pro; code/schema work routes to Codex GPT-5.5 exact. Anthropic is reserved. Free/hold models are blocked by default.

| Tier | Provider / Model | Used For | Fallback |
| --- | --- | --- | --- |
| `flash_worker` | OpenRouter `deepseek/deepseek-v4-flash` | Source inventory, extraction QA, classification, dedupe, retrieval, chronology, candidate formatting | Disabled unless explicit pool policy |
| `pro_orchestrator` | OpenRouter `deepseek/deepseek-v4-pro` | Orchestration, authority mapping, contradiction analysis, hostile review, high-risk synthesis, final gates, reducer support | Disabled unless explicit pool policy |
| `codex_exact` | `openai-codex` `gpt-5.5` | Code, schema migrations, tests, harness self-improvement | Never |
| `anthropic_reserved` | Anthropic Opus/Sonnet aliases | Future reserved option only | Never |
| `blocked` | None | Missing data, disabled profile, held/free model, unsafe route, unknown provider | Never |

---

## Essential Commands

| Command | Mode | Writes? | Purpose |
| --- | --- | --- | --- |
| `init` | setup | yes | Create or initialize a SQLite ledger |
| `doctor` | diagnostic | optional | Check/repair schema drift and safety state |
| `status` | diagnostic | no | Summarize run, blockers, leases, failures, attention |
| `seed-matter` | setup | with `--write` | Register sources and snapshots from an inventory |
| `extract-sources` | setup/repair | with `--write` | Create local extraction/OCR derivatives |
| `schedule` | planning | with `--write` | Preview or persist scheduling blocked reasons |
| `coordinator` | planning | with `--write` | Plan or create task graphs from a case goal |
| `run-free-loop` | execution | yes | Run bounded supervisor ticks and workers |
| `model-policy` | audit | no | Validate, resolve, or decide model routing |
| `validate` | validation | yes | Record a validation result for a gate |
| `verifier` | validation | optional | Run independent verifier checks |
| `reduce` | canonical boundary | with `--write` | Reduce candidate through the canonical writer |
| `orchestrator` | recovery | optional | Inspect/tick/failures/signals for matter orchestrators |
| `maintenance` | recovery | optional | Isolated maintenance diagnostics and reports |
| `work-run` | persistence | optional | Start/resume/export/reuse durable work runs |
| `monitor` | operator loop | no | Interactive curses TUI for real-time harness visibility |
| `control-panel` | operator loop | no | Structured handoff packet for human operator review |
| `runbook` | handoff | no | Export operator runbook with next action and blocker details |

---

## Development and Verification

```bash
python -m pytest -q
python -m compileall -q atticus tests
git diff --check && git diff --cached --check
```

Optional static check:
```bash
basedpyright atticus tests --outputjson > /tmp/atticus-basedpyright.json
python -c "import json; d=json.load(open('/tmp/atticus-basedpyright.json')); exit(d['summary']['errorCount'])"
```

Tests do not hit live provider APIs. Architecture decision records live in `docs/architecture/` (ADR 001-008), alongside the [Atticus Harness V1 Architecture Research Paper](docs/architecture/architecture-v1-research-paper.md).

---

## Agent Handoff Prompt

```text
You are operating Atticus Harness in LOCAL_PATH_REDACTED/atticus-harness.
Read README.md and docs/architecture before editing. Preserve the evidence-first
doctrine: matter isolation, reducer-only canonical writes, citation target and
quote support validation, no silent model fallback, no autonomous external legal
actions, and no memory/cache-as-proof.

Start with:
python -m atticus.cli doctor --db <DB> --schema --json
python -m atticus.cli status --db <DB> --matter <MATTER>
python -m atticus.cli schedule --db <DB> --matter <MATTER> --capacity 15 --dry-run

If blocked, inspect error_logs, human_attention, orchestrator_events, leases,
provider_runs, validation_results, and task blocked_reasons_json. Fix root
causes in code/tests when the harness is brittle; do not hide failures by
loosening gates.
```
