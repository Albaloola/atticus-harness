from __future__ import annotations

from typing import cast
from collections.abc import Mapping
import hashlib
from pathlib import Path
import json
import sqlite3


import pytest

from atticus.adapters.base import AdapterBlocked
from atticus.adapters.openclaw import OpenClawAdapter
from atticus.cli import main as cli_main
from atticus.context.packs import build_context_pack
from atticus.core.matters import MatterAccessDenied
from atticus.core.policies import LegalStage, TaskStatus, TrustStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.migration.import_old_run import import_candidates
from atticus.providers.budget import BudgetExceeded, require_budget
from atticus.providers.policy import ProviderActual, ProviderRequest, record_provider_policy_decision
from atticus.reducer.reducer import ReductionBlocked, reduce_candidate
from atticus.retrieval.source_chunks import chunk_extracted_artifact
from atticus.retrieval.ask import answer_question
from atticus.retrieval.index import rebuild_search_index
from atticus.retrieval.source_chunks import chunk_extracted_artifact
from atticus.scheduler.lease import acquire_lease
from atticus.scheduler.planner import select_runnable_tasks
from atticus.validation.canonical_write_guard import CanonicalWriteDenied
from atticus.validation.gates import run_validation
from atticus.workers.outputs import record_worker_result
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _add_extracted_source_chunk(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    content: str,
    path: str = "/candidate/extract.txt",
    matter_scope: str = "atticus",
    confidence: float = 0.9,
) -> str:
    artifact_id = repo.add_artifact(
        conn,
        matter_scope=matter_scope,
        path=path,
        artifact_type="extracted_text",
        content=content,
        source_ids=[source_id],
    )
    _ = chunk_extracted_artifact(
        conn,
        matter_scope=matter_scope,
        source_id=source_id,
        artifact_id=artifact_id,
        confidence=confidence,
    )
    return artifact_id


def _json_mapping(text: str) -> Mapping[str, object]:
    value = json.loads(text)
    assert isinstance(value, Mapping)
    return cast(Mapping[str, object], value)


def _count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(float(str(row["n"])))


def valid_packet(task_id: str) -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "candidate summary",
        "findings": [
            {
                "finding_id": "finding-1",
                "text": "finding",
                "finding_type": "drafting_note",
                "citation_ids": [],
                "confidence": 0.5,
                "reasoning_status": "uncertain",
            }
        ],
        "citations": [],
        "proposed_artifacts": [
            {
                "path": f"canonical/{task_id}.json",
                "artifact_type": "evidence_registry",
                "stage": "S0",
                "title": "Evidence registry",
                "content": "{}",
            }
        ],
        "proposed_tasks": [],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
    }


def cited_fact_packet(task_id: str, source_id: str) -> dict[str, object]:
    packet = valid_packet(task_id)
    packet["findings"] = [
        {
            "finding_id": "finding-1",
            "text": "source-supported fact",
            "finding_type": "fact",
            "citation_ids": ["cite-1"],
            "confidence": 0.8,
            "reasoning_status": "supported",
        }
    ]
    packet["citations"] = [
        {
            "citation_id": "cite-1",
            "target_type": "source",
            "target_id": source_id,
            "locator": "p.1",
            "quoted_text_hash": "a" * 64,
        }
    ]
    return packet


def test_provider_mismatch_is_recorded_and_blocked(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        decision = record_provider_policy_decision(
            conn,
            requested=ProviderRequest("openrouter", "deepseek/deepseek-v4-pro", allow_fallback=False),
            actual=ProviderActual("openrouter", "deepseek/deepseek-v4-flash"),
            task_id="task-provider",
        )
        row = cast(Mapping[str, object], conn.execute("SELECT * FROM provider_runs").fetchone())
        attention = cast(Mapping[str, object], conn.execute("SELECT reason FROM human_attention").fetchone())
    assert not decision.allowed
    assert decision.result == "failed_closed"
    assert row["requested_model"] == "deepseek/deepseek-v4-pro"
    assert row["actual_model"] == "deepseek/deepseek-v4-flash"
    assert "fallback was not allowed" in str(attention["reason"])


def test_stage_foundation_gates_block_downstream_legacy_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="draft-too-early",
                title="Draft too early",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                status=TaskStatus.QUEUED,
            ),
        )
        runnable = select_runnable_tasks(conn, capacity=3)
        row = cast(Mapping[str, object], conn.execute("SELECT blocked_reasons_json FROM tasks WHERE task_id = 'draft-too-early'").fetchone())
    assert runnable == []
    assert "missing certification" in str(row["blocked_reasons_json"])


def test_budget_gate_blocks_over_budget_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _ = repo.add_budget(conn, scope_type="stage", scope_id="S0", limit_usd=0.01)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="expensive",
                title="Expensive indexing",
                task_type="index",
                stage=LegalStage.S0_SOURCE_INVENTORY,
                status=TaskStatus.QUEUED,
                provider_policy={"estimated_cost_usd": 0.50},
            ),
        )
        runnable = select_runnable_tasks(conn, capacity=1)
        row = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'expensive'").fetchone())
        with pytest.raises(BudgetExceeded):
            _ = require_budget(conn, scope_type="stage", scope_id="S0", requested_usd=0.50)

    assert runnable == []
    assert row["status"] == "blocked"
    assert "budget blocked" in str(row["blocked_reasons_json"])


def test_select_runnable_tasks_dry_run_does_not_write_blocked_reasons(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="draft-preview-too-early",
                title="Draft preview too early",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                status=TaskStatus.QUEUED,
            ),
        )
        runnable = select_runnable_tasks(conn, capacity=3, dry_run=True)
        row = cast(Mapping[str, object], conn.execute("SELECT status, blocked_reasons_json FROM tasks WHERE task_id = 'draft-preview-too-early'").fetchone())

    assert runnable == []
    assert row["status"] == TaskStatus.QUEUED
    assert json.loads(str(row["blocked_reasons_json"])) == []


def test_context_packs_are_deterministic_and_fingerprinted(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/a.pdf", sha256="a" * 64)
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/a.txt",
            artifact_type="extraction_record",
            content="same content",
            source_ids=[source_id],
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="ctx-task",
                title="Context task",
                task_type="extract",
                source_dependencies=[source_id],
                artifact_dependencies=[artifact_id],
            ),
        )
        first = build_context_pack(conn, task_id="ctx-task")
        second = build_context_pack(conn, task_id="ctx-task")

    assert first.context_pack_id == second.context_pack_id
    assert first.fingerprint == second.fingerprint
    assert first.sections[0]["name"] == "stable_prefix"


def test_context_pack_rejects_oversized_budget_before_persisting(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/huge.txt",
            artifact_type="extraction_record",
            content="oversized " * 2000,
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="ctx-too-small",
                title="Context too small",
                task_type="extract",
                artifact_dependencies=[artifact_id],
            ),
        )
        with pytest.raises(ValueError, match="token budget"):
            _ = build_context_pack(conn, task_id="ctx-too-small", token_budget=100)
        context_count = _count(conn, "SELECT COUNT(*) AS n FROM context_packs")

    assert context_count == 0


def test_context_pack_rejects_cross_matter_dependencies(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/secret.pdf", sha256="e" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="beta-work-order",
                title="Beta work order",
                task_type="extract",
                matter_scope="beta",
                source_dependencies=[source_id],
            ),
        )
        with pytest.raises(ValueError, match="missing or unauthorized source dependencies"):
            _ = build_context_pack(conn, task_id="beta-work-order")

        context_count = _count(conn, "SELECT COUNT(*) AS n FROM context_packs")

    assert context_count == 0


def test_context_pack_includes_artifacts_from_completed_task_dependencies(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="upstream", title="Upstream", task_type="extract", matter_scope="alpha"))
        artifact_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/upstream.json",
            artifact_type="evidence_registry",
            trust_status=TrustStatus.VALIDATED,
            produced_by_task_id="upstream",
            content="upstream artifact",
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="downstream",
                title="Downstream",
                task_type="citation_audit",
                matter_scope="alpha",
                task_dependencies=["upstream"],
            ),
        )
        pack = build_context_pack(conn, task_id="downstream")

    artifact_bundle = next(section for section in pack.sections if section["name"] == "artifact_bundle")
    citation_targets = next(section for section in pack.sections if section["name"] == "citation_targets")
    artifact_ids = [str(item["artifact_id"]) for item in cast(list[Mapping[str, object]], artifact_bundle["content"])]
    assert artifact_ids == [artifact_id]
    assert artifact_id in cast(Mapping[str, object], citation_targets["content"])["allowed_artifact_targets"]


def test_citation_spans_require_known_records_and_claim_validation(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/evidence.pdf", sha256="b" * 64)
        claim_id = repo.add_claim(conn, claim_text="The record supports this fact.")
        failed = run_validation(conn, gate_name="claim_evidence_support", target_type="claim", target_id=claim_id)
        with pytest.raises(sqlite3.IntegrityError):
            _ = repo.add_citation_span(conn, target_type="claim", target_id=claim_id, source_id="missing")
        _ = repo.add_citation_span(conn, target_type="claim", target_id=claim_id, source_id=source_id)
        passed = run_validation(conn, gate_name="claim_evidence_support", target_type="claim", target_id=claim_id)

    assert not failed.passed
    assert passed.passed


def test_validation_failure_creates_human_attention(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        outcome = run_validation(conn, gate_name="source_inventory", target_type="matter", target_id="atticus")
        attention = cast(Mapping[str, object], conn.execute("SELECT target_type, target_id, reason FROM human_attention").fetchone())
    assert not outcome.passed
    assert attention["target_type"] == "matter"
    assert attention["target_id"] == "atticus"
    assert "validation failed" in str(attention["reason"])


def test_citation_integrity_does_not_claim_quote_support_checked(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/evidence.pdf", sha256="b" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-target-only",
                title="Citation target only",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                source_dependencies=[source_id],
            ),
        )
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="citation-target-only",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=cited_fact_packet("citation-target-only", source_id),
        )
        outcome = run_validation(conn, gate_name="citation_integrity", target_type="candidate", target_id=candidate_id)

    assert outcome.passed
    assert outcome.details["proof_target_checked"] is True
    assert outcome.details["quote_support_checked"] is False
    assert "proof_checked" not in outcome.details


def test_citation_support_integrity_fails_without_quote(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/evidence.pdf", sha256="b" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-support-missing",
                title="Citation support missing",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                source_dependencies=[source_id],
            ),
        )
        packet = cited_fact_packet("citation-support-missing", source_id)
        cast(list[dict[str, object]], packet["citations"])[0].pop("quoted_text_hash", None)
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="citation-support-missing",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=packet,
        )
        outcome = run_validation(conn, gate_name="citation_support_integrity", target_type="candidate", target_id=candidate_id)

    assert not outcome.passed
    assert cast(list[object], outcome.details["missing_quote"])


def test_citation_support_integrity_checks_quote_hash_and_source_material(tmp_path: Path):
    db_path = init_db(tmp_path)
    quote = "The tenant must pay rent by 1 May."
    quote_hash = hashlib.sha256(" ".join(quote.split()).encode("utf-8")).hexdigest()
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/lease.pdf", sha256="c" * 64)
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/lease-extract.txt",
            artifact_type="extracted_text",
            content=f"Opening text. {quote} Closing text.",
            source_ids=[source_id],
        )
        _ = chunk_extracted_artifact(conn, matter_scope="atticus", source_id=source_id, artifact_id=artifact_id, confidence=0.9)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-support-ok",
                title="Citation support ok",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                source_dependencies=[source_id],
            ),
        )
        packet = cited_fact_packet("citation-support-ok", source_id)
        citation = cast(list[dict[str, object]], packet["citations"])[0]
        citation["quote"] = quote
        citation["quoted_text_hash"] = quote_hash
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="citation-support-ok",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=packet,
        )
        outcome = run_validation(conn, gate_name="citation_support_integrity", target_type="candidate", target_id=candidate_id)

    assert outcome.passed
    assert cast(list[object], outcome.details["checked_citations"])
    with repo.db_connection(db_path, read_only=True) as conn:
        rows = conn.execute(
            """
            SELECT finding_id, citation_id, target_type, target_id, quote_hash, support_status, support_level,
                   proposition_text, semantic_support_status, requires_human_review
            FROM citation_support_results
            WHERE candidate_id = ?
            """,
            (candidate_id,),
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["support_status"] == "verified_quote_in_source"
    assert rows[0]["support_level"] == "quote"
    assert rows[0]["quote_hash"] == quote_hash
    assert rows[0]["proposition_text"] == "source-supported fact"
    assert rows[0]["semantic_support_status"] in {"supported", "partially_supported"}
    assert rows[0]["requires_human_review"] == 0


def test_citation_support_integrity_fails_hash_mismatch(tmp_path: Path):
    db_path = init_db(tmp_path)
    quote = "The tenant must pay rent by 1 May."
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/lease.pdf", sha256="c" * 64)
        _add_extracted_source_chunk(
            conn,
            source_id=source_id,
            path="/candidate/lease-extract.txt",
            content=quote,
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-support-bad-hash",
                title="Citation support bad hash",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                source_dependencies=[source_id],
            ),
        )
        packet = cited_fact_packet("citation-support-bad-hash", source_id)
        citation = cast(list[dict[str, object]], packet["citations"])[0]
        citation["quote"] = quote
        citation["quoted_text_hash"] = "d" * 64
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="citation-support-bad-hash",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=packet,
        )
        outcome = run_validation(conn, gate_name="citation_support_integrity", target_type="candidate", target_id=candidate_id)

    assert not outcome.passed
    assert cast(list[object], outcome.details["hash_mismatch"])
    with repo.db_connection(db_path, read_only=True) as conn:
        row = conn.execute(
            "SELECT support_status, reason FROM citation_support_results WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()
    assert row is not None
    assert row["support_status"] == "quote_hash_mismatch"
    assert "quoted_text_hash mismatch" in str(row["reason"])


def test_citation_support_integrity_accepts_ordered_ellipsis_fragments(tmp_path: Path):
    db_path = init_db(tmp_path)
    quote = "Student Name: MISS ANFAL ELBUSHRA ... Rent: GBP 4686.18"
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/lease.pdf", sha256="c" * 64)
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/lease-extract.txt",
            artifact_type="extracted_text",
            content="Student Name: MISS ANFAL ELBUSHRA\nSome intervening terms.\nRent: GBP 4686.18",
            source_ids=[source_id],
        )
        _ = chunk_extracted_artifact(conn, matter_scope="atticus", source_id=source_id, artifact_id=artifact_id, confidence=0.9)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-support-ellipsis",
                title="Citation support ellipsis",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                source_dependencies=[source_id],
            ),
        )
        packet = cited_fact_packet("citation-support-ellipsis", source_id)
        citation = cast(list[dict[str, object]], packet["citations"])[0]
        citation["quote"] = quote
        citation.pop("quoted_text_hash", None)
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="citation-support-ellipsis",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=packet,
        )
        outcome = run_validation(conn, gate_name="citation_support_integrity", target_type="candidate", target_id=candidate_id)

    assert outcome.passed


def test_citation_support_integrity_replaces_prior_support_results(tmp_path: Path):
    db_path = init_db(tmp_path)
    quote = "The tenant must pay rent by 1 May."
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/lease.pdf", sha256="c" * 64)
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/lease-extract.txt",
            artifact_type="extracted_text",
            content=quote,
            source_ids=[source_id],
        )
        _ = chunk_extracted_artifact(conn, matter_scope="atticus", source_id=source_id, artifact_id=artifact_id, confidence=0.9)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-support-replace",
                title="Citation support replace",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                source_dependencies=[source_id],
            ),
        )
        packet = cited_fact_packet("citation-support-replace", source_id)
        citation = cast(list[dict[str, object]], packet["citations"])[0]
        citation["quote"] = quote
        citation.pop("quoted_text_hash", None)
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="citation-support-replace",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=packet,
        )
        first = run_validation(conn, gate_name="citation_support_integrity", target_type="candidate", target_id=candidate_id)
        second = run_validation(conn, gate_name="citation_support_integrity", target_type="candidate", target_id=candidate_id)
        count = conn.execute(
            "SELECT COUNT(*) AS n FROM citation_support_results WHERE candidate_id = ?",
            (candidate_id,),
        ).fetchone()

    assert first.passed
    assert second.passed
    assert count is not None
    assert count["n"] == 1


def _authority_law_packet(task_id: str, authority_id: str) -> dict[str, object]:
    packet = valid_packet(task_id)
    packet["findings"] = [
        {
            "finding_id": "finding-law-1",
            "text": "The authority establishes a legal rule.",
            "finding_type": "law",
            "citation_ids": ["cite-authority-1"],
            "confidence": 0.8,
            "reasoning_status": "supported",
        }
    ]
    packet["citations"] = [
        {
            "citation_id": "cite-authority-1",
            "target_type": "authority",
            "target_id": authority_id,
            "locator": "p.1",
        }
    ]
    return packet


def test_candidate_authority_is_allowed_orientation_but_not_proof(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO legal_authorities(authority_id, matter_scope, jurisdiction, citation, authority_type, title, status, source_url, created_at, updated_at)
            VALUES ('auth-candidate', 'atticus', 'Scotland', 'Example v University [2024] CSOH 1', 'case', 'Example', 'candidate', '', 'now', 'now')
            """
        )
        repo.add_task(conn, TaskSpec(task_id="law-candidate-authority", title="Law", task_type="authority_map", stage=LegalStage.S6_AUTHORITY_LAW_MAP))
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="law-candidate-authority",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=_authority_law_packet("law-candidate-authority", "auth-candidate"),
        )
        outcome = run_validation(conn, gate_name="citation_integrity", target_type="candidate", target_id=candidate_id)

    assert not outcome.passed
    assert "supported law findings require at least one proof-allowed authority citation" in str(outcome.details["error"])


def test_verified_current_authority_is_proof_allowed(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO legal_authorities(authority_id, matter_scope, jurisdiction, citation, authority_type, title, status, source_url, created_at, updated_at)
            VALUES ('auth-verified', 'atticus', 'Scotland', 'Example v University [2024] CSOH 1', 'case', 'Example', 'candidate', '', 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO authority_verifications(authority_verification_id, matter_scope, authority_id, jurisdiction, binding_status,
              currentness_status, proposition_supported, checked_by, checked_at, details_json)
            VALUES ('auth-ver-1', 'atticus', 'auth-verified', 'Scotland', 'persuasive', 'current', 1, 'test', 'now', '{}')
            """
        )
        repo.add_task(conn, TaskSpec(task_id="law-verified-authority", title="Law", task_type="authority_map", stage=LegalStage.S6_AUTHORITY_LAW_MAP))
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="law-verified-authority",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=_authority_law_packet("law-verified-authority", "auth-verified"),
        )
        outcome = run_validation(conn, gate_name="citation_integrity", target_type="candidate", target_id=candidate_id)

    assert outcome.passed


def test_authority_status_without_current_verification_is_orientation_only(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO legal_authorities(authority_id, matter_scope, jurisdiction, citation, authority_type, title, status, source_url, created_at, updated_at)
            VALUES ('auth-status-only', 'atticus', 'Scotland', 'Example v University [2024] CSOH 1', 'case', 'Example', 'verified', '', 'now', 'now')
            """
        )
        repo.add_task(conn, TaskSpec(task_id="law-status-authority", title="Law", task_type="authority_map", stage=LegalStage.S6_AUTHORITY_LAW_MAP))
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="law-status-authority",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=_authority_law_packet("law-status-authority", "auth-status-only"),
        )
        outcome = run_validation(conn, gate_name="citation_integrity", target_type="candidate", target_id=candidate_id)

    assert not outcome.passed
    assert "supported law findings require at least one proof-allowed authority citation" in str(outcome.details["error"])


def test_citation_support_integrity_checks_verified_authority_text_hash(tmp_path: Path):
    db_path = init_db(tmp_path)
    quote = "A tenant may rely on a proved contractual defence."
    quote_hash = hashlib.sha256(" ".join(quote.split()).encode("utf-8")).hexdigest()
    with repo.db_connection(db_path) as conn:
        conn.execute(
            """
            INSERT INTO legal_authorities(authority_id, matter_scope, jurisdiction, citation, authority_type, title, status, source_url, created_at, updated_at)
            VALUES ('auth-text', 'atticus', 'Scotland', 'Example v University [2024] CSOH 1', 'case', 'Example', 'candidate', '', 'now', 'now')
            """
        )
        conn.execute(
            """
            INSERT INTO authority_verifications(authority_verification_id, matter_scope, authority_id, jurisdiction, binding_status,
              currentness_status, proposition_supported, checked_by, checked_at, details_json)
            VALUES ('auth-ver-text', 'atticus', 'auth-text', 'Scotland', 'persuasive', 'current', 1, 'test', 'now', ?)
            """,
            (json.dumps({"authority_text": quote, "authority_text_hash": quote_hash}),),
        )
        repo.add_task(conn, TaskSpec(task_id="law-authority-text", title="Law", task_type="authority_map", stage=LegalStage.S6_AUTHORITY_LAW_MAP))
        packet = _authority_law_packet("law-authority-text", "auth-text")
        citation = cast(list[dict[str, object]], packet["citations"])[0]
        citation["quote"] = quote
        citation["quoted_text_hash"] = quote_hash
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="law-authority-text",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload=packet,
        )
        outcome = run_validation(conn, gate_name="citation_support_integrity", target_type="candidate", target_id=candidate_id)

    assert outcome.passed
    assert cast(list[dict[str, object]], outcome.details["support_statuses"])[0]["support_status"] == "verified_quote_in_authority"


def test_expired_worker_lease_quarantines_late_output(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="late", title="Late worker", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="late", worker_id="worker-1", seconds=-1)
        candidate_id = record_worker_result(
            conn,
            task_id="late",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=valid_packet("late"),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'late'").fetchone())
    assert candidate["status"] == "quarantined"
    assert "expired" in str(candidate["quarantined_reason"])
    assert task["status"] == "quarantined"


def test_reducer_writes_canonical_only_with_valid_lease_and_validations(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="reduce-me", title="Reduce me", task_type="extract"))
        worker_lease = acquire_lease(conn, task_id="reduce-me", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="reduce-me",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=valid_packet("reduce-me"),
        )
        reducer_lease = acquire_lease(conn, task_id="reduce-me", worker_id="reducer-1", lease_role="reducer")
        with pytest.raises(CanonicalWriteDenied):
            _ = reduce_candidate(
                conn,
                candidate_id=candidate_id,
                reducer_lease_id=reducer_lease,
                writer_role="worker",
                dry_run=False,
            )
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        artifact = cast(Mapping[str, object], conn.execute("SELECT trust_status, produced_by_task_id FROM artifacts WHERE artifact_id = ?", (result["artifact_id"],)).fetchone())
    assert str(result["artifact_id"]).startswith("art-")
    assert artifact["trust_status"] == "validated"
    assert artifact["produced_by_task_id"] == "reduce-me"


def test_reducer_creates_decision_packet_task_when_final_certification_is_withheld(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/final-source.pdf", sha256="9" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="final-gate",
                title="Final quality gate",
                task_type="final_quality_gate",
                stage=LegalStage.S9_FINAL_QUALITY_GATE,
                source_dependencies=[source_id],
                expected_value=0.7,
            ),
        )
        validation_id = repo.record_validation(
            conn,
            target_type="matter",
            target_id="atticus",
            gate_name="test-final-prerequisites",
            passed=True,
            matter_scope="atticus",
        )
        for certification_type in ("draft_preparation", "hostile_review"):
            repo.add_certification(
                conn,
                subject_type="matter",
                subject_id="atticus",
                certification_type=certification_type,
                validator="test",
                validation_result_id=validation_id,
            )
        worker_lease = acquire_lease(conn, task_id="final-gate", worker_id="worker-1")
        packet = cited_fact_packet("final-gate", source_id)
        packet["risk_flags"] = [{"risk_id": "risk-1", "description": "unresolved defect", "citation_ids": ["cite-1"]}]
        candidate_id = record_worker_result(
            conn,
            task_id="final-gate",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=packet,
        )
        reducer_lease = acquire_lease(conn, task_id="final-gate", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        certifications = cast(list[dict[str, object]], result["certifications"])
        decision_task_id = str(certifications[0]["decision_task_id"])
        decision_task = cast(
            Mapping[str, object],
            conn.execute("SELECT * FROM tasks WHERE task_id = ?", (decision_task_id,)).fetchone(),
        )
        provider_policy = _json_mapping(str(decision_task["provider_policy_json"]))
        attention_count = _count(
            conn,
            "SELECT COUNT(*) AS n FROM human_attention WHERE target_id = ? AND reason LIKE 'withheld final_quality_gate requires operator decision packet%'",
            (decision_task_id,),
        )
        event_count = _count(
            conn,
            "SELECT COUNT(*) AS n FROM orchestrator_events WHERE event_type = 'orchestrator.certification_decision_task_created'",
        )

    assert certifications[0]["withheld"] is True
    assert certifications[0]["decision_task_created"] is True
    assert decision_task["status"] == TaskStatus.QUEUED
    assert decision_task["stage"] == LegalStage.S9_FINAL_QUALITY_GATE
    assert decision_task["task_type"] == "certification_decision_packet"
    assert provider_policy["model"] == "deepseek/deepseek-v4-pro"
    assert provider_policy["model_decision"]["decision_tier"] == "pro_orchestrator"
    assert attention_count == 1
    assert event_count == 1


def _add_final_prerequisite_certifications(conn: sqlite3.Connection) -> None:
    validation_id = repo.record_validation(
        conn,
        target_type="matter",
        target_id="atticus",
        gate_name="test-final-prerequisites",
        passed=True,
        matter_scope="atticus",
    )
    for certification_type in ("draft_preparation", "hostile_review"):
        repo.add_certification(
            conn,
            subject_type="matter",
            subject_id="atticus",
            certification_type=certification_type,
            validator="test",
            validation_result_id=validation_id,
        )


def test_reducer_withholds_final_certification_without_quote_support(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/final-source.pdf", sha256="8" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="final-gate-no-quote",
                title="Final quality gate",
                task_type="final_quality_gate",
                stage=LegalStage.S9_FINAL_QUALITY_GATE,
                source_dependencies=[source_id],
            ),
        )
        _add_final_prerequisite_certifications(conn)
        worker_lease = acquire_lease(conn, task_id="final-gate-no-quote", worker_id="worker-1")
        packet = cited_fact_packet("final-gate-no-quote", source_id)
        candidate_id = record_worker_result(
            conn,
            task_id="final-gate-no-quote",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=packet,
        )
        reducer_lease = acquire_lease(conn, task_id="final-gate-no-quote", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        certifications = cast(list[dict[str, object]], result["certifications"])
        validation = conn.execute(
            "SELECT gate_name, passed FROM validation_results WHERE validation_result_id = ?",
            (certifications[0]["validation_result_id"],),
        ).fetchone()

    assert certifications[0]["withheld"] is True
    assert certifications[0]["reason"] == "final quality gate citation support/currentness validation failed"
    assert validation["gate_name"] == "citation_support_integrity"
    assert validation["passed"] == 0


def test_reducer_issues_final_certification_with_quote_support(tmp_path: Path):
    db_path = init_db(tmp_path)
    quote = "Final proof supports the certified fact."
    quote_hash = hashlib.sha256(" ".join(quote.split()).encode("utf-8")).hexdigest()
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/final-source.pdf", sha256="8" * 64)
        artifact_id = repo.add_artifact(
            conn,
            path="/candidate/final-extract.txt",
            artifact_type="extracted_text",
            content=quote,
            source_ids=[source_id],
        )
        _ = chunk_extracted_artifact(conn, matter_scope="atticus", source_id=source_id, artifact_id=artifact_id, confidence=0.9)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="final-gate-supported",
                title="Final quality gate",
                task_type="final_quality_gate",
                stage=LegalStage.S9_FINAL_QUALITY_GATE,
                source_dependencies=[source_id],
            ),
        )
        _add_final_prerequisite_certifications(conn)
        worker_lease = acquire_lease(conn, task_id="final-gate-supported", worker_id="worker-1")
        packet = cited_fact_packet("final-gate-supported", source_id)
        citation = cast(list[dict[str, object]], packet["citations"])[0]
        citation["quote"] = quote
        citation["quoted_text_hash"] = quote_hash
        candidate_id = record_worker_result(
            conn,
            task_id="final-gate-supported",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=packet,
        )
        reducer_lease = acquire_lease(conn, task_id="final-gate-supported", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        certifications = cast(list[dict[str, object]], result["certifications"])

    assert certifications[0]["certification_type"] == "final_quality_gate"
    assert "withheld" not in certifications[0]


def test_reducer_surfaces_operator_decision_point_without_new_loop(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, path="/raw/final-source.pdf", sha256="8" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="final-gate-terminal",
                title="Final quality gate terminal",
                task_type="final_quality_gate",
                stage=LegalStage.S9_FINAL_QUALITY_GATE,
                source_dependencies=[source_id],
            ),
        )
        validation_id = repo.record_validation(
            conn,
            target_type="matter",
            target_id="atticus",
            gate_name="test-final-prerequisites",
            passed=True,
            matter_scope="atticus",
        )
        for certification_type in ("draft_preparation", "hostile_review"):
            repo.add_certification(
                conn,
                subject_type="matter",
                subject_id="atticus",
                certification_type=certification_type,
                validator="test",
                validation_result_id=validation_id,
            )
        worker_lease = acquire_lease(conn, task_id="final-gate-terminal", worker_id="worker-1")
        packet = cited_fact_packet("final-gate-terminal", source_id)
        packet["summary"] = "No internal Atticus repairs remain; remaining defects are operator-dependent."
        packet["risk_flags"] = [{"risk_id": "risk-1", "text": "operator decision required", "citation_ids": ["cite-1"]}]
        candidate_id = record_worker_result(
            conn,
            task_id="final-gate-terminal",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=packet,
        )
        reducer_lease = acquire_lease(conn, task_id="final-gate-terminal", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        certifications = cast(list[dict[str, object]], result["certifications"])
        decision_task_count = _count(
            conn,
            "SELECT COUNT(*) AS n FROM tasks WHERE task_id = 'final-gate-terminal--certification-decision'",
        )
        attention_count = _count(
            conn,
            "SELECT COUNT(*) AS n FROM human_attention WHERE target_type = 'matter' AND reason LIKE 'final quality gate reached operator decision point:%'",
        )
        event_count = _count(
            conn,
            "SELECT COUNT(*) AS n FROM orchestrator_events WHERE event_type = 'master_orchestrator.user_intervention_required'",
        )

    assert certifications[0]["withheld"] is True
    assert certifications[0]["decision_task_created"] is False
    assert certifications[0]["decision_task_id"] == ""
    assert decision_task_count == 0
    assert attention_count == 1
    assert event_count == 1


def test_reducer_revalidates_candidate_citations_against_task_context(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        allowed_source = repo.add_source(conn, matter_scope="alpha", path="/alpha/allowed.pdf", sha256="a" * 64)
        outside_source = repo.add_source(conn, matter_scope="alpha", path="/alpha/outside.pdf", sha256="b" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="reduce-citation-context",
                title="Reduce citation context",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[allowed_source],
            ),
        )
        worker_lease = acquire_lease(conn, task_id="reduce-citation-context", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="reduce-citation-context",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=cited_fact_packet("reduce-citation-context", allowed_source),
        )
        tampered = cited_fact_packet("reduce-citation-context", outside_source)
        _ = conn.execute(
            "UPDATE candidate_outputs SET payload_json = ? WHERE candidate_id = ?",
            (json.dumps(tampered, sort_keys=True, separators=(",", ":")), candidate_id),
        )
        outcome = run_validation(conn, gate_name="reducer_packet_schema", target_type="candidate", target_id=candidate_id)
        reducer_lease = acquire_lease(conn, task_id="reduce-citation-context", worker_id="reducer-1", lease_role="reducer")
        with pytest.raises(ReductionBlocked, match="outside work order context"):
            _ = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)

    assert outcome.passed is False
    assert "outside work order context" in str(outcome.details)


def test_reducer_preserves_task_matter_on_canonical_artifact(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="beta-reduce", title="Beta reduce", task_type="extract", matter_scope="beta"))
        worker_lease = acquire_lease(conn, task_id="beta-reduce", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="beta-reduce",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=valid_packet("beta-reduce"),
        )
        reducer_lease = acquire_lease(conn, task_id="beta-reduce", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        artifact = cast(Mapping[str, object], conn.execute("SELECT matter_scope, trust_status, produced_by_task_id FROM artifacts WHERE artifact_id = ?", (result["artifact_id"],)).fetchone())
    assert result["matter_scope"] == "beta"
    assert artifact["matter_scope"] == "beta"
    assert artifact["trust_status"] == "validated"
    assert artifact["produced_by_task_id"] == "beta-reduce"


def test_reducer_links_canonical_artifact_to_cited_sources(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="cited-reduce",
                title="Cited reduce",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        worker_lease = acquire_lease(conn, task_id="cited-reduce", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="cited-reduce",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=cited_fact_packet("cited-reduce", source_id),
        )
        reducer_lease = acquire_lease(conn, task_id="cited-reduce", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        links = conn.execute("SELECT source_id FROM artifact_sources WHERE artifact_id = ?", (result["artifact_id"],)).fetchall()

    assert [str(row["source_id"]) for row in links] == [source_id]


def test_reducer_blocks_if_task_source_becomes_stale_after_candidate_recording(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="stale-before-reduce",
                title="Stale before reduce",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        worker_lease = acquire_lease(conn, task_id="stale-before-reduce", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="stale-before-reduce",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=cited_fact_packet("stale-before-reduce", source_id),
        )
        reducer_lease = acquire_lease(conn, task_id="stale-before-reduce", worker_id="reducer-1", lease_role="reducer")
        _ = conn.execute("UPDATE sources SET stale = 1 WHERE source_id = ?", (source_id,))
        with pytest.raises(ReductionBlocked, match="stale artifacts|task-context schema validation|candidate failed reducer validations"):
            reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        artifact_count = _count(conn, "SELECT COUNT(*) AS n FROM artifacts WHERE produced_by_task_id = 'stale-before-reduce'")

    assert artifact_count == 0


def test_extracted_text_artifact_citation_is_orientation_only_for_fact_finding(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        extraction_artifact = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/source.txt",
            artifact_type="extracted_text",
            content="OCR text",
            source_ids=[source_id],
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="artifact-proof",
                title="Artifact proof",
                task_type="extract",
                matter_scope="alpha",
                artifact_dependencies=[extraction_artifact],
            ),
        )
        lease_id = acquire_lease(conn, task_id="artifact-proof", worker_id="worker-1")
        payload = cited_fact_packet("artifact-proof", source_id)
        payload["citations"] = [
            {
                "citation_id": "cite-1",
                "target_type": "artifact",
                "target_id": extraction_artifact,
                "locator": "ocr excerpt",
                "quoted_text_hash": "a" * 64,
            }
        ]
        candidate_id = record_worker_result(
            conn,
            task_id="artifact-proof",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=payload,
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "quarantined"
    assert "orientation only" in str(candidate["quarantined_reason"])


def test_reducer_imports_accepted_candidate_proposed_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="parent-reduce",
                title="Parent reduce",
                task_type="source_inventory",
                matter_scope="beta",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        _ = repo.add_source(conn, source_id="NAP-SRC-0001", matter_scope="beta", path="/beta/one.pdf", sha256="1" * 64)
        _ = repo.add_source(conn, source_id="NAP-SRC-0002", matter_scope="beta", path="/beta/two.pdf", sha256="2" * 64)
        _ = repo.add_source(conn, source_id="NAP-SRC-0003", matter_scope="beta", path="/beta/three.pdf", sha256="3" * 64)
        worker_lease = acquire_lease(conn, task_id="parent-reduce", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="parent-reduce",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload={
                "schema_version": RESULT_PACKET_SCHEMA_VERSION,
                "task_id": "parent-reduce",
                "summary": "candidate summary",
                "findings": [
                    {
                        "finding_id": "finding-1",
                        "text": "finding",
                        "finding_type": "drafting_note",
                        "citation_ids": [],
                        "confidence": 0.5,
                        "reasoning_status": "uncertain",
                    }
                ],
                "citations": [],
                "proposed_artifacts": [{"path": "canonical/parent.json", "artifact_type": "evidence_registry", "stage": "S0", "title": "Parent", "content": "{}"}],
                "proposed_tasks": [
                    {
                        "task_id": "accepted-followup",
                        "title": "Accepted follow-up",
                        "task_type": "extraction_gap_followup",
                        "matter_scope": "beta",
                        "stage": "S0",
                        "instructions": "Extract NAP-SRC-0001 and NAP-SRC-0002 only.",
                    },
                    {
                        "task_id": "accepted-gap-search",
                        "title": "Accepted gap search",
                        "task_type": "targeted_source_gap_search",
                        "matter_scope": "beta",
                        "stage": "S0",
                        "instructions": "Search the matter source inventory for missing priority documents.",
                    }
                ],
                "uncertainties": [],
                "contradictions": [],
                "risk_flags": [],
                "redaction_flags": [],
                "external_action_requests": [],
            },
        )
        reducer_lease = acquire_lease(conn, task_id="parent-reduce", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        followup = cast(Mapping[str, object], conn.execute("SELECT status, matter_scope, source_dependencies_json, provider_policy_json FROM tasks WHERE task_id = 'accepted-followup'").fetchone())
        gap_search = cast(Mapping[str, object], conn.execute("SELECT source_dependencies_json FROM tasks WHERE task_id = 'accepted-gap-search'").fetchone())

    assert result["imported_tasks"] == ["accepted-followup", "accepted-gap-search"]
    assert followup["status"] == str(TaskStatus.QUEUED)
    assert followup["matter_scope"] == "beta"
    assert json.loads(str(followup["source_dependencies_json"])) == ["NAP-SRC-0001", "NAP-SRC-0002"]
    assert json.loads(str(gap_search["source_dependencies_json"])) == ["NAP-SRC-0001", "NAP-SRC-0002", "NAP-SRC-0003"]
    assert "deepseek/deepseek-v4-flash" in str(followup["provider_policy_json"])


def test_reducer_rejects_proposed_cloud_ocr_tasks_as_unconfigured_external_tools(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="BETA-SRC-0001", matter_scope="beta", path="/beta/one.png", sha256="1" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="parent-ocr-reduce",
                title="Parent OCR reduce",
                task_type="ocr_enhancement",
                matter_scope="beta",
                source_dependencies=[source_id],
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        packet = valid_packet("parent-ocr-reduce")
        packet["proposed_tasks"] = [
            {
                "task_id": "cloud-ocr-followup",
                "title": "Cloud-based OCR follow-up",
                "task_type": "ocr_enhancement",
                "matter_scope": "beta",
                "stage": "S1",
                "instructions": "Use Google Cloud Vision or AWS Textract to OCR BETA-SRC-0001.",
                "source_dependencies": [source_id],
                "provider_policy": {"allow_fallback": True, "capabilities": ["ocr", "document_processing"]},
            }
        ]
        worker_lease = acquire_lease(conn, task_id="parent-ocr-reduce", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="parent-ocr-reduce",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=packet,
        )
        reducer_lease = acquire_lease(conn, task_id="parent-ocr-reduce", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        imported = conn.execute("SELECT 1 FROM tasks WHERE task_id = 'cloud-ocr-followup'").fetchone()
        attention = cast(
            Mapping[str, object],
            conn.execute(
                """
                SELECT reason
                FROM human_attention
                WHERE target_type = 'proposed_task' AND target_id = 'cloud-ocr-followup'
                ORDER BY attention_id DESC
                LIMIT 1
                """
            ).fetchone(),
        )

    assert result["imported_tasks"] == []
    assert imported is None
    assert "external/cloud OCR is not a configured Atticus execution capability" in str(attention["reason"])


def test_reducer_rejects_unscoped_search_followup_tasks(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(
            conn,
            TaskSpec(
                task_id="parent-search-reduce",
                title="Parent search reduce",
                task_type="evidence_search",
                matter_scope="beta",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        packet = valid_packet("parent-search-reduce")
        packet["proposed_tasks"] = [
            {
                "task_id": "unscoped-search-followup",
                "title": "Unscoped search follow-up",
                "task_type": "evidence_search",
                "matter_scope": "beta",
                "stage": "S2",
                "instructions": "Search the matter emails and case notes for related records.",
                "source_dependencies": [],
                "artifact_dependencies": [],
                "task_dependencies": [],
            },
            {
                "task_id": "unscoped-privacy-followup",
                "title": "Unscoped privacy follow-up",
                "task_type": "privacy_review",
                "matter_scope": "beta",
                "stage": "S9",
                "instructions": "Review all original documents for indirect identifiers.",
                "source_dependencies": [],
                "artifact_dependencies": [],
                "task_dependencies": [],
            },
            {
                "task_id": "unscoped-redaction-fix",
                "title": "Unscoped redaction fix",
                "task_type": "redaction_fix",
                "matter_scope": "beta",
                "stage": "S9",
                "instructions": "Fix any remaining redaction problems in the bundle.",
                "source_dependencies": [],
                "artifact_dependencies": [],
                "task_dependencies": [],
            }
        ]
        worker_lease = acquire_lease(conn, task_id="parent-search-reduce", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="parent-search-reduce",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=packet,
        )
        reducer_lease = acquire_lease(conn, task_id="parent-search-reduce", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        imported = conn.execute("SELECT 1 FROM tasks WHERE task_id = 'unscoped-search-followup'").fetchone()
        privacy_imported = conn.execute("SELECT 1 FROM tasks WHERE task_id = 'unscoped-privacy-followup'").fetchone()
        redaction_fix_imported = conn.execute("SELECT 1 FROM tasks WHERE task_id = 'unscoped-redaction-fix'").fetchone()
        attention = cast(
            Mapping[str, object],
            conn.execute(
                """
                SELECT reason
                FROM human_attention
                WHERE target_type = 'proposed_task' AND target_id = 'unscoped-search-followup'
                ORDER BY attention_id DESC
                LIMIT 1
                """
            ).fetchone(),
        )
        privacy_attention = cast(
            Mapping[str, object],
            conn.execute(
                """
                SELECT reason
                FROM human_attention
                WHERE target_type = 'proposed_task' AND target_id = 'unscoped-privacy-followup'
                ORDER BY attention_id DESC
                LIMIT 1
                """
            ).fetchone(),
        )
        redaction_fix_attention = cast(
            Mapping[str, object],
            conn.execute(
                """
                SELECT reason
                FROM human_attention
                WHERE target_type = 'proposed_task' AND target_id = 'unscoped-redaction-fix'
                ORDER BY attention_id DESC
                LIMIT 1
                """
            ).fetchone(),
        )

    assert result["imported_tasks"] == []
    assert imported is None
    assert privacy_imported is None
    assert redaction_fix_imported is None
    assert "proposed source/evidence search or review has no source, artifact, or task scope" in str(attention["reason"])
    assert "proposed source/evidence search or review has no source, artifact, or task scope" in str(privacy_attention["reason"])
    assert "proposed source/evidence search or review has no source, artifact, or task scope" in str(redaction_fix_attention["reason"])


def test_reducer_rejects_repeated_scoped_followup_loop_and_dedupes_attention(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="BETA-SRC-0001", matter_scope="beta", path="/beta/one.pdf", sha256="1" * 64)
        previous = ""
        for index in range(5):
            task_id = f"search-loop-{index}"
            repo.add_task(
                conn,
                TaskSpec(
                    task_id=task_id,
                    title=f"Search loop {index}",
                    task_type="source_search",
                    matter_scope="beta",
                    source_dependencies=[source_id],
                    provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False, "estimated_cost_usd": 0.0},
                ),
            )
            if previous:
                _ = conn.execute("UPDATE tasks SET parent_task_id = ? WHERE task_id = ?", (previous, task_id))
            previous = task_id

        packet = valid_packet(previous)
        packet["proposed_tasks"] = [
            {
                "task_id": "search-loop-5",
                "title": "Search loop 5",
                "task_type": "source_search",
                "matter_scope": "beta",
                "stage": "S2",
                "instructions": "Search BETA-SRC-0001 again for the same records.",
                "source_dependencies": [source_id],
            }
        ]
        worker_lease = acquire_lease(conn, task_id=previous, worker_id="worker-1")
        candidate_id = record_worker_result(conn, task_id=previous, lease_id=worker_lease, worker_id="worker-1", payload=packet)
        reducer_lease = acquire_lease(conn, task_id=previous, worker_id="reducer-1", lease_role="reducer")
        first = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)

        # Re-importing the same reduced candidate should not create duplicate open attention.
        _ = repo.add_task(
            conn,
            TaskSpec(
                task_id="search-loop-repeat",
                title="Search loop repeat",
                task_type="source_search",
                matter_scope="beta",
                source_dependencies=[source_id],
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        _ = conn.execute("UPDATE tasks SET parent_task_id = ? WHERE task_id = 'search-loop-repeat'", (previous,))
        repeat_packet = valid_packet("search-loop-repeat")
        repeat_packet["proposed_tasks"] = packet["proposed_tasks"]
        repeat_worker_lease = acquire_lease(conn, task_id="search-loop-repeat", worker_id="worker-2")
        repeat_candidate_id = record_worker_result(conn, task_id="search-loop-repeat", lease_id=repeat_worker_lease, worker_id="worker-2", payload=repeat_packet)
        repeat_reducer_lease = acquire_lease(conn, task_id="search-loop-repeat", worker_id="reducer-2", lease_role="reducer")
        second = reduce_candidate(conn, candidate_id=repeat_candidate_id, reducer_lease_id=repeat_reducer_lease, dry_run=False)

        imported = conn.execute("SELECT 1 FROM tasks WHERE task_id = 'search-loop-5'").fetchone()
        attention_count = _count(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM human_attention
            WHERE target_type = 'proposed_task'
              AND target_id = 'search-loop-5'
              AND status = 'open'
            """,
        )
        duplicate_events = _count(
            conn,
            """
            SELECT COUNT(*) AS n
            FROM events
            WHERE event_type = 'proposed_task.rejection_duplicate_seen'
            """,
        )

    assert first["imported_tasks"] == []
    assert second["imported_tasks"] == []
    assert imported is None
    assert attention_count == 1
    assert duplicate_events == 1


def test_reducer_rejects_cross_matter_proposed_tasks_and_task_id_collisions(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="BETA-SRC-0001", matter_scope="beta", path="/beta/one.pdf", sha256="1" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="parent-collision-reduce",
                title="Parent collision reduce",
                task_type="source_inventory",
                matter_scope="beta",
                provider_policy={"provider": "openrouter", "model": "deepseek/deepseek-v4-flash", "allow_fallback": False, "estimated_cost_usd": 0.0},
            ),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="existing-followup",
                title="Existing follow-up",
                task_type="source_inventory",
                matter_scope="beta",
                source_dependencies=[source_id],
            ),
        )
        packet = valid_packet("parent-collision-reduce")
        packet["proposed_tasks"] = [
            {
                "task_id": "cross-matter-followup",
                "title": "Cross matter follow-up",
                "task_type": "source_inventory",
                "matter_scope": "alpha",
                "stage": "S0",
                "instructions": "This must not be imported into alpha.",
            },
            {
                "task_id": "existing-followup",
                "title": "Collision follow-up",
                "task_type": "source_inventory",
                "matter_scope": "beta",
                "stage": "S0",
                "instructions": "This must not mutate source dependencies.",
                "source_dependencies": [],
            },
        ]
        worker_lease = acquire_lease(conn, task_id="parent-collision-reduce", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="parent-collision-reduce",
            lease_id=worker_lease,
            worker_id="worker-1",
            payload=packet,
        )
        reducer_lease = acquire_lease(conn, task_id="parent-collision-reduce", worker_id="reducer-1", lease_role="reducer")
        result = reduce_candidate(conn, candidate_id=candidate_id, reducer_lease_id=reducer_lease, dry_run=False)
        cross = conn.execute("SELECT 1 FROM tasks WHERE task_id = 'cross-matter-followup'").fetchone()
        existing = cast(Mapping[str, object], conn.execute("SELECT source_dependencies_json FROM tasks WHERE task_id = 'existing-followup'").fetchone())
        rejected = _count(conn, "SELECT COUNT(*) AS n FROM human_attention WHERE target_type = 'proposed_task' AND severity = 'blocker'")

    assert result["imported_tasks"] == []
    assert cross is None
    assert json.loads(str(existing["source_dependencies_json"])) == [source_id]
    assert rejected == 2


def test_migration_imports_drafts_as_rough_notes_and_never_certifies(tmp_path: Path):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "legacy"
    drafts = workspace / "case" / "drafts"
    drafts.mkdir(parents=True)
    _ = (drafts / "appeal_draft.md").write_text("draft only", encoding="utf-8")

    with repo.db_connection(db_path) as conn:
        result = import_candidates(conn, workspace=workspace, dry_run=False)
        artifact = cast(Mapping[str, object], conn.execute("SELECT artifact_type, trust_status FROM artifacts").fetchone())
        cert_count = _count(conn, "SELECT COUNT(*) AS n FROM certifications")

    assert len(result.candidates) == 1
    assert artifact["artifact_type"] == "draft"
    assert artifact["trust_status"] == "rough_note"
    assert cert_count == 0


def test_openclaw_adapter_never_starts_accidentally():
    with pytest.raises(AdapterBlocked):
        OpenClawAdapter().launch()


def test_factory_cli_dry_runs_do_not_launch_or_mutate_execution_state(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="cli-task", title="CLI task", task_type="extract"))

    assert cli_main(["schedule", "--db", str(db_path), "--capacity", "1", "--dry-run"]) == 0
    assert cli_main(["lease", "--db", str(db_path), "--task-id", "cli-task", "--dry-run"]) == 0
    assert cli_main(["work-order", "--db", str(db_path), "--task-id", "cli-task", "--dry-run"]) == 0

    with repo.db_connection(db_path) as conn:
        lease_count = _count(conn, "SELECT COUNT(*) AS n FROM leases")
        context_count = _count(conn, "SELECT COUNT(*) AS n FROM context_packs")

    assert lease_count == 0
    assert context_count == 0


def test_factory_cli_run_local_requires_write_and_then_records_candidate(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="cli-local", title="CLI local", task_type="extract"))
        lease_id = acquire_lease(conn, task_id="cli-local", worker_id="atticus-local")

    assert cli_main([
        "run-local",
        "--db",
        str(db_path),
        "--task-id",
        "cli-local",
        "--lease-id",
        lease_id,
        "--output-dir",
        str(tmp_path / "out"),
    ]) == 0
    with repo.db_connection(db_path) as conn:
        assert _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs") == 0

    assert cli_main([
        "run-local",
        "--db",
        str(db_path),
        "--task-id",
        "cli-local",
        "--lease-id",
        lease_id,
        "--worker-id",
        "atticus-local",
        "--output-dir",
        str(tmp_path / "out"),
        "--write",
    ]) == 0
    with repo.db_connection(db_path) as conn:
        assert _count(conn, "SELECT COUNT(*) AS n FROM candidate_outputs WHERE status = 'candidate'") == 1


def test_factory_cli_rebuild_search_index_requires_write_and_records_projection(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        artifact_id = repo.add_artifact(
            conn,
            path="/validated/cli-index.txt",
            artifact_type="production_crosswalk",
            title="CLI index",
            content="CLI rebuild index evidence",
            trust_status=TrustStatus.VALIDATED,
        )

    assert cli_main(["rebuild-search-index", "--db", str(db_path)]) == 0
    dry_run_output = _json_mapping(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        assert _count(conn, "SELECT COUNT(*) AS n FROM search_index_entries") == 0
        assert _count(conn, "SELECT COUNT(*) AS n FROM index_rebuilds") == 0

    assert dry_run_output["dry_run"] is True
    assert dry_run_output["matter_scope"] == "atticus"
    assert dry_run_output["requires_write"] is True

    assert cli_main(["rebuild-search-index", "--db", str(db_path), "--write"]) == 0
    write_output = _json_mapping(capsys.readouterr().out)
    with repo.db_connection(db_path) as conn:
        indexed = cast(Mapping[str, object], conn.execute("SELECT record_id, matter_scope FROM search_index_entries").fetchone())
        rebuild_count = _count(conn, "SELECT COUNT(*) AS n FROM index_rebuilds")
        event_count = _count(conn, "SELECT COUNT(*) AS n FROM events WHERE event_type = 'search_index.rebuilt'")

    assert write_output["dry_run"] is False
    assert write_output["entry_count"] == 1
    assert write_output["matter_scope"] == "atticus"
    assert indexed["record_id"] == artifact_id
    assert indexed["matter_scope"] == "atticus"
    assert rebuild_count == 1
    assert event_count == 1


def test_matter_scoped_cli_requires_authorized_execution_context(tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _ = repo.add_artifact(
            conn,
            matter_scope="beta",
            path="/beta/authorized.txt",
            artifact_type="matter_note",
            content="betaauthorized evidence",
            trust_status=TrustStatus.VALIDATED,
        )

    assert cli_main(["ask", "--db", str(db_path), "--matter", "beta", "betaauthorized"]) == 2
    assert "not authorized" in capsys.readouterr().err
    assert cli_main(["rebuild-search-index", "--db", str(db_path), "--matter", "beta"]) == 2
    assert "not authorized" in capsys.readouterr().err
    assert cli_main(["rebuild-search-index", "--db", str(db_path), "--matter", "beta", "--write"]) == 2
    assert "not authorized" in capsys.readouterr().err

    monkeypatch.setenv("ATTICUS_AUTHORIZED_MATTER", "beta")
    assert cli_main(["ask", "--db", str(db_path), "--matter", "beta", "betaauthorized"]) == 0
    assert "betaauthorized" in capsys.readouterr().out
    assert cli_main(["rebuild-search-index", "--db", str(db_path), "--matter", "beta", "--write"]) == 0
    write_output = _json_mapping(capsys.readouterr().out)
    assert write_output["matter_scope"] == "beta"


def test_matter_scoped_api_requires_authorized_context(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        _ = repo.add_artifact(
            conn,
            matter_scope="beta",
            path="/beta/api.txt",
            artifact_type="matter_note",
            content="betaapi evidence",
            trust_status=TrustStatus.VALIDATED,
        )
        with pytest.raises(MatterAccessDenied):
            _ = rebuild_search_index(conn, matter_scope="beta")
        _ = rebuild_search_index(conn, matter_scope="beta", authorized_matter_scope="beta")

    with pytest.raises(MatterAccessDenied):
        _ = answer_question(str(db_path), "betaapi", matter_scope="beta")
    answer = answer_question(str(db_path), "betaapi", matter_scope="beta", authorized_matter_scope="beta")

    assert answer.citations
    assert answer.citations[0].path == "/beta/api.txt"
