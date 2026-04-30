from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import cast

import pytest

from atticus.core.tasks import TaskSpec
from atticus.core.policies import TaskStatus, TrustStatus
from atticus.db import repo
from atticus.reducer.reducer import MATTER_CERTIFICATIONS_BY_TASK_TYPE, _certification_blocker
from atticus.scheduler.lease import acquire_lease
from atticus.workers.outputs import record_worker_result
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION, ResultPacketError, parse_result


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _count(conn: sqlite3.Connection, sql: str) -> int:
    row = conn.execute(sql).fetchone()
    assert row is not None
    return int(str(row[0]))


def v2_packet(task_id: str, *, citation_target_id: str | None = None) -> dict[str, object]:
    citation_id = "cite-source-1"
    citations = []
    citation_ids: list[str] = []
    if citation_target_id:
        citation_ids = [citation_id]
        citations.append(
            {
                "citation_id": citation_id,
                "target_type": "source",
                "target_id": citation_target_id,
                "locator": "p.1",
                "quoted_text_hash": "a" * 64,
            }
        )
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "Evidence-bound candidate summary.",
        "findings": [
            {
                "finding_id": "finding-1",
                "text": "The candidate states only what the cited source supports.",
                "finding_type": "fact" if citation_target_id else "drafting_note",
                "citation_ids": citation_ids,
                "confidence": 0.85 if citation_target_id else 0.5,
                "reasoning_status": "supported" if citation_target_id else "uncertain",
            }
        ],
        "citations": citations,
        "proposed_artifacts": [
            {
                "path": f"candidate/{task_id}.json",
                "artifact_type": "evidence_registry",
                "stage": "S0",
                "title": "Candidate evidence registry",
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


def artifact_citation_packet(task_id: str, artifact_id: str) -> dict[str, object]:
    packet = v2_packet(task_id)
    packet["citations"] = [
        {
            "citation_id": "cite-artifact-1",
            "target_type": "artifact",
            "target_id": artifact_id,
            "locator": "extracted text",
        }
    ]
    findings = cast(list[dict[str, object]], packet["findings"])
    findings[0].update(
        {
            "finding_type": "fact",
            "citation_ids": ["cite-artifact-1"],
            "confidence": 0.8,
            "reasoning_status": "supported",
        }
    )
    return packet


def validation_result_packet(task_id: str, validation_result_id: int) -> dict[str, object]:
    packet = v2_packet(task_id)
    packet["citations"] = [
        {
            "citation_id": "cite-validation-1",
            "target_type": "validation_result",
            "target_id": str(validation_result_id),
            "locator": "gate",
        }
    ]
    findings = cast(list[dict[str, object]], packet["findings"])
    findings[0]["citation_ids"] = ["cite-validation-1"]
    return packet


def test_result_packet_v2_rejects_missing_version_and_extra_fields():
    packet = v2_packet("packet-task")
    missing_version = dict(packet)
    del missing_version["schema_version"]

    with pytest.raises(ResultPacketError, match="schema_version"):
        _ = parse_result(missing_version)

    extra = dict(packet)
    extra["model_notes"] = "uncontrolled"
    with pytest.raises(ResultPacketError, match="unexpected worker result keys"):
        _ = parse_result(extra, strict=True)


def test_result_packet_v2_rejects_finding_citation_id_not_defined():
    packet = v2_packet("packet-task")
    findings = cast(list[dict[str, object]], packet["findings"])
    findings[0]["citation_ids"] = ["missing-citation"]

    with pytest.raises(ResultPacketError, match="undefined citation ids"):
        _ = parse_result(packet)


def test_result_packet_v2_rejects_auxiliary_citation_id_not_defined():
    packet = v2_packet("packet-task")
    packet["uncertainties"] = [
        {
            "uncertainty_id": "uncertainty-1",
            "text": "uncertain point",
            "citation_ids": ["missing-citation"],
        }
    ]

    with pytest.raises(ResultPacketError, match="uncertainties\\[0\\] references undefined citation ids"):
        _ = parse_result(packet)


def test_result_packet_v2_accepts_auxiliary_citation_id_when_defined():
    packet = v2_packet("packet-task", citation_target_id="SRC-0001")
    packet["uncertainties"] = [
        {
            "uncertainty_id": "uncertainty-1",
            "text": "uncertain point",
            "citation_ids": ["cite-source-1"],
        }
    ]

    parsed = parse_result(packet)

    assert parsed.uncertainties[0]["citation_ids"] == ["cite-source-1"]


def test_result_packet_v2_normalizes_atticus_working_artifact_path():
    packet = v2_packet("path-task")
    artifacts = cast(list[dict[str, object]], packet["proposed_artifacts"])
    artifacts[0]["path"] = "/home/alba/atticus-harness/matters/napier/03-working/draft-notes/path-task.md"

    parsed = parse_result(packet)

    assert parsed.proposed_artifacts[0]["path"] == "candidate/draft-notes/path-task.md"


def test_result_packet_v2_rejects_arbitrary_absolute_artifact_path():
    packet = v2_packet("path-task")
    artifacts = cast(list[dict[str, object]], packet["proposed_artifacts"])
    artifacts[0]["path"] = "/etc/passwd"

    with pytest.raises(ResultPacketError, match="relative safe path"):
        _ = parse_result(packet)


def test_result_packet_v2_rejects_placeholder_redacted_draft_content():
    packet = v2_packet("redacted-draft-task")
    artifacts = cast(list[dict[str, object]], packet["proposed_artifacts"])
    artifacts[0]["artifact_type"] = "redacted_draft"
    artifacts[0]["content"] = "# Redacted Draft\n\n[Remaining complaint content unchanged]"

    with pytest.raises(ResultPacketError, match="complete replacement text"):
        _ = parse_result(packet)


def test_result_packet_v2_rejects_supported_law_without_authority_citation():
    packet = v2_packet("law-task", citation_target_id="SRC-0001")
    findings = cast(list[dict[str, object]], packet["findings"])
    findings[0]["finding_type"] = "law"

    with pytest.raises(ResultPacketError, match="supported law findings require"):
        _ = parse_result(packet)


def test_result_packet_v2_accepts_supported_law_with_authority_citation():
    packet = v2_packet("law-task")
    packet["citations"] = [
        {
            "citation_id": "cite-authority-1",
            "target_type": "authority",
            "target_id": "auth-1",
            "locator": "s.1",
        }
    ]
    findings = cast(list[dict[str, object]], packet["findings"])
    findings[0].update(
        {
            "finding_type": "law",
            "citation_ids": ["cite-authority-1"],
            "reasoning_status": "supported",
        }
    )

    parsed = parse_result(packet)

    assert parsed.findings[0]["finding_type"] == "law"


def test_citation_audit_certification_is_withheld_when_audit_reports_defects():
    packet = v2_packet("citation-audit", citation_target_id="SRC-0001")
    packet["risk_flags"] = [{"risk_id": "risk-1", "description": "Fabricated citation risk"}]
    artifacts = cast(list[dict[str, object]], packet["proposed_artifacts"])
    artifacts[0]["content"] = "# Citation Audit\n\n### Overall Result: FAIL"

    blocker = _certification_blocker(certification_type="citation_audit", packet=packet)

    assert blocker == "citation audit found defects requiring repair before certification"


def test_privacy_certification_is_withheld_when_redaction_flags_remain():
    packet = v2_packet("privacy-audit", citation_target_id="SRC-0001")
    packet["redaction_flags"] = [{"flag_id": "redact-1", "description": "Student ID remains"}]
    artifacts = cast(list[dict[str, object]], packet["proposed_artifacts"])
    artifacts[0]["content"] = "# Privacy Audit\n\n## Audit Result: FAIL"

    blocker = _certification_blocker(certification_type="privacy_redaction_audit", packet=packet)

    assert blocker == "privacy audit found redaction defects requiring repair before certification"


def test_redaction_review_task_type_maps_to_privacy_certification_gate():
    assert MATTER_CERTIFICATIONS_BY_TASK_TYPE["redaction_review"] == "privacy_redaction_audit"
    assert MATTER_CERTIFICATIONS_BY_TASK_TYPE["redaction_verification"] == "privacy_redaction_audit"


def test_record_worker_result_stores_normalized_proposed_artifact_path(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="normalize-path-task", title="Normalize path", task_type="extract", matter_scope="alpha"))
        lease_id = acquire_lease(conn, task_id="normalize-path-task", worker_id="worker-1")
        packet = v2_packet("normalize-path-task")
        artifacts = cast(list[dict[str, object]], packet["proposed_artifacts"])
        artifacts[0]["path"] = "/candidate/normalize-path-task/output.md"

        candidate_id = record_worker_result(
            conn,
            task_id="normalize-path-task",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=packet,
        )
        row = cast(Mapping[str, object], conn.execute("SELECT payload_json FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    stored = json.loads(str(row["payload_json"]))
    assert stored["proposed_artifacts"][0]["path"] == "candidate/normalize-path-task/output.md"


def test_result_packet_v2_rejects_memory_only_proof_for_material_findings():
    packet = v2_packet("packet-task")
    packet["citations"] = [
        {
            "citation_id": "cite-memory-1",
            "target_type": "memory",
            "target_id": "mem-1",
            "locator": "memory",
        }
    ]
    findings = cast(list[dict[str, object]], packet["findings"])
    findings[0].update(
        {
            "finding_type": "fact",
            "citation_ids": ["cite-memory-1"],
            "reasoning_status": "supported",
        }
    )

    with pytest.raises(ResultPacketError, match="orientation only"):
        _ = parse_result(packet)


def test_record_worker_result_rejects_citation_outside_task_context(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        repo.add_task(conn, TaskSpec(task_id="alpha-task", title="Alpha task", task_type="extract", matter_scope="alpha"))
        lease_id = acquire_lease(conn, task_id="alpha-task", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="alpha-task",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=v2_packet("alpha-task", citation_target_id=source_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        lease = cast(Mapping[str, object], conn.execute("SELECT status FROM leases WHERE lease_id = ?", (lease_id,)).fetchone())

    assert candidate["status"] == "quarantined"
    assert "outside work order context" in str(candidate["quarantined_reason"])
    assert lease["status"] == "failed"


def test_record_worker_result_accepts_v2_citations_inside_task_context(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="alpha-task",
                title="Alpha task",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        lease_id = acquire_lease(conn, task_id="alpha-task", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="alpha-task",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=v2_packet("alpha-task", citation_target_id=source_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        task = cast(Mapping[str, object], conn.execute("SELECT status FROM tasks WHERE task_id = 'alpha-task'").fetchone())

    assert candidate["status"] == "candidate"
    assert task["status"] == "reducer_pending"


def test_record_worker_result_allows_source_citations_from_task_dependency_artifact_graph(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        repo.add_task(conn, TaskSpec(task_id="parent-task", title="Parent task", task_type="extract", matter_scope="alpha"))
        _ = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/parent/evidence-map.md",
            artifact_type="evidence_map",
            title="Parent evidence map",
            content="Parent cites source",
            source_ids=[source_id],
            produced_by_task_id="parent-task",
        )
        repo.update_task_status(conn, "parent-task", TaskStatus.COMPLETE, "parent artifact available")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="child-task",
                title="Child task",
                task_type="evidence_gathering",
                matter_scope="alpha",
                task_dependencies=["parent-task"],
            ),
        )
        lease_id = acquire_lease(conn, task_id="child-task", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="child-task",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=v2_packet("child-task", citation_target_id=source_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "candidate"
    assert candidate["quarantined_reason"] == ""


def test_record_worker_result_allows_citations_from_task_dependency_artifact_closure(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        registry_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="canonical/registry.json",
            artifact_type="evidence_registry",
            title="Evidence registry",
            content="Registry cites source",
            trust_status=TrustStatus.VALIDATED,
            source_ids=[source_id],
        )
        repo.add_task(conn, TaskSpec(task_id="draft-task", title="Draft task", task_type="draft_preparation", matter_scope="alpha"))
        _ = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/draft.md",
            artifact_type="draft_complaint",
            title="Draft",
            content="Draft uses registry",
            trust_status=TrustStatus.VALIDATED,
            artifact_dependency_ids=[registry_id],
            produced_by_task_id="draft-task",
        )
        repo.update_task_status(conn, "draft-task", TaskStatus.COMPLETE, "draft artifact available")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-audit",
                title="Citation audit",
                task_type="citation_audit",
                matter_scope="alpha",
                task_dependencies=["draft-task"],
            ),
        )
        lease_id = acquire_lease(conn, task_id="citation-audit", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="citation-audit",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=artifact_citation_packet("citation-audit", registry_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "candidate"
    assert candidate["quarantined_reason"] == ""


def test_review_task_can_cite_dependency_draft_as_review_proof(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="draft-task", title="Draft task", task_type="draft_preparation", matter_scope="alpha"))
        draft_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/draft.md",
            artifact_type="draft_complaint",
            title="Draft",
            content="Draft contains an unsupported claim",
            trust_status=TrustStatus.VALIDATED,
            produced_by_task_id="draft-task",
        )
        repo.update_task_status(conn, "draft-task", TaskStatus.COMPLETE, "draft artifact available")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-audit",
                title="Citation audit",
                task_type="citation_audit",
                matter_scope="alpha",
                task_dependencies=["draft-task"],
            ),
        )
        lease_id = acquire_lease(conn, task_id="citation-audit", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="citation-audit",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=artifact_citation_packet("citation-audit", draft_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "candidate"
    assert candidate["quarantined_reason"] == ""


def test_citation_fix_task_can_cite_dependency_draft_as_repair_proof(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="draft-task", title="Draft task", task_type="draft_preparation", matter_scope="alpha"))
        draft_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/draft.md",
            artifact_type="draft_complaint",
            title="Draft",
            content="Draft contains a fabricated citation",
            trust_status=TrustStatus.VALIDATED,
            produced_by_task_id="draft-task",
        )
        repo.update_task_status(conn, "draft-task", TaskStatus.COMPLETE, "draft artifact available")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="citation-fix",
                title="Citation fix",
                task_type="citation_fix",
                matter_scope="alpha",
                task_dependencies=["draft-task"],
            ),
        )
        lease_id = acquire_lease(conn, task_id="citation-fix", worker_id="worker-1")
        packet = artifact_citation_packet("citation-fix", draft_id)
        findings = cast(list[dict[str, object]], packet["findings"])
        findings[0]["finding_type"] = "contradiction"
        candidate_id = record_worker_result(
            conn,
            task_id="citation-fix",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=packet,
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "candidate"
    assert candidate["quarantined_reason"] == ""


def test_redaction_implementation_task_can_cite_dependency_draft_as_repair_proof(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="draft-task", title="Draft task", task_type="draft_preparation", matter_scope="alpha"))
        draft_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/draft.md",
            artifact_type="draft_complaint",
            title="Draft",
            content="Draft contains unredacted personal data",
            trust_status=TrustStatus.VALIDATED,
            produced_by_task_id="draft-task",
        )
        repo.update_task_status(conn, "draft-task", TaskStatus.COMPLETE, "draft artifact available")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="redaction-implementation",
                title="Redaction implementation",
                task_type="redaction_implementation",
                matter_scope="alpha",
                task_dependencies=["draft-task"],
            ),
        )
        lease_id = acquire_lease(conn, task_id="redaction-implementation", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="redaction-implementation",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=artifact_citation_packet("redaction-implementation", draft_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "candidate"
    assert candidate["quarantined_reason"] == ""


def test_redaction_application_task_can_cite_dependency_draft_as_repair_proof(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="draft-task", title="Draft task", task_type="draft_preparation", matter_scope="alpha"))
        draft_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/draft.md",
            artifact_type="draft_complaint",
            title="Draft",
            content="Draft contains unredacted personal data",
            trust_status=TrustStatus.VALIDATED,
            produced_by_task_id="draft-task",
        )
        repo.update_task_status(conn, "draft-task", TaskStatus.COMPLETE, "draft artifact available")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="redaction-application",
                title="Redaction application",
                task_type="redaction_application",
                matter_scope="alpha",
                task_dependencies=["draft-task"],
            ),
        )
        lease_id = acquire_lease(conn, task_id="redaction-application", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="redaction-application",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=artifact_citation_packet("redaction-application", draft_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "candidate"
    assert candidate["quarantined_reason"] == ""


def test_redaction_application_task_can_cite_redaction_annotation_as_repair_proof(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="review-task", title="Review task", task_type="redaction_review", matter_scope="alpha"))
        annotation_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/redaction-annotations.md",
            artifact_type="redaction_annotation",
            title="Redaction annotations",
            content="Redact personal identifiers",
            trust_status=TrustStatus.VALIDATED,
            produced_by_task_id="review-task",
        )
        repo.update_task_status(conn, "review-task", TaskStatus.COMPLETE, "redaction annotations available")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="redaction-application",
                title="Redaction application",
                task_type="redaction_application",
                matter_scope="alpha",
                task_dependencies=["review-task"],
            ),
        )
        lease_id = acquire_lease(conn, task_id="redaction-application", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="redaction-application",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=artifact_citation_packet("redaction-application", annotation_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "candidate"
    assert candidate["quarantined_reason"] == ""


def test_redaction_fix_task_can_cite_dependency_draft_and_annotation_as_repair_proof(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="review-task", title="Review task", task_type="redaction_review", matter_scope="alpha"))
        annotation_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/redaction-annotations.md",
            artifact_type="redaction_annotation",
            title="Redaction annotations",
            content="Use [Matriculation Number] for the student identifier placeholder",
            trust_status=TrustStatus.VALIDATED,
            produced_by_task_id="review-task",
        )
        repo.update_task_status(conn, "review-task", TaskStatus.COMPLETE, "redaction annotations available")
        repo.add_task(conn, TaskSpec(task_id="draft-task", title="Draft task", task_type="redaction_application", matter_scope="alpha"))
        draft_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/redacted-draft.md",
            artifact_type="redacted_draft",
            title="Redacted draft",
            content="Draft still contains an imprecise [REDACTED] placeholder",
            trust_status=TrustStatus.VALIDATED,
            artifact_dependency_ids=[annotation_id],
            produced_by_task_id="draft-task",
        )
        repo.update_task_status(conn, "draft-task", TaskStatus.COMPLETE, "redacted draft available")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="redaction-fix",
                title="Redaction fix",
                task_type="redaction_fix",
                matter_scope="alpha",
                task_dependencies=["draft-task"],
            ),
        )
        lease_id = acquire_lease(conn, task_id="redaction-fix", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="redaction-fix",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=artifact_citation_packet("redaction-fix", draft_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "candidate"
    assert candidate["quarantined_reason"] == ""


def test_non_review_task_cannot_use_draft_artifact_as_fact_proof(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        draft_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="candidate/draft.md",
            artifact_type="draft_complaint",
            title="Draft",
            content="Draft contains an unsupported claim",
            trust_status=TrustStatus.VALIDATED,
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="evidence-task",
                title="Evidence task",
                task_type="evidence_gathering",
                matter_scope="alpha",
                artifact_dependencies=[draft_id],
            ),
        )
        lease_id = acquire_lease(conn, task_id="evidence-task", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="evidence-task",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=artifact_citation_packet("evidence-task", draft_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "quarantined"
    assert "orientation only" in str(candidate["quarantined_reason"])


def test_extracted_artifact_citation_quarantine_names_source_citation_repair(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        artifact_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/03-working/extracted-text/source.txt",
            artifact_type="extracted_text",
            title="source extracted",
            content="extracted source text",
            source_ids=[source_id],
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="alpha-artifact-citation",
                title="Alpha artifact citation",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        lease_id = acquire_lease(conn, task_id="alpha-artifact-citation", worker_id="worker-1")
        candidate_id = record_worker_result(
            conn,
            task_id="alpha-artifact-citation",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=artifact_citation_packet("alpha-artifact-citation", artifact_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        event = cast(
            Mapping[str, object],
            conn.execute(
                """
                SELECT event_type, payload_json
                FROM orchestrator_events
                WHERE matter_scope = 'alpha'
                ORDER BY created_at DESC
                LIMIT 1
                """
            ).fetchone(),
        )

    assert candidate["status"] == "quarantined"
    assert f"Cite source:{source_id}" in str(candidate["quarantined_reason"])
    assert event["event_type"] == "orchestrator.worker_failed"
    assert "worker output quarantined" in str(event["payload_json"])


def test_record_worker_result_rejects_legacy_cross_matter_source_dependency(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="beta-legacy-cross-source",
                title="Beta legacy cross source",
                task_type="extract",
                matter_scope="beta",
            ),
        )
        lease_id = acquire_lease(conn, task_id="beta-legacy-cross-source", worker_id="worker-1")
        _ = conn.execute(
            "UPDATE tasks SET source_dependencies_json = ? WHERE task_id = ?",
            (f'["{source_id}"]', "beta-legacy-cross-source"),
        )
        candidate_id = record_worker_result(
            conn,
            task_id="beta-legacy-cross-source",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=v2_packet("beta-legacy-cross-source", citation_target_id=source_id),
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())

    assert candidate["status"] == "quarantined"
    assert "outside work order context" in str(candidate["quarantined_reason"])


def test_record_worker_result_scopes_validation_result_citations_to_task_matter(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        beta_validation = repo.record_validation(conn, target_type="matter", target_id="beta", gate_name="foundation", passed=True)
        alpha_validation = repo.record_validation(conn, target_type="matter", target_id="alpha", gate_name="foundation", passed=True)
        repo.add_task(conn, TaskSpec(task_id="alpha-bad-validation", title="Alpha bad validation", task_type="extract", matter_scope="alpha"))
        bad_lease = acquire_lease(conn, task_id="alpha-bad-validation", worker_id="worker-1")
        bad_candidate_id = record_worker_result(
            conn,
            task_id="alpha-bad-validation",
            lease_id=bad_lease,
            worker_id="worker-1",
            payload=validation_result_packet("alpha-bad-validation", beta_validation),
        )
        repo.add_task(conn, TaskSpec(task_id="alpha-good-validation", title="Alpha good validation", task_type="extract", matter_scope="alpha"))
        good_lease = acquire_lease(conn, task_id="alpha-good-validation", worker_id="worker-1")
        good_candidate_id = record_worker_result(
            conn,
            task_id="alpha-good-validation",
            lease_id=good_lease,
            worker_id="worker-1",
            payload=validation_result_packet("alpha-good-validation", alpha_validation),
        )
        bad_candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (bad_candidate_id,)).fetchone())
        good_candidate = cast(Mapping[str, object], conn.execute("SELECT status FROM candidate_outputs WHERE candidate_id = ?", (good_candidate_id,)).fetchone())
        validation_matters = {
            int(row["validation_result_id"]): str(row["matter_scope"])
            for row in conn.execute("SELECT validation_result_id, matter_scope FROM validation_results").fetchall()
        }

    assert bad_candidate["status"] == "quarantined"
    assert "outside work order context" in str(bad_candidate["quarantined_reason"])
    assert good_candidate["status"] == "candidate"
    assert validation_matters[beta_validation] == "beta"
    assert validation_matters[alpha_validation] == "alpha"


def test_external_action_request_is_blocked_and_quarantined(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="unsafe-action", title="Unsafe action", task_type="draft"))
        lease_id = acquire_lease(conn, task_id="unsafe-action", worker_id="worker-1")
        packet = v2_packet("unsafe-action")
        packet["external_action_requests"] = [{"action_type": "email", "recipient": "other@example.test"}]
        candidate_id = record_worker_result(
            conn,
            task_id="unsafe-action",
            lease_id=lease_id,
            worker_id="worker-1",
            payload=packet,
        )
        candidate = cast(Mapping[str, object], conn.execute("SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
        blocks = _count(conn, "SELECT COUNT(*) FROM external_action_blocks")

    assert candidate["status"] == "quarantined"
    assert "external action requests are blocked" in str(candidate["quarantined_reason"])
    assert blocks == 1
