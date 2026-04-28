---
name: pleading-review
version: 1
jurisdiction: scotland
forum: court
stage: S9
required_inputs: ["draft", "authority_map", "chronology"]
creates_tasks: ["pleading_citation_audit", "authority_audit", "remedy_support_audit", "privacy_redaction_audit"]
required_certifications: ["hostile_review", "authority_map"]
validation_gates: ["claim_evidence_support", "authority_citation_format", "stale_dependency"]
risk_level: critical
description: Review a pleading candidate before final quality gate.
---

Block overstatement, missing authority, unsupported remedies, stale procedural assumptions, and privacy leaks.
