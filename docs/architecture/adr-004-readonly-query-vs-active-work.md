# ADR-004: Read-Only Query Vs Active Work

Status: Implemented

## Decision

Read-only memory/query commands are separate from the active legal factory.

## Implementation

Read-only path:

- `atticus ask`
- `atticus status`
- `atticus inspect`

Active factory path:

- `validate`
- `certify`
- `schedule`
- `lease`
- `work-order`
- `reduce`
- `budget`
- `provider-policy` with optional DB recording
- `human-attention`
- `import-candidates --write`
- `rebuild-search-index --write`

`ask` uses a read-only SQLite connection. It blocks external action, worker-launch, validation, certification, and drafting intents.
Matter-scoped read/query commands must authorize `--matter` against the execution-context matter (`ATTICUS_AUTHORIZED_MATTER`, default `atticus`) before reading snippets or citations.
`rebuild-search-index` is an operator maintenance command for disposable retrieval projections; it defaults to dry-run and requires `--write` before it updates projection or audit rows.

## Consequences

- Asking a question cannot start workers.
- Querying current memory cannot mutate canonical state.
- Factory commands are explicit and dry-run oriented.
