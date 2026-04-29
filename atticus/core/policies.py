"""Core safety policy definitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LegalStage(StrEnum):
    S0_SOURCE_INVENTORY = "S0"
    S1_EXTRACTION = "S1"
    S2_EVIDENCE_REGISTRY = "S2"
    S3_PRODUCTION_STATUS = "S3"
    S4_BASELINE_CHRONOLOGY = "S4"
    S5_ISSUE_ROUTE_MAP = "S5"
    S6_AUTHORITY_LAW_MAP = "S6"
    S7_HOSTILE_REVIEW = "S7"
    S8_DRAFT_PREPARATION = "S8"
    S9_FINAL_QUALITY_GATE = "S9"


class TrustStatus(StrEnum):
    CANDIDATE = "candidate"
    ROUGH_NOTE = "rough_note"
    UNVERIFIED_LEGACY = "unverified_legacy"
    VALIDATED = "validated"
    CERTIFIED = "certified"
    STALE = "stale"
    REJECTED = "rejected"
    CONFLICTED = "conflicted"


class IntentCategory(StrEnum):
    READ_ONLY_QUERY = "READ_ONLY_QUERY"
    STATUS_QUERY = "STATUS_QUERY"
    CONTROL_COMMAND = "CONTROL_COMMAND"
    WORK_ORDER = "WORK_ORDER"
    LEGAL_DRAFT_REQUEST = "LEGAL_DRAFT_REQUEST"
    VALIDATION_REQUEST = "VALIDATION_REQUEST"
    EXTERNAL_ACTION = "EXTERNAL_ACTION"


class TaskStatus(StrEnum):
    QUEUED = "queued"
    READY = "ready"
    LEASED = "leased"
    RUNNING = "running"
    REDUCER_PENDING = "reducer_pending"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"
    QUARANTINED = "quarantined"


READ_ONLY_INTENTS = {IntentCategory.READ_ONLY_QUERY, IntentCategory.STATUS_QUERY}


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str
    intent: IntentCategory | None = None


def classify_intent(text: str) -> IntentCategory:
    """Small conservative intent classifier for CLI ask mode."""

    lowered = text.strip().lower()
    external_terms = ("file ", "email ", "send ", "upload ", "serve ", "contact ")
    work_terms = ("run ", "rerun ", "resume ", "start worker", "launch worker", "do the task")
    draft_terms = ("draft ", "write a motion", "prepare filing", "complaint", "brief")
    validation_terms = ("validate ", "certify ", "quality gate")
    status_terms = ("status", "what is blocked", "blocked tasks", "run state")

    if any(term in lowered for term in external_terms):
        return IntentCategory.EXTERNAL_ACTION
    if any(term in lowered for term in work_terms):
        return IntentCategory.WORK_ORDER
    if any(term in lowered for term in draft_terms):
        return IntentCategory.LEGAL_DRAFT_REQUEST
    if any(term in lowered for term in validation_terms):
        return IntentCategory.VALIDATION_REQUEST
    if any(term in lowered for term in status_terms):
        return IntentCategory.STATUS_QUERY
    if lowered.startswith(("/", "atticus ")):
        return IntentCategory.CONTROL_COMMAND
    return IntentCategory.READ_ONLY_QUERY


def enforce_read_only_intent(text: str) -> PolicyDecision:
    intent = classify_intent(text)
    if intent in READ_ONLY_INTENTS:
        return PolicyDecision(True, "read-only retrieval allowed", intent)
    if intent == IntentCategory.EXTERNAL_ACTION:
        return PolicyDecision(False, "external legal actions are blocked", intent)
    return PolicyDecision(False, "requires explicit approved work-order mode", intent)


STAGE_FOUNDATION_REQUIREMENTS: dict[str, list[dict[str, str]]] = {
    LegalStage.S1_EXTRACTION: [
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "source_inventory"},
    ],
    LegalStage.S2_EVIDENCE_REGISTRY: [
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "source_inventory"},
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "extraction_coverage"},
    ],
    LegalStage.S3_PRODUCTION_STATUS: [
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "evidence_registry"},
    ],
    LegalStage.S4_BASELINE_CHRONOLOGY: [
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "evidence_registry"},
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "production_mapping"},
    ],
    LegalStage.S5_ISSUE_ROUTE_MAP: [
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "chronology_citations"},
    ],
    LegalStage.S6_AUTHORITY_LAW_MAP: [
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "issue_route_map"},
    ],
    LegalStage.S7_HOSTILE_REVIEW: [
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "issue_route_map"},
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "authority_map"},
    ],
    LegalStage.S8_DRAFT_PREPARATION: [
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "chronology_citations"},
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "authority_map"},
        # Hostile review gates final QA, not draft creation; otherwise S8 and S7 deadlock.
    ],
    LegalStage.S9_FINAL_QUALITY_GATE: [
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "draft_preparation"},
        {"subject_type": "matter", "subject_id": "atticus", "certification_type": "hostile_review"},
    ],
}
