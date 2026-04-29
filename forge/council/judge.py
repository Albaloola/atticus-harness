"""Final council judge."""

from __future__ import annotations

from forge.audit.packet import GateResult, ReviewerVerdict


def judge(gates: list[GateResult], verdicts: list[ReviewerVerdict]) -> ReviewerVerdict:
    blockers: list[str] = []
    failed_gates = [gate.name for gate in gates if not gate.passed]
    if failed_gates:
        blockers.append(f"Failed deterministic gates: {', '.join(failed_gates)}")
    non_approved = [f"{verdict.role}={verdict.verdict}" for verdict in verdicts if verdict.verdict != "approve"]
    if non_approved:
        blockers.append(f"Reviewer did not approve: {', '.join(non_approved)}")
    repairs = [verdict.role for verdict in verdicts if verdict.recommended_repairs]
    if repairs:
        blockers.append(f"Reviewer requested repairs: {', '.join(repairs)}")
    high_risk = [verdict.role for verdict in verdicts if verdict.risk_level == "high" and verdict.blocking_issues]
    if high_risk:
        blockers.append(f"High-risk blocking review: {', '.join(high_risk)}")
    return ReviewerVerdict(
        role="judge",
        verdict="reject" if blockers else "approve",
        confidence=0.9,
        risk_level="high" if blockers else "low",
        blocking_issues=blockers,
        recommended_repairs=blockers,
    )
