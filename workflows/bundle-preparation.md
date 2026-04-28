---
name: bundle-preparation
version: 1
jurisdiction: scotland
forum: court_or_tribunal
stage: S3
required_inputs: ["source_inventory", "production_mapping"]
creates_tasks: ["bundle_index", "production_crosswalk", "bundle_integrity_audit"]
required_certifications: ["source_inventory"]
validation_gates: ["production_mapping", "hash_validity"]
risk_level: medium
description: Build a candidate bundle index and integrity audit task graph.
---

Prepare bundle materials as candidate artifacts only. Build a verification-ready index that preserves source ids, hashes, production references, document dates, page ranges, and any known redaction or privilege status.

Do not file, serve, lodge, upload, or contact any court, tribunal, party, solicitor, or public body. If a document is missing, stale, duplicated, corrupted, or outside matter scope, flag it and create a follow-up task rather than smoothing the bundle.
