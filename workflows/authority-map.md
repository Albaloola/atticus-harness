---
name: authority-map
version: 1
jurisdiction: scotland
forum: any
stage: S6
required_inputs: ["issue_route_map"]
creates_tasks: ["authority_identification", "authority_rule_extraction", "authority_audit"]
required_certifications: ["issue_route_map"]
validation_gates: ["authority_citation_format", "stale_dependency"]
risk_level: high
description: Build and audit a legal authority map for the matter.
---

Authority rules must cite authorities and jurisdiction. Do not treat memory as authority.
