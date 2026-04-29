"""Minimalist reviewer."""

from __future__ import annotations

from forge.audit.packet import DiffStats, ReviewerVerdict


def minimalist_review(*, stats: DiffStats, changed_files: list[str]) -> ReviewerVerdict:
    blockers = []
    if stats.files_changed > 8:
        blockers.append("Change is too broad for one autonomous task.")
    if stats.lines_added + stats.lines_deleted > 800:
        blockers.append("Diff exceeds small-change budget.")
    return ReviewerVerdict(
        role="minimalist",
        verdict="repair" if blockers else "approve",
        confidence=0.85,
        risk_level="medium" if blockers else "low",
        blocking_issues=blockers,
        recommended_repairs=["Split into a smaller task."] if blockers else [],
        files_of_concern=changed_files if blockers else [],
    )
