# ADR-001: Standalone Harness

Status: Implemented

## Decision

Atticus is the durable source of truth for legal memory, work state, evidence provenance, provider usage, validations, certifications, budgets, and human-attention records.

Agent runtimes are adapters. They may execute bounded work orders, but they do not own canonical state.

## Implementation

- SQLite schema version 2 in `atticus/db/schema.py`.
- Append-only `events` with update/delete triggers and hash chaining.
- Projection tables for runs, matters, tasks, leases, sources, artifacts, evidence graph, tracked files, rebuildable search indexes, provider usage, budgets, context packs, worker candidates, reducer packets, and human attention.
- Repository helpers in `atticus/db/repo.py` emit durable events for critical transitions.
- CLI commands expose read-only paths and active factory paths separately.

## Consequences

- The harness can be audited without reading worker transcripts.
- Status and validation can be rebuilt from durable state.
- Future OpenClaw/Codex/Claude/Direct API adapters can be swapped without moving legal truth out of Atticus.
