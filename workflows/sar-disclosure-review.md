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

Treat personal data and third-party data carefully. Flag privilege, redaction, and disclosure risks.
