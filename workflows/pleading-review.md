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

Block overstatement, missing authority, unsupported remedies, stale procedural assumptions, and privacy leaks. Treat the pleading as high-risk until every material factual assertion, legal proposition, remedy, jurisdictional assumption, and procedural step can be checked.

Do not certify a draft because it reads well. It must survive citation audit, authority audit, factual support audit, remedy support audit, and privacy/redaction review. Any unsupported allegation, wrong forum term, missing authority, stale source, or uncertain deadline must fail or create a follow-up task.
