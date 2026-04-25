"""Parse and validate structured worker result packets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from atticus.workers.contracts import REQUIRED_RESULT_PACKET_KEYS


class ResultPacketError(ValueError):
    """Raised when a worker result packet is not structurally usable."""


@dataclass(frozen=True)
class ParsedResultPacket:
    task_id: str
    summary: str
    findings: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    proposed_artifacts: list[dict[str, Any]]
    proposed_tasks: list[dict[str, Any]]
    raw: dict[str, Any]


def parse_result(payload: dict[str, Any]) -> ParsedResultPacket:
    missing = sorted(REQUIRED_RESULT_PACKET_KEYS - set(payload))
    if missing:
        raise ResultPacketError(f"missing worker result keys: {', '.join(missing)}")
    findings = payload.get("findings")
    citations = payload.get("citations")
    proposed_artifacts = payload.get("proposed_artifacts")
    if not isinstance(findings, list) or not isinstance(citations, list) or not isinstance(proposed_artifacts, list):
        raise ResultPacketError("findings, citations, and proposed_artifacts must be lists")
    return ParsedResultPacket(
        task_id=str(payload["task_id"]),
        summary=str(payload.get("summary") or ""),
        findings=[dict(item) for item in findings],
        citations=[dict(item) for item in citations],
        proposed_artifacts=[dict(item) for item in proposed_artifacts],
        proposed_tasks=[dict(item) for item in payload.get("proposed_tasks", [])],
        raw=dict(payload),
    )


def packet_as_dict(packet: ParsedResultPacket) -> dict[str, Any]:
    return {
        "task_id": packet.task_id,
        "summary": packet.summary,
        "findings": packet.findings,
        "citations": packet.citations,
        "proposed_artifacts": packet.proposed_artifacts,
        "proposed_tasks": packet.proposed_tasks,
    }
