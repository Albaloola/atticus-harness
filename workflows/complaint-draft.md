---
name: complaint-draft
version: 1
jurisdiction: scotland
forum: public_body_or_provider
stage: S8
required_inputs: ["chronology", "evidence_registry", "matter_posture"]
creates_tasks: ["complaint_issue_map", "complaint_draft", "complaint_citation_audit", "hostile_review"]
required_certifications: ["chronology_citations", "hostile_review"]
validation_gates: ["claim_evidence_support", "stale_dependency"]
risk_level: high
description: Draft an evidence-backed complaint with citation and hostile-review tasks.
---

Create a complaint draft as a candidate artifact only. Do not send it. Preserve uncertainty and citation discipline.
