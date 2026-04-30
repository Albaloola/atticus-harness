"""Parse and validate structured worker result packets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast


class ResultPacketError(ValueError):
    """Raised when a worker result packet is not structurally usable."""


RESULT_PACKET_SCHEMA_VERSION = "worker_result_packet.v2"
FINDING_TYPES = frozenset({"fact", "law", "procedure", "inference", "drafting_note", "contradiction", "risk"})
CITATION_TARGET_TYPES = frozenset(
    {"source", "artifact", "authority", "chronology_event", "claim", "memory", "validation_result"}
)
EVIDENCE_CITATION_TARGET_TYPES = frozenset({"source", "artifact", "authority", "chronology_event", "claim"})
REASONING_STATUSES = frozenset({"supported", "inferred", "uncertain", "needs_research", "contradicted"})
RESULT_PACKET_REQUIRED_KEYS = frozenset(
    {
        "schema_version",
        "task_id",
        "summary",
        "findings",
        "citations",
        "proposed_artifacts",
        "proposed_tasks",
        "uncertainties",
        "contradictions",
        "risk_flags",
        "redaction_flags",
        "external_action_requests",
    }
)
RESULT_PACKET_ALLOWED_KEYS = RESULT_PACKET_REQUIRED_KEYS
FINDING_REQUIRED_KEYS = frozenset({"finding_id", "text", "finding_type", "citation_ids", "confidence", "reasoning_status"})
FINDING_ALLOWED_KEYS = FINDING_REQUIRED_KEYS
CITATION_REQUIRED_KEYS = frozenset({"citation_id", "target_type", "target_id", "locator"})
CITATION_ALLOWED_KEYS = CITATION_REQUIRED_KEYS | frozenset({"quoted_text_hash", "quote", "excerpt"})
PROPOSED_ARTIFACT_REQUIRED_KEYS = frozenset({"path", "artifact_type", "stage", "title", "content"})
PROPOSED_ARTIFACT_ALLOWED_KEYS = PROPOSED_ARTIFACT_REQUIRED_KEYS
FULL_TEXT_ARTIFACT_TYPES = frozenset({"complaint_draft", "draft", "draft_complaint", "redacted_draft"})
INCOMPLETE_DRAFT_MARKERS = (
    "[remaining",
    "[conclusion unchanged",
    "[remaining complaint content",
    "remaining legal arguments unchanged",
    "remaining complaint content",
    "conclusion unchanged",
    "content omitted",
    "not reproduced",
)
PROPOSED_TASK_REQUIRED_KEYS = frozenset({"task_id", "title", "task_type", "stage", "matter_scope", "instructions"})
PROPOSED_TASK_ALLOWED_KEYS = PROPOSED_TASK_REQUIRED_KEYS | frozenset(
    {
        "source_dependencies",
        "artifact_dependencies",
        "task_dependencies",
        "matter_dependencies",
        "validation_gates",
        "required_certifications",
        "provider_policy",
        "expected_value",
        "cost_limit_usd",
    }
)
SHA256_HEX_LEN = 64


@dataclass(frozen=True)
class ParsedResultPacket:
    schema_version: str
    task_id: str
    summary: str
    findings: list[dict[str, object]]
    citations: list[dict[str, object]]
    proposed_artifacts: list[dict[str, object]]
    proposed_tasks: list[dict[str, object]]
    uncertainties: list[dict[str, object]]
    contradictions: list[dict[str, object]]
    risk_flags: list[dict[str, object]]
    redaction_flags: list[dict[str, object]]
    external_action_requests: list[dict[str, object]]
    raw: dict[str, object]


def parse_result(
    payload: Mapping[str, object],
    *,
    strict: bool = True,
    allowed_citation_targets: Mapping[str, set[str]] | None = None,
    proof_citation_targets: Mapping[str, set[str]] | None = None,
) -> ParsedResultPacket:
    missing = sorted(RESULT_PACKET_REQUIRED_KEYS - set(payload))
    if missing:
        raise ResultPacketError(f"missing worker result keys: {', '.join(missing)}")
    unexpected = sorted(set(payload) - RESULT_PACKET_ALLOWED_KEYS)
    if strict and unexpected:
        raise ResultPacketError(f"unexpected worker result keys: {', '.join(unexpected)}")
    schema_version = payload.get("schema_version")
    if schema_version != RESULT_PACKET_SCHEMA_VERSION:
        raise ResultPacketError(f"schema_version must be {RESULT_PACKET_SCHEMA_VERSION}")
    task_id = _required_string(payload, "task_id")
    summary = _required_string(payload, "summary")
    if not summary.strip():
        raise ResultPacketError("summary must not be empty")

    citations = _validate_citations(_list_value(payload, "citations"), allowed_citation_targets=allowed_citation_targets)
    citations_by_id = {str(item["citation_id"]): item for item in citations}
    findings = _validate_findings(
        _list_value(payload, "findings"),
        citations_by_id=citations_by_id,
        proof_citation_targets=proof_citation_targets,
    )
    proposed_artifacts = _validate_proposed_artifacts(_list_value(payload, "proposed_artifacts"))
    proposed_tasks = _validate_proposed_tasks(_list_value(payload, "proposed_tasks"))
    uncertainties = _validate_auxiliary_citation_references(
        "uncertainties",
        _list_value(payload, "uncertainties"),
        citations_by_id=citations_by_id,
    )
    contradictions = _validate_auxiliary_citation_references(
        "contradictions",
        _list_value(payload, "contradictions"),
        citations_by_id=citations_by_id,
    )
    risk_flags = _validate_auxiliary_citation_references(
        "risk_flags",
        _list_value(payload, "risk_flags"),
        citations_by_id=citations_by_id,
    )
    redaction_flags = _validate_auxiliary_citation_references(
        "redaction_flags",
        _list_value(payload, "redaction_flags"),
        citations_by_id=citations_by_id,
    )
    external_action_requests = _mapping_list("external_action_requests", _list_value(payload, "external_action_requests"))
    if external_action_requests:
        raise ResultPacketError("external action requests are blocked")
    return ParsedResultPacket(
        schema_version=RESULT_PACKET_SCHEMA_VERSION,
        task_id=task_id,
        summary=summary,
        findings=findings,
        citations=citations,
        proposed_artifacts=proposed_artifacts,
        proposed_tasks=proposed_tasks,
        uncertainties=uncertainties,
        contradictions=contradictions,
        risk_flags=risk_flags,
        redaction_flags=redaction_flags,
        external_action_requests=external_action_requests,
        raw=dict(payload),
    )


def result_packet_json_schema() -> dict[str, object]:
    """Return the shared JSON schema used in prompts and Codex CLI output mode."""

    object_list_schema = {"type": "array", "items": {"type": "object"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(RESULT_PACKET_REQUIRED_KEYS),
        "properties": {
            "schema_version": {"const": RESULT_PACKET_SCHEMA_VERSION},
            "task_id": {"type": "string", "minLength": 1},
            "summary": {"type": "string", "minLength": 1},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(FINDING_REQUIRED_KEYS),
                    "properties": {
                        "finding_id": {"type": "string", "minLength": 1},
                        "text": {"type": "string", "minLength": 1},
                        "finding_type": {"enum": sorted(FINDING_TYPES)},
                        "citation_ids": {"type": "array", "items": {"type": "string"}},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reasoning_status": {"enum": sorted(REASONING_STATUSES)},
                    },
                },
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(CITATION_REQUIRED_KEYS),
                    "properties": {
                        "citation_id": {"type": "string", "minLength": 1},
                        "target_type": {"enum": sorted(CITATION_TARGET_TYPES)},
                        "target_id": {"type": "string", "minLength": 1},
                        "locator": {"type": "string"},
                        "quoted_text_hash": {
                            "type": "string",
                            "pattern": "^[0-9a-fA-F]{64}$",
                            "description": "Optional. Omit unless Atticus supplied the exact SHA-256 hex digest for the quoted text.",
                        },
                        "quote": {"type": "string"},
                        "excerpt": {"type": "string"},
                    },
                },
            },
            "proposed_artifacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(PROPOSED_ARTIFACT_REQUIRED_KEYS),
                    "properties": {
                        "path": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Relative candidate artifact path. Use candidate/<task_id>/<filename>; never use /home, /tmp, or another absolute filesystem path.",
                        },
                        "artifact_type": {"type": "string", "minLength": 1},
                        "stage": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            },
            "proposed_tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": sorted(PROPOSED_TASK_REQUIRED_KEYS),
                    "properties": {
                        "task_id": {"type": "string", "minLength": 1},
                        "title": {"type": "string", "minLength": 1},
                        "task_type": {"type": "string", "minLength": 1},
                        "stage": {"type": "string", "minLength": 1},
                        "matter_scope": {"type": "string", "minLength": 1},
                        "instructions": {"type": "string", "minLength": 1},
                        "source_dependencies": {"type": "array", "items": {"type": "string"}},
                        "artifact_dependencies": {"type": "array", "items": {"type": "string"}},
                        "task_dependencies": {"type": "array", "items": {"type": "string"}},
                        "matter_dependencies": {"type": "array", "items": {"type": "string"}},
                        "validation_gates": {"type": "array", "items": {"type": "string"}},
                        "required_certifications": object_list_schema,
                        "provider_policy": {"type": "object"},
                        "expected_value": {"type": "number"},
                        "cost_limit_usd": {"type": "number"},
                    },
                },
            },
            "uncertainties": object_list_schema,
            "contradictions": object_list_schema,
            "risk_flags": object_list_schema,
            "redaction_flags": object_list_schema,
            "external_action_requests": {"type": "array", "maxItems": 0, "items": {"type": "object"}},
        },
    }


def _mapping_list(field: str, value: list[object]) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ResultPacketError(f"{field}[{index}] must be a JSON object")
        item_map = cast(Mapping[object, object], item)
        items.append({str(key): value for key, value in item_map.items()})
    return items


def _list_value(payload: Mapping[str, object], field: str) -> list[object]:
    value = payload.get(field)
    if not isinstance(value, list):
        raise ResultPacketError(f"{field} must be a list")
    return cast(list[object], value)


def _required_string(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise ResultPacketError(f"{field} must be a string")
    return value


def _validate_findings(
    value: list[object],
    *,
    citations_by_id: Mapping[str, Mapping[str, object]],
    proof_citation_targets: Mapping[str, set[str]] | None,
) -> list[dict[str, object]]:
    findings = _mapping_list("findings", value)
    seen: set[str] = set()
    for index, finding in enumerate(findings):
        _require_no_extra_keys(f"findings[{index}]", finding, allowed=FINDING_ALLOWED_KEYS, required=FINDING_REQUIRED_KEYS)
        finding_id = _required_item_string(f"findings[{index}]", finding, "finding_id")
        if finding_id in seen:
            raise ResultPacketError(f"duplicate finding_id: {finding_id}")
        seen.add(finding_id)
        _ = _required_item_string(f"findings[{index}]", finding, "text")
        finding_type = _required_item_string(f"findings[{index}]", finding, "finding_type")
        if finding_type not in FINDING_TYPES:
            raise ResultPacketError(f"findings[{index}].finding_type is unsupported: {finding_type}")
        reasoning_status = _required_item_string(f"findings[{index}]", finding, "reasoning_status")
        if reasoning_status not in REASONING_STATUSES:
            raise ResultPacketError(f"findings[{index}].reasoning_status is unsupported: {reasoning_status}")
        confidence = finding.get("confidence")
        if not isinstance(confidence, int | float) or isinstance(confidence, bool) or confidence < 0 or confidence > 1:
            raise ResultPacketError(f"findings[{index}].confidence must be a number from 0 to 1")
        citation_list = finding.get("citation_ids")
        if not isinstance(citation_list, list):
            raise ResultPacketError(f"findings[{index}].citation_ids must be an array of non-empty strings")
        citation_items = cast(list[object], citation_list)
        if not all(isinstance(item, str) and item for item in citation_items):
            raise ResultPacketError(f"findings[{index}].citation_ids must be an array of non-empty strings")
        citation_id_list = cast(list[str], citation_items)
        missing = sorted(set(citation_id_list) - set(citations_by_id))
        if missing:
            raise ResultPacketError(f"findings[{index}] references undefined citation ids: {', '.join(missing)}")
        if finding_type in {"fact", "law", "procedure", "contradiction", "risk"} and reasoning_status not in {"uncertain", "needs_research"}:
            if not citation_id_list:
                raise ResultPacketError(f"findings[{index}] {finding_type} findings require citations or an uncertain reasoning_status")
            proof_targets = set()
            for citation_id in citation_id_list:
                if citation_id not in citations_by_id:
                    continue
                citation = citations_by_id[citation_id]
                target_type = str(citation.get("target_type") or "")
                target_id = str(citation.get("target_id") or "")
                if proof_citation_targets is not None and target_id not in proof_citation_targets.get(target_type, set()):
                    continue
                proof_targets.add(target_type)
            if finding_type == "law" and "authority" not in proof_targets:
                raise ResultPacketError(
                    f"findings[{index}] supported law findings require at least one proof-allowed authority citation; sources may establish case facts but not legal-rule support"
                )
            if not proof_targets.intersection(EVIDENCE_CITATION_TARGET_TYPES):
                raise ResultPacketError(
                    f"findings[{index}] {finding_type} findings require proof-allowed source, artifact, authority, chronology_event, or claim evidence citations; memory, validation_result, derivative extraction artifacts, stale artifacts, and rough drafts are orientation only"
                )
    return findings


def _validate_auxiliary_citation_references(
    field: str,
    value: list[object],
    *,
    citations_by_id: Mapping[str, Mapping[str, object]],
) -> list[dict[str, object]]:
    items = _mapping_list(field, value)
    for index, item in enumerate(items):
        if "citation_ids" not in item:
            continue
        citation_list = item["citation_ids"]
        if not isinstance(citation_list, list):
            raise ResultPacketError(f"{field}[{index}].citation_ids must be an array of non-empty strings")
        citation_items = cast(list[object], citation_list)
        if not all(isinstance(citation_id, str) and citation_id for citation_id in citation_items):
            raise ResultPacketError(f"{field}[{index}].citation_ids must be an array of non-empty strings")
        missing = sorted(set(cast(list[str], citation_items)) - set(citations_by_id))
        if missing:
            raise ResultPacketError(f"{field}[{index}] references undefined citation ids: {', '.join(missing)}")
    return items


def _validate_citations(value: list[object], *, allowed_citation_targets: Mapping[str, set[str]] | None) -> list[dict[str, object]]:
    citations = _mapping_list("citations", value)
    seen: set[str] = set()
    for index, citation in enumerate(citations):
        _require_no_extra_keys(f"citations[{index}]", citation, allowed=CITATION_ALLOWED_KEYS, required=CITATION_REQUIRED_KEYS)
        citation_id = _required_item_string(f"citations[{index}]", citation, "citation_id")
        if citation_id in seen:
            raise ResultPacketError(f"duplicate citation_id: {citation_id}")
        seen.add(citation_id)
        target_type = _required_item_string(f"citations[{index}]", citation, "target_type")
        target_id = _required_item_string(f"citations[{index}]", citation, "target_id")
        _ = _required_item_string(f"citations[{index}]", citation, "locator", allow_empty=True)
        if target_type not in CITATION_TARGET_TYPES:
            raise ResultPacketError(f"citations[{index}].target_type is unsupported: {target_type}")
        quoted_text_hash = citation.get("quoted_text_hash")
        if quoted_text_hash is not None and quoted_text_hash != "":
            if not isinstance(quoted_text_hash, str) or not _is_sha256(quoted_text_hash):
                raise ResultPacketError(f"citations[{index}].quoted_text_hash must be a sha256 hex digest when present")
        if allowed_citation_targets is not None and target_id not in allowed_citation_targets.get(target_type, set()):
            raise ResultPacketError(f"citations[{index}] target {target_type}:{target_id} is outside work order context")
    return citations


def _validate_proposed_artifacts(value: list[object]) -> list[dict[str, object]]:
    artifacts = _mapping_list("proposed_artifacts", value)
    for index, artifact in enumerate(artifacts):
        _require_no_extra_keys(
            f"proposed_artifacts[{index}]",
            artifact,
            allowed=PROPOSED_ARTIFACT_ALLOWED_KEYS,
            required=PROPOSED_ARTIFACT_REQUIRED_KEYS,
        )
        path = _required_item_string(f"proposed_artifacts[{index}]", artifact, "path")
        normalized_path = _normalize_proposed_artifact_path(path)
        first_part = normalized_path.split("/", 1)[0]
        if (
            not normalized_path
            or "\x00" in normalized_path
            or ".." in normalized_path.split("/")
            or normalized_path.startswith(("/", "~/", "~\\"))
            or first_part.endswith(":")
        ):
            raise ResultPacketError(f"proposed_artifacts[{index}].path must be a relative safe path")
        artifact["path"] = normalized_path
        artifact_type = _required_item_string(f"proposed_artifacts[{index}]", artifact, "artifact_type")
        _ = _required_item_string(f"proposed_artifacts[{index}]", artifact, "stage", allow_empty=True)
        _ = _required_item_string(f"proposed_artifacts[{index}]", artifact, "title", allow_empty=True)
        content = _required_item_string(f"proposed_artifacts[{index}]", artifact, "content", allow_empty=True)
        if artifact_type in FULL_TEXT_ARTIFACT_TYPES and _looks_like_incomplete_draft(content):
            raise ResultPacketError(
                f"proposed_artifacts[{index}].content for {artifact_type} must be complete replacement text, not placeholder or omitted sections"
            )
    return artifacts


def _looks_like_incomplete_draft(content: str) -> bool:
    lowered = content.lower()
    return any(marker in lowered for marker in INCOMPLETE_DRAFT_MARKERS)


def _normalize_proposed_artifact_path(path: str) -> str:
    """Return a safe relative candidate path or leave validation to the caller.

    Live workers sometimes echo the repository path from the work order
    examples, for example ``/home/.../matters/<matter>/03-working/foo.md``.
    That is not a write target, but it is unambiguous Atticus-local candidate
    material. Normalize only those known Atticus working/candidate prefixes and
    keep arbitrary absolute paths fail-closed.
    """

    normalized = path.strip().replace("\\", "/")
    if not normalized:
        return ""
    if normalized.startswith("/"):
        looks_atticus_local = "/atticus-harness/" in normalized or "/matters/" in normalized
        if normalized.startswith("/candidate/"):
            normalized = f"candidate/{normalized.split('/candidate/', 1)[1]}"
        elif "/03-working/" in normalized and looks_atticus_local:
            normalized = f"candidate/{normalized.split('/03-working/', 1)[1]}"
        elif "/candidate/" in normalized and looks_atticus_local:
            normalized = f"candidate/{normalized.split('/candidate/', 1)[1]}"
        else:
            return normalized
    parts = [part for part in normalized.split("/") if part and part != "."]
    return "/".join(parts)


def _validate_proposed_tasks(value: list[object]) -> list[dict[str, object]]:
    tasks = _mapping_list("proposed_tasks", value)
    for index, task in enumerate(tasks):
        _require_no_extra_keys(f"proposed_tasks[{index}]", task, allowed=PROPOSED_TASK_ALLOWED_KEYS, required=PROPOSED_TASK_REQUIRED_KEYS)
        for key in PROPOSED_TASK_REQUIRED_KEYS:
            _ = _required_item_string(f"proposed_tasks[{index}]", task, key)
        for key in ("source_dependencies", "artifact_dependencies", "task_dependencies", "matter_dependencies", "validation_gates"):
            if key in task:
                value_raw = task[key]
                if not isinstance(value_raw, list):
                    raise ResultPacketError(f"proposed_tasks[{index}].{key} must be an array of strings")
                value_items = cast(list[object], value_raw)
                if not all(isinstance(item, str) for item in value_items):
                    raise ResultPacketError(f"proposed_tasks[{index}].{key} must be an array of strings")
        if "required_certifications" in task:
            _ = _mapping_list(f"proposed_tasks[{index}].required_certifications", cast(list[object], task["required_certifications"]) if isinstance(task["required_certifications"], list) else [])
    return tasks


def _require_no_extra_keys(field: str, value: Mapping[str, object], *, allowed: frozenset[str], required: frozenset[str]) -> None:
    missing = sorted(required - set(value))
    if missing:
        raise ResultPacketError(f"{field} missing required keys: {', '.join(missing)}")
    extra = sorted(set(value) - allowed)
    if extra:
        raise ResultPacketError(f"{field} has unexpected keys: {', '.join(extra)}")


def _required_item_string(field: str, value: Mapping[str, object], key: str, *, allow_empty: bool = False) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ResultPacketError(f"{field}.{key} must be a string")
    if not allow_empty and not item:
        raise ResultPacketError(f"{field}.{key} must not be empty")
    return item


def _is_sha256(value: str) -> bool:
    return len(value) == SHA256_HEX_LEN and all(ch in "0123456789abcdefABCDEF" for ch in value)


def packet_as_dict(packet: ParsedResultPacket) -> dict[str, object]:
    return {
        "schema_version": packet.schema_version,
        "task_id": packet.task_id,
        "summary": packet.summary,
        "findings": packet.findings,
        "citations": packet.citations,
        "proposed_artifacts": packet.proposed_artifacts,
        "proposed_tasks": packet.proposed_tasks,
        "uncertainties": packet.uncertainties,
        "contradictions": packet.contradictions,
        "risk_flags": packet.risk_flags,
        "redaction_flags": packet.redaction_flags,
        "external_action_requests": packet.external_action_requests,
    }
