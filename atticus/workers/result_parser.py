"""Parse and validate structured worker result packets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from atticus.workers.contracts import REQUIRED_RESULT_PACKET_KEYS


class ResultPacketError(ValueError):
    """Raised when a worker result packet is not structurally usable."""


@dataclass(frozen=True)
class ParsedResultPacket:
    task_id: str
    summary: str
    findings: list[dict[str, object]]
    citations: list[dict[str, object]]
    proposed_artifacts: list[dict[str, object]]
    proposed_tasks: list[dict[str, object]]
    raw: dict[str, object]


def parse_result(payload: Mapping[str, object]) -> ParsedResultPacket:
    missing = sorted(REQUIRED_RESULT_PACKET_KEYS - set(payload))
    if missing:
        raise ResultPacketError(f"missing worker result keys: {', '.join(missing)}")
    findings = payload.get("findings")
    citations = payload.get("citations")
    proposed_artifacts = payload.get("proposed_artifacts")
    if not isinstance(findings, list) or not isinstance(citations, list) or not isinstance(proposed_artifacts, list):
        raise ResultPacketError("findings, citations, and proposed_artifacts must be lists")
    proposed_tasks = payload.get("proposed_tasks", [])
    if not isinstance(proposed_tasks, list):
        raise ResultPacketError("proposed_tasks must be a list when present")
    return ParsedResultPacket(
        task_id=str(payload["task_id"]),
        summary=str(payload.get("summary") or ""),
        findings=_mapping_list("findings", cast(list[object], findings)),
        citations=_mapping_list("citations", cast(list[object], citations)),
        proposed_artifacts=_mapping_list("proposed_artifacts", cast(list[object], proposed_artifacts)),
        proposed_tasks=_mapping_list("proposed_tasks", cast(list[object], proposed_tasks)),
        raw=dict(payload),
    )


def _mapping_list(field: str, value: list[object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ResultPacketError(f"{field}[{index}] must be a JSON object")
        item_map = cast(Mapping[object, object], item)
        items.append({str(key): value for key, value in item_map.items()})
    return items


def packet_as_dict(packet: ParsedResultPacket) -> dict[str, object]:
    return {
        "task_id": packet.task_id,
        "summary": packet.summary,
        "findings": packet.findings,
        "citations": packet.citations,
        "proposed_artifacts": packet.proposed_artifacts,
        "proposed_tasks": packet.proposed_tasks,
    }
