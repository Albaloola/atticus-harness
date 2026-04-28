---
name: hostile-review
version: 1
jurisdiction: scotland
forum: any
stage: S7
required_inputs: ["candidate_artifact", "citations"]
creates_tasks: ["hostile_opponent_review", "citation_audit", "procedural_audit"]
required_certifications: ["evidence_registry"]
validation_gates: ["claim_evidence_support", "authority_citation_format", "stale_dependency"]
risk_level: high
description: Attack a candidate legal output for evidential, authority, procedural, and overstatement defects.
---

Verifier tasks must look for defects rather than rubber-stamping candidate work. Attack the output as an opponent, judge, clerk, regulator, or reviewer might.

Check every material claim for citation support, legal authority, jurisdiction, procedural fit, remedy support, stale evidence, privacy/redaction risk, overstatement, and contradiction. A pass must explain what was checked. A fail must identify unsupported claims, weak claims, citation defects, authority defects, procedural defects, and recommended fixes.
