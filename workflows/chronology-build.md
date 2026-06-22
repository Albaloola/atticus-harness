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

Build chronology events only from cited matter-scoped source or artifact material. Each event should state the date, date precision, actor, event, source citation, and confidence.

Do not infer dates, sequence, motives, admissions, or causation unless the finding is clearly labelled as inference and tied to cited material. Flag uncertain dates, disputed accounts, missing documents, stale sources, and contradictions. Create follow-up tasks for gaps instead of filling them with narrative.
