from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast
import hashlib

from atticus.core.policies import LegalStage
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.retrieval.source_chunks import chunk_extracted_artifact
from atticus.validation.gates import run_validation
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION
from atticus.workers.work_order import build_work_order


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_chunk_extracted_artifact_creates_offsets_and_hashes(tmp_path: Path):
    db_path = init_db(tmp_path)
    text = "First paragraph about background.\n\nSecond paragraph about decisive rent arrears evidence."
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.txt", sha256="a" * 64)
        artifact_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/extracted/source.txt",
            artifact_type="extracted_text",
            content=text,
            source_ids=[source_id],
        )
        chunks = chunk_extracted_artifact(
            conn,
            matter_scope="alpha",
            source_id=source_id,
            artifact_id=artifact_id,
            confidence=0.91,
        )
        rows = conn.execute(
            "SELECT source_id, artifact_id, start_offset, end_offset, text_hash, confidence FROM source_chunks WHERE source_id = ?",
            (source_id,),
        ).fetchall()

    assert chunks
    assert len(rows) == len(chunks)
    assert rows[0]["source_id"] == source_id
    assert rows[0]["artifact_id"] == artifact_id
    assert int(str(rows[0]["end_offset"])) > int(str(rows[0]["start_offset"]))
    assert len(str(rows[0]["text_hash"])) == 64
    assert rows[0]["confidence"] == 0.91


def test_context_retrieves_late_relevant_chunk_not_prefix_only(tmp_path: Path):
    db_path = init_db(tmp_path)
    late_clause = "Decisive late clause: university promised a rent freeze before collections."
    long_prefix = "Background filler. " * 900
    text = f"{long_prefix}\n\n{late_clause}"
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/lease.txt", sha256="b" * 64)
        artifact_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/extracted/lease.txt",
            artifact_type="extracted_text",
            content=text,
            source_ids=[source_id],
        )
        _ = conn.execute(
            """
            INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method,
              coverage_status, confidence, metadata_json, created_at)
            VALUES ('extract-late', ?, ?, 'plain_text', 'complete', 0.9, '{}', '2026-04-30T00:00:00+00:00')
            """,
            (source_id, artifact_id),
        )
        _ = chunk_extracted_artifact(conn, matter_scope="alpha", source_id=source_id, artifact_id=artifact_id, extraction_id="extract-late")
        repo.add_task(
            conn,
            TaskSpec(
                task_id="ctx-late-clause",
                title="Find rent freeze clause",
                task_type="citation_audit",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                instructions="Find the decisive late clause about the university rent freeze before collections.",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        order = build_work_order(conn, task_id="ctx-late-clause", persist_context=False)

    sections = cast(list[Mapping[str, object]], cast(Mapping[str, object], order.as_dict()["context_pack"])["sections"])
    materials_section = next(section for section in sections if section["name"] == "source_materials")
    materials = cast(list[Mapping[str, object]], materials_section["content"])
    chunks = cast(list[Mapping[str, object]], materials[0]["selected_source_chunks"])
    assert late_clause in str(materials[0]["content_excerpt"])
    assert chunks
    assert any(late_clause in str(chunk["text"]) for chunk in chunks)


def test_quote_support_uses_source_chunk_text(tmp_path: Path):
    db_path = init_db(tmp_path)
    quote = "Late evidence says collections were paused."
    quote_hash = hashlib.sha256(" ".join(quote.split()).encode("utf-8")).hexdigest()
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.txt", sha256="c" * 64)
        artifact_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/extracted/source.txt",
            artifact_type="extracted_text",
            content="This artifact excerpt does not contain the later quote.",
            source_ids=[source_id],
        )
        _ = conn.execute(
            """
            INSERT INTO source_chunks(chunk_id, matter_scope, source_id, source_snapshot_id, extraction_id,
              artifact_id, page_number, start_offset, end_offset, text_hash, text, confidence, metadata_json, created_at)
            VALUES ('chunk-late-quote', 'alpha', ?, '', 'extract-manual', ?, NULL, 5000, 5040, ?, ?, 0.95, '{}', '2026-04-30T00:00:00+00:00')
            """,
            (source_id, artifact_id, quote_hash, quote),
        )
        repo.add_task(
            conn,
            TaskSpec(
                task_id="support-from-chunk",
                title="Support from chunk",
                task_type="draft",
                stage=LegalStage.S8_DRAFT_PREPARATION,
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        candidate_id = repo.record_candidate_output(
            conn,
            task_id="support-from-chunk",
            lease_id=None,
            worker_id="worker-1",
            output_type="worker_result_packet",
            payload={
                "schema_version": RESULT_PACKET_SCHEMA_VERSION,
                "task_id": "support-from-chunk",
                "summary": "Chunk-supported fact.",
                "findings": [
                    {
                        "finding_id": "finding-1",
                        "text": "Collections were paused.",
                        "finding_type": "fact",
                        "citation_ids": ["cite-1"],
                        "confidence": 0.8,
                        "reasoning_status": "supported",
                    }
                ],
                "citations": [
                    {
                        "citation_id": "cite-1",
                        "target_type": "source",
                        "target_id": source_id,
                        "locator": "chunk-late-quote",
                        "quote": quote,
                        "quoted_text_hash": quote_hash,
                    }
                ],
                "proposed_artifacts": [],
                "proposed_tasks": [],
                "uncertainties": [],
                "contradictions": [],
                "risk_flags": [],
                "redaction_flags": [],
                "external_action_requests": [],
            },
        )
        outcome = run_validation(conn, gate_name="citation_support_integrity", target_type="candidate", target_id=candidate_id)

    assert outcome.passed
    assert cast(list[object], outcome.details["checked_citations"])
