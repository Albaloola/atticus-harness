"""Catalog of historical bad inputs that Atticus must reject or repair."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


ExpectedOutcome = Literal["reject", "repair", "operator_attention"]


@dataclass(frozen=True)
class BadFixture:
    fixture_id: str
    category: str
    expected_outcome: ExpectedOutcome
    reason: str
    payload: Mapping[str, object]
    allowed_citation_targets: Mapping[str, set[str]] | None = None
    proof_citation_targets: Mapping[str, set[str]] | None = None


def base_worker_packet(task_id: str = "bad-fixture-task") -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "bad fixture packet",
        "findings": [],
        "citations": [],
        "proposed_artifacts": [],
        "proposed_tasks": [],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
    }


def bad_worker_packet_fixtures() -> tuple[BadFixture, ...]:
    return (
        _missing_citation_id(),
        _fabricated_citation_target(),
        _derivative_ocr_artifact_as_fact_proof(),
        _draft_artifact_as_fact_proof(),
        _unsupported_law_without_authority(),
        _candidate_authority_as_law_proof(),
        _external_cloud_ocr_request(),
        _unscoped_proposed_search_task(),
        _cost_limit_below_estimate(),
    )


def provider_control_plane_fixtures() -> tuple[BadFixture, ...]:
    return (
        BadFixture(
            fixture_id="provider-401",
            category="provider_control_plane",
            expected_outcome="operator_attention",
            reason="OpenRouter authentication failures are provider blockers, not worker-quality failures.",
            payload={"status_code": 401, "body": {"error": "invalid API key"}, "provider": "openrouter"},
        ),
        BadFixture(
            fixture_id="provider-timeout",
            category="provider_transient",
            expected_outcome="repair",
            reason="Provider timeouts should release leases and requeue or repair without poisoning task quality state.",
            payload={"error_type": "timeout", "provider": "openrouter", "elapsed_seconds": 120},
        ),
        BadFixture(
            fixture_id="provider-reasoning-only-no-json",
            category="worker_contract",
            expected_outcome="repair",
            reason="Reasoning-only responses without the worker JSON packet need contract repair or retry.",
            payload={"content": "I thought through the problem but did not emit JSON."},
        ),
        BadFixture(
            fixture_id="context-over-budget",
            category="context_budget",
            expected_outcome="repair",
            reason="Context over-budget failures should trigger decomposition or compaction repair.",
            payload={"estimated_tokens": 200_000, "limit_tokens": 120_000},
        ),
        BadFixture(
            fixture_id="stale-context-pack",
            category="reuse_staleness",
            expected_outcome="reject",
            reason="A context pack with old source hashes must not be reused as current work.",
            payload={"source_id": "NAP-SRC-0001", "pack_sha256": "old", "current_sha256": "new"},
        ),
        BadFixture(
            fixture_id="high-risk-reducer-pending-no-review",
            category="reducer_review",
            expected_outcome="operator_attention",
            reason="S6-S9 reducer-pending work must appear in reducer review and matter-health next action.",
            payload={"stage": "S9", "candidate_status": "reducer_pending", "review_queue_item": None},
        ),
    )


def all_bad_fixtures() -> tuple[BadFixture, ...]:
    return bad_worker_packet_fixtures() + provider_control_plane_fixtures()


def _finding(finding_type: str, citation_ids: list[str], *, reasoning_status: str = "supported") -> dict[str, object]:
    return {
        "finding_id": f"finding-{finding_type}",
        "text": f"fixture {finding_type}",
        "finding_type": finding_type,
        "citation_ids": citation_ids,
        "confidence": 0.8,
        "reasoning_status": reasoning_status,
    }


def _citation(citation_id: str, target_type: str, target_id: str) -> dict[str, object]:
    return {
        "citation_id": citation_id,
        "target_type": target_type,
        "target_id": target_id,
        "locator": "p.1",
        "quote": "fixture quote",
    }


def _missing_citation_id() -> BadFixture:
    packet = base_worker_packet()
    packet["findings"] = [_finding("fact", ["missing-cite"])]
    return BadFixture(
        fixture_id="missing-citation-id",
        category="worker_packet",
        expected_outcome="reject",
        reason="Findings cannot reference citation IDs that are absent from the packet.",
        payload=packet,
    )


def _fabricated_citation_target() -> BadFixture:
    packet = base_worker_packet()
    packet["findings"] = [_finding("fact", ["cite-1"])]
    packet["citations"] = [_citation("cite-1", "source", "NAP-SRC-9999")]
    return BadFixture(
        fixture_id="fabricated-citation-target",
        category="worker_packet",
        expected_outcome="reject",
        reason="Citation targets must come from the task allow-list.",
        payload=packet,
        allowed_citation_targets={"source": {"NAP-SRC-0001"}},
        proof_citation_targets={"source": {"NAP-SRC-0001"}},
    )


def _derivative_ocr_artifact_as_fact_proof() -> BadFixture:
    packet = base_worker_packet()
    packet["findings"] = [_finding("fact", ["cite-ocr"])]
    packet["citations"] = [_citation("cite-ocr", "artifact", "ART-OCR-1")]
    return BadFixture(
        fixture_id="derivative-ocr-artifact-as-proof",
        category="worker_packet",
        expected_outcome="reject",
        reason="OCR text artifacts orient the model; material facts must cite the source, not the derivative artifact.",
        payload=packet,
        allowed_citation_targets={"artifact": {"ART-OCR-1"}},
        proof_citation_targets={"source": {"NAP-SRC-0001"}},
    )


def _draft_artifact_as_fact_proof() -> BadFixture:
    packet = base_worker_packet()
    packet["findings"] = [_finding("fact", ["cite-draft"])]
    packet["citations"] = [_citation("cite-draft", "artifact", "ART-DRAFT-1")]
    return BadFixture(
        fixture_id="draft-artifact-as-fact-proof",
        category="worker_packet",
        expected_outcome="reject",
        reason="Draft/review artifacts cannot be sole proof for underlying legal facts.",
        payload=packet,
        allowed_citation_targets={"artifact": {"ART-DRAFT-1"}},
        proof_citation_targets={"source": {"NAP-SRC-0001"}},
    )


def _unsupported_law_without_authority() -> BadFixture:
    packet = base_worker_packet()
    packet["findings"] = [_finding("law", ["cite-source"])]
    packet["citations"] = [_citation("cite-source", "source", "NAP-SRC-0001")]
    return BadFixture(
        fixture_id="unsupported-law-without-authority",
        category="worker_packet",
        expected_outcome="reject",
        reason="Supported law findings require proof-allowed authority citations.",
        payload=packet,
        allowed_citation_targets={"source": {"NAP-SRC-0001"}},
        proof_citation_targets={"source": {"NAP-SRC-0001"}},
    )


def _candidate_authority_as_law_proof() -> BadFixture:
    packet = base_worker_packet()
    packet["findings"] = [_finding("law", ["cite-authority"])]
    packet["citations"] = [_citation("cite-authority", "authority", "AUTH-CANDIDATE-1")]
    return BadFixture(
        fixture_id="candidate-authority-as-law-proof",
        category="worker_packet",
        expected_outcome="reject",
        reason="Candidate or unverified authorities may orient but must not prove legal propositions.",
        payload=packet,
        allowed_citation_targets={"authority": {"AUTH-CANDIDATE-1"}},
        proof_citation_targets={"authority": set()},
    )


def _external_cloud_ocr_request() -> BadFixture:
    packet = base_worker_packet()
    packet["external_action_requests"] = [{"type": "cloud_ocr", "provider": "external"}]
    return BadFixture(
        fixture_id="external-cloud-ocr-request",
        category="worker_packet",
        expected_outcome="reject",
        reason="Workers must not request external legal/evidence actions directly.",
        payload=packet,
    )


def _unscoped_proposed_search_task() -> BadFixture:
    packet = base_worker_packet()
    packet["proposed_tasks"] = [
        {
            "task_id": "unscoped-search",
            "title": "Search broadly",
            "task_type": "search",
            "stage": "S2",
            "matter_scope": "napier",
            "instructions": "Search for records without scoped source dependencies.",
        }
    ]
    return BadFixture(
        fixture_id="unscoped-proposed-search-task",
        category="proposed_task",
        expected_outcome="repair",
        reason="Unscoped proposed search/review tasks must be rejected or rewritten with explicit source/matter scope.",
        payload=packet,
    )


def _cost_limit_below_estimate() -> BadFixture:
    packet = base_worker_packet()
    packet["proposed_tasks"] = [
        {
            "task_id": "zero-budget-task",
            "title": "Impossible budget",
            "task_type": "source_inventory",
            "stage": "S0",
            "matter_scope": "napier",
            "instructions": "Run live model work with no budget.",
            "provider_policy": {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro", "estimated_cost_usd": 0.01},
            "cost_limit_usd": 0,
        }
    ]
    return BadFixture(
        fixture_id="cost-limit-below-estimate",
        category="proposed_task",
        expected_outcome="repair",
        reason="Imported/proposed tasks with impossible cost limits need normalization or budget approval, not repeated worker failure.",
        payload=packet,
    )
