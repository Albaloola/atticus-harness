"""Atticus domain reviewer heuristics."""

from __future__ import annotations

from forge.audit.packet import ReviewerVerdict


DOMAIN_RISK_TERMS = ["citation", "evidence", "audit", "canonical", "reducer", "trusted memory", "external legal"]


def domain_review(*, diff: str, changed_files: list[str]) -> ReviewerVerdict:
    lowered = diff.lower()
    blockers = []
    if "allow_fallback" in lowered and "legal" in lowered and "logged" not in lowered:
        blockers.append("Legal-critical fallback behavior appears to change without explicit logging.")
    if "trusted" in lowered and "candidate" in lowered and "validation" not in lowered:
        blockers.append("Candidate/trusted memory boundary may be weakened.")
    risk = "medium" if any(term in lowered for term in DOMAIN_RISK_TERMS) else "low"
    return ReviewerVerdict(
        role="domain_reviewer",
        verdict="reject" if blockers else "approve",
        confidence=0.55,
        risk_level="high" if blockers else risk,
        blocking_issues=blockers,
        files_of_concern=changed_files if blockers else [],
    )
