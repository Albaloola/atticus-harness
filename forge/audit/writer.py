"""Audit packet writer."""

from __future__ import annotations

import json
from pathlib import Path

from forge.audit.packet import AuditPacket, EngineResult


def audit_dir(repo: Path, iteration_id: str, date_prefix: str) -> Path:
    return repo / ".forge" / "audit" / date_prefix / iteration_id


def write_audit_packet(
    repo: Path,
    packet: AuditPacket,
    *,
    engine_result: EngineResult | None = None,
    diff: str = "",
    test_output: str = "",
) -> Path:
    date_prefix = (packet.timestamp_start or "unknown")[:10].replace("-", "")
    target = audit_dir(repo, packet.iteration_id, date_prefix)
    target.mkdir(parents=True, exist_ok=True)
    (target / "report.json").write_text(json.dumps(packet.as_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (target / "report.md").write_text(_markdown(packet), encoding="utf-8")
    (target / "diff.patch").write_text(diff, encoding="utf-8")
    (target / "test-output.log").write_text(test_output, encoding="utf-8")
    if engine_result is not None:
        (target / "stdout.log").write_text(engine_result.stdout, encoding="utf-8")
        (target / "stderr.log").write_text(engine_result.stderr, encoding="utf-8")
    else:
        (target / "stdout.log").write_text("", encoding="utf-8")
        (target / "stderr.log").write_text("", encoding="utf-8")
    return target


def latest_audit(repo: Path) -> Path | None:
    audit_root = repo / ".forge" / "audit"
    if not audit_root.exists():
        return None
    reports = sorted(audit_root.glob("*/*/report.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def _markdown(packet: AuditPacket) -> str:
    task = packet.task
    verdicts = packet.reviewer_verdicts
    gates = packet.gate_results
    return "\n".join(
        [
            f"# Forge Audit {packet.iteration_id}",
            "",
            f"- Decision: `{packet.final_decision}`",
            f"- Branch: `{packet.branch_name}`",
            f"- Commit: `{packet.commit_sha or 'none'}`",
            f"- Task: {task.get('title', '')}",
            f"- Risk score: {packet.risk_score}",
            "",
            "## Diff Stats",
            "",
            f"- Files changed: {packet.diff_stats.files_changed}",
            f"- Lines added: {packet.diff_stats.lines_added}",
            f"- Lines deleted: {packet.diff_stats.lines_deleted}",
            "",
            "## Gates",
            "",
            *[f"- {gate.get('name')}: {'pass' if gate.get('passed') else 'fail'}" for gate in gates],
            "",
            "## Reviewers",
            "",
            *[f"- {verdict.get('role')}: {verdict.get('verdict')} ({verdict.get('risk_level')})" for verdict in verdicts],
            "",
        ]
    )
