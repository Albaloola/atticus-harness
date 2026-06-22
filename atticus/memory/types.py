"""Legal memory taxonomy for Atticus."""

from __future__ import annotations

LEGAL_MEMORY_TYPES: dict[str, str] = {
    "user_profile": "Private user role, preferences, knowledge, and drafting expectations.",
    "matter_posture": "Procedural state, forum, parties, live deadlines, and current orders.",
    "strategy": "Case theory, remedy sought, litigation risk, and tactical choices.",
    "evidence_fact": "Durable fact learned from cited source material.",
    "contradiction": "Known conflict between sources, statements, or legal positions.",
    "authority_rule": "Verified legal authority or rule, with jurisdiction and citation.",
    "drafting_preference": "Style and terminology preferences for the user or forum.",
    "disclosure_obligation": "SAR, FOI, GDPR, recovery, productions, privilege, and redaction duties.",
    "external_reference": "Where to find external information without treating it as proof.",
    "procedural_deadline": "Procedural deadline or hearing date requiring verification.",
    "risk_register": "Material legal, evidential, procedural, privacy, or tactical risk.",
}

SOURCE_REQUIRED_MEMORY_TYPES = frozenset(
    {
        "matter_posture",
        "strategy",
        "evidence_fact",
        "contradiction",
        "authority_rule",
        "disclosure_obligation",
        "external_reference",
        "procedural_deadline",
        "risk_register",
    }
)
