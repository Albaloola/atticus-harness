# ADR-002: OpenClaw As Adapter

Status: Implemented

## Decision

OpenClaw is a possible execution adapter only. It is not resumed, launched, or treated as canonical during Atticus initialization, queries, migration, validation, scheduling previews, or reports.

## Implementation

- `atticus/adapters/openclaw.py` defines `OpenClawAdapter.launch()` as blocked.
- `atticus/workers/launcher.py` blocks live legal worker launches in this harness pass.
- Migration reads legacy OpenClaw workspace files as candidate/rough-note material.
- Tests assert OpenClaw launch raises `AdapterBlocked`.

## Consequences

- The old swarm cannot accidentally restart through the harness.
- Legacy outputs can be salvaged without inheriting their trust.
- Live execution can be added later behind explicit work-order, lease, provider, budget, and safety gates.
