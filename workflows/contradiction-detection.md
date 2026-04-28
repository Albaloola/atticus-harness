---
name: contradiction-detection
version: 1
jurisdiction: scotland
forum: any
stage: S5
required_inputs: ["chronology", "evidence_registry"]
creates_tasks: ["contradiction_scan", "contradiction_register", "hostile_review"]
required_certifications: ["chronology_citations"]
validation_gates: ["claim_evidence_support", "stale_dependency"]
risk_level: high
description: Detect and register contradictions across sources, artifacts, claims, and chronology.
---

Do not smooth conflicts away. Record conflicts with citations and proposed follow-up tasks.
