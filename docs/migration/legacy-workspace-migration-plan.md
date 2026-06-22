# Legacy Workspace Migration Plan

Status: Implemented dry-run and candidate import path

## Inputs

- Current OpenClaw legal workspace: `LOCAL_PATH_REDACTED/.openclaw/workspace-atticus-legal`
- Archived prior run: `LOCAL_PATH_REDACTED/archives/atticus_legal_20260425T155713Z`

Both are read-only references for this pass.

## Implemented Flow

1. `migrate-report` scans supported text-ish files and classifies them.
2. `import-candidates --dry-run` lists candidate imports without writes.
3. `import-candidates --write` imports artifacts as `candidate`, `rough_note`, or `unverified_legacy`.
4. Imported artifacts receive validation tasks; they are not certified.
5. Rejected/noise classes are skipped from candidate import.

## Classification Classes

- `source_inventory` / `source_index`
- `extraction_record`
- `evidence_registry`
- `production_crosswalk`
- `chronology_fragment`
- `authority_note`
- `analysis`
- `draft`
- `hostile_review`
- `duplicate_noise`
- `failed_useful`
- `failed_no_output`
- `legacy_note`

## Trust Rules

- Manifests, source indexes, evidence registries, and crosswalks import as `candidate`.
- Analysis, drafts, authorities, hostile reviews, and failed-useful outputs import as `rough_note`.
- Failed-no-output, cache, bytecode, and infrastructure noise are rejected/skipped.
- No imported record is certified automatically.

## Report Fields

Dry-run reports include total files, counts by classification/trust status, examples with path, relative path, size, mtime, extension, optional hash, classification, confidence, matched rule, and human-attention hint.

## Next Migration Work

- Parse CSV manifests into `sources`, `source_snapshots`, and `production_mappings`.
- Parse old harness ledger tables for task status and useful failed outputs.
- Detect duplicate hashes across current workspace and archive.
- Generate human review packets for conflicting path-derived vs metadata-derived classifications.
