---
name: witness-statement-prep
version: 1
jurisdiction: scotland
forum: court_or_tribunal
stage: S8
required_inputs: ["chronology", "source_bundle"]
creates_tasks: ["witness_topics", "witness_statement_draft", "factual_support_audit", "privacy_redaction_audit"]
required_certifications: ["chronology_citations"]
validation_gates: ["claim_evidence_support", "stale_dependency"]
risk_level: high
description: Prepare a witness statement workflow with fact support and redaction audit.
---

Separate witness recollection from documentary facts. Every factual assertion needs support or uncertainty.
