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

Identify authorities only from matter-scoped sources, validated artifacts, or explicitly supplied authority records. Extract the rule, jurisdiction, forum relevance, citation, locator, and limits of the authority.

Do not treat legal memory, summaries, or model recollection as authority. If an authority is missing, ambiguous, stale, or from the wrong jurisdiction, mark the point as needs_research and create a follow-up task. The authority audit must be able to check each rule against a citation.
