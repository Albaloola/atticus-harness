---
name: chronology-build
version: 1
jurisdiction: scotland
forum: any
stage: S4
required_inputs: ["source_inventory", "evidence_registry"]
creates_tasks: ["chronology_extract_events", "chronology_consistency_audit"]
required_certifications: ["evidence_registry"]
validation_gates: ["chronology_citations", "stale_dependency"]
risk_level: medium
description: Build a cited baseline chronology from matter-scoped evidence.
---

Build chronology events only from cited source or artifact material. Flag uncertain dates and contradictions.
