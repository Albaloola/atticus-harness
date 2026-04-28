---
name: sar-disclosure-review
version: 1
jurisdiction: uk
forum: data_protection
stage: S3
required_inputs: ["source_inventory", "production_mapping"]
creates_tasks: ["sar_scope_map", "disclosure_gap_audit", "privacy_redaction_audit"]
required_certifications: ["source_inventory"]
validation_gates: ["production_mapping", "stale_dependency"]
risk_level: high
description: Review SAR or disclosure material for gaps, scope, and redaction risks.
---

Treat personal data, third-party data, special category data, privilege, confidentiality, and redaction issues carefully. Map what was requested, what was disclosed, what appears missing, what is withheld, and what legal or procedural basis is stated.

Do not assume a disclosure breach from absence alone. Flag gaps, inconsistent logs, unclear exemptions, stale production records, and privacy risks with citations. Create follow-up tasks for missing correspondence, schedules, metadata, or human legal review.
