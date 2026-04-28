# ADR 008: Legal Control Plane V2

Atticus remains an evidence-first legal harness: model output is candidate
material until validation and reducer acceptance make it canonical. The v2
control-plane additions strengthen that boundary rather than replacing it with
agent convenience.

## Worker Result Packet V2

Workers must return `worker_result_packet.v2` packets with these top-level
fields:

- `schema_version`
- `task_id`
- `summary`
- `findings`
- `citations`
- `proposed_artifacts`
- `proposed_tasks`
- `uncertainties`
- `contradictions`
- `risk_flags`
- `redaction_flags`
- `external_action_requests`

Findings cite explicit citation IDs. Citations must target records visible in
the work-order context or matter-scoped legal graph. External action requests
are not executed; they are blocked and recorded.

## Context Packs V2

Context packs are built from deterministic sections:

- stable system prefix
- matter posture
- task contract
- evidence manifest
- artifact bundle
- authority map
- legal memory index
- validation gates
- risk flags
- open contradictions
- required output schema
- attached skills
- available tools

Operators can inspect what a worker would see:

```bash
python -m atticus.cli context --db data/atticus.sqlite3 --task-id TASK_ID --json
python -m atticus.cli context --db data/atticus.sqlite3 --task-id TASK_ID --explain
```

## Legal Tools

The tool registry classifies tools by read/write behavior, destructive risk,
live-provider requirements, and matter scoping:

```bash
python -m atticus.cli tools list --db data/atticus.sqlite3 --json
```

Read-only tools inspect sources, artifacts, memory, validation gates, and
context packs. Mutating tools are guarded and matter-scoped. Draft artifact
editing uses read-before-write hashes and creates artifact versions; validated
or certified drafts are not edited by ordinary worker tools.

## Legal Memory

Typed legal memory is a matter-scoped operational projection, not proof.
Evidence, law, procedure, contradiction, authority, and risk memories require
citations to existing source, artifact, authority, claim, chronology, memory, or
validation records. Drafting preferences and user profiles may be uncited only
when they are clearly user-provided.

```bash
python -m atticus.cli memory list --db data/atticus.sqlite3 --matter MATTER
python -m atticus.cli memory show MEMORY_ID --db data/atticus.sqlite3 --matter MATTER
python -m atticus.cli memory mark-stale --db data/atticus.sqlite3 --matter MATTER --memory-id MEMORY_ID --reason "newer evidence" --write
python -m atticus.cli memory export-index --db data/atticus.sqlite3 --matter MATTER
```

## Verifier And Workflows

Independent verifier checks can attack candidate packets before reduction:

```bash
python -m atticus.cli verifier run --db data/atticus.sqlite3 --candidate-id CANDIDATE_ID --type citation_audit --json
python -m atticus.cli verifier run --db data/atticus.sqlite3 --candidate-id CANDIDATE_ID --type hostile_opponent_review --write --json
```

Markdown workflows create task graphs in dry-run mode by default:

```bash
python -m atticus.cli workflow list
python -m atticus.cli workflow show complaint-draft
python -m atticus.cli workflow run chronology-build --db data/atticus.sqlite3 --matter MATTER
python -m atticus.cli workflow run hostile-review --db data/atticus.sqlite3 --matter MATTER --write
```

Built-in workflows cover chronology, complaint drafting, witness statement
preparation, bundle preparation, authority mapping, SAR/disclosure review,
contradiction detection, hostile review, pleading review, and court
correspondence drafting.

## Sessions And Hooks

Sessions persist sensitive matter-scoped transcripts without replaying provider
calls:

```bash
python -m atticus.cli session list --db data/atticus.sqlite3 --matter MATTER
python -m atticus.cli session show SESSION_ID --db data/atticus.sqlite3
python -m atticus.cli session resume SESSION_ID --db data/atticus.sqlite3
python -m atticus.cli session export SESSION_ID --db data/atticus.sqlite3
```

The hook system is internal Python only. It logs lifecycle evaluations and
blocks unsafe external legal actions, cross-matter context, and final drafting
without required hostile-review certification. It warns on stale evidence so
uncertainty remains visible.

## Command Registry

CLI commands now have auditable metadata:

```bash
python -m atticus.cli commands list --json
python -m atticus.cli command show run-free-loop --json
```

The registry marks read-only, write, dry-run, live-provider, workflow, and
prompt command surfaces so operators can review safety behavior before running
them.
