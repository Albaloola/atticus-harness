"""Security reviewer heuristics."""

from __future__ import annotations

from forge.audit.packet import ReviewerVerdict
from forge.gates.secrets import scan_diff_for_secrets, scan_forbidden_commands


def security_review(*, diff: str, changed_files: list[str], engine_output: str = "") -> ReviewerVerdict:
    secret = scan_diff_for_secrets(diff)
    command = scan_forbidden_commands(diff + "\n" + engine_output)
    blockers = []
    if not secret.passed:
        blockers.append(f"Potential secret material: {secret.details}")
    if not command.passed:
        blockers.append(f"Forbidden command introduced: {command.details}")
    return ReviewerVerdict(
        role="security_reviewer",
        verdict="reject" if blockers else "approve",
        confidence=0.8,
        risk_level="high" if blockers else "low",
        blocking_issues=blockers,
        files_of_concern=changed_files if blockers else [],
    )
