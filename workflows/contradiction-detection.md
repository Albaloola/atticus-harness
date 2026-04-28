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

Do not smooth conflicts away. Record each contradiction as a verification-ready issue with both sides cited, the affected claim or chronology event, the materiality of the conflict, and what evidence could resolve it.

Do not decide credibility unless the work order includes a validated basis for doing so. Mark unresolved conflicts plainly, preserve minority or adverse evidence, and create follow-up tasks for missing records or human review.
