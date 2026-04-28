---
name: court-correspondence-draft
version: 1
jurisdiction: scotland
forum: court
stage: S8
required_inputs: ["matter_posture", "draft_instructions"]
creates_tasks: ["court_letter_draft", "procedural_audit", "privacy_redaction_audit"]
required_certifications: ["matter_posture"]
validation_gates: ["stale_dependency"]
risk_level: high
description: Draft court correspondence as a candidate artifact with procedural and privacy audit.
---

Draft only. Produce a candidate court correspondence artifact with clear addressee, procedural context, requested order or action, supporting citations, deadline assumptions, and privacy/redaction warnings.

Do not send, file, serve, lodge, upload, or contact the court. Do not claim that service or lodging has happened unless the matter record proves it as a past fact. If procedural authority, time limits, party roles, or forum terminology are uncertain, flag them for procedural audit.
