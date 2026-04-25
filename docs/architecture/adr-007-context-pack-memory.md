# ADR-007: Context Pack Memory

Status: Implemented

## Decision

Agent context is a versioned, reproducible harness artifact. Context packs use stable prefixes for provider cache efficiency and deterministic fingerprints for auditability.

## Implementation

- `context_packs` stores task id, pack type, fingerprint, token budget, token estimate, cache metrics, and section JSON.
- `atticus/context/packs.py` builds packs from task contracts, source dependencies, artifact dependencies, required certifications, validation gates, and provider policy.
- Pack fingerprints are SHA-256 hashes of canonical JSON sections.
- Work orders can preview packs without writes or persist them with `--write-context`.

## Consequences

- A worker can be rehydrated from the same context pack.
- Cache-friendly stable instructions stay before volatile task details.
- Context drift is detectable through fingerprint changes.
