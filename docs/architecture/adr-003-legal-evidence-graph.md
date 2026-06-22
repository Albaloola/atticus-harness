# ADR-003: Legal Evidence Graph

Status: Implemented

## Decision

Atticus represents legal work as a hash-first evidence graph rather than a loose artifact folder.

## Implementation

The schema includes:

- `sources` and `source_snapshots`
- `artifacts`, `artifact_versions`, `artifact_sources`, and `artifact_dependencies`
- `extraction_records`, `ocr_records`, and `transcription_records`
- `production_mappings`
- `chronology_events`, `issues`, `claims`, and `legal_authorities`
- `citation_spans`
- `validation_results` and `certifications`

Helpers in `atticus/graph/evidence.py` and `atticus/db/repo.py` create evidence graph records. Staleness propagation marks dependent artifacts stale when source hashes change.

## Consequences

- Every serious assertion can be tied to source, artifact, or authority records.
- Certifications are scoped and revocable through stale dependencies.
- Migration can preserve old work as candidate graph nodes without trusting it.
