from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import json
import sqlite3
from typing import cast

import pytest

from atticus.cli import main as cli_main
from atticus.core.policies import TrustStatus
from atticus.db import repo
from atticus.memory.types import LEGAL_MEMORY_TYPES
from atticus.status.inspect import inspect_record
from atticus.tools import registry as tool_registry
from atticus.tools.base import BaseTool, ToolContext, ToolMetadata, ToolPermissionError, ToolValidationError
from atticus.tools.registry import get_tool, invoke_tool, list_tools


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(str(row[0]))


def test_tools_list_exposes_classified_legal_tools_and_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)

    tools = {tool.name: tool for tool in list_tools()}
    assert tools["ListMatterSources"].read_only is True
    assert tools["InspectRecord"].read_only is True
    assert tools["ReadDraftArtifact"].read_only is True
    assert tools["EditDraftArtifact"].destructive is True
    assert tools["ReduceCandidate"].read_only is False

    assert cli_main(["tools", "list", "--db", str(db_path), "--json"]) == 0
    output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    listed = {str(item["name"]): item for item in cast(list[Mapping[str, object]], output["tools"])}
    assert listed["ListMatterSources"]["read_only"] is True
    assert listed["EditDraftArtifact"]["requires_write"] is True


def test_read_only_tool_invocation_is_matter_scoped_and_audited(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        alpha_source = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        _ = repo.add_source(conn, matter_scope="beta", path="/beta/source.pdf", sha256="b" * 64)
        ctx = ToolContext(conn=conn, matter_scope="alpha", actor="test")
        result = invoke_tool("ListMatterSources", {}, ctx)
        event_count = _count(conn, "SELECT COUNT(*) FROM events WHERE event_type = 'tool.invoked'")

    assert [row["source_id"] for row in cast(list[dict[str, object]], result["sources"])] == [alpha_source]
    assert event_count == 1


def test_source_tools_surface_ocr_derivative_without_promoting_it_to_evidence(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, source_id="SRC-OCR", matter_scope="alpha", path="/alpha/scan.pdf", sha256="a" * 64)
        artifact_id = repo.add_artifact(
            conn,
            artifact_id="art-extracted-SRC-OCR",
            matter_scope="alpha",
            path="/alpha/03-working/extracted-text/SRC-OCR.txt",
            artifact_type="extracted_text",
            trust_status=TrustStatus.CANDIDATE,
            sha256="b" * 64,
            title="SRC-OCR extracted text",
            content="OCR text for the scan",
            source_ids=[source_id],
        )
        _ = conn.execute(
            """
            INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method,
              coverage_status, confidence, metadata_json, created_at)
            VALUES ('extract-ocr', ?, ?, 'existing_ocr_text', 'complete', 0.75, ?, 'now')
            """,
            (
                source_id,
                artifact_id,
                json.dumps(
                    {
                        "extracted_by": "atticus.local_extraction",
                        "extractor_tool": "existing_text",
                        "source_path": "/alpha/scan.pdf",
                        "output_path": "/alpha/03-working/extracted-text/SRC-OCR.txt",
                        "text_sha256": "b" * 64,
                    }
                ),
            ),
        )
        _ = conn.execute(
            """
            INSERT INTO ocr_records(ocr_id, source_id, artifact_id, engine,
              page_count, coverage_status, metadata_json, created_at)
            VALUES ('ocr-1', ?, ?, 'existing_text', 1, 'complete', ?, 'now')
            """,
            (source_id, artifact_id, json.dumps({"extracted_by": "atticus.local_extraction", "extractor_tool": "existing_text"})),
        )
        ctx = ToolContext(conn=conn, matter_scope="alpha", actor="test")
        listed = invoke_tool("ListMatterSources", {}, ctx)
        inspected = invoke_tool("InspectRecord", {"record_type": "source", "record_id": source_id}, ctx)
    cli_inspected = inspect_record(str(db_path), record_type="source", record_id=source_id)

    listed_source = cast(list[dict[str, object]], listed["sources"])[0]
    derivative = cast(list[dict[str, object]], listed_source["source_material_derivatives"])[0]
    inspect_derivative = cast(list[dict[str, object]], cast(dict[str, object], inspected["record"])["source_material_derivatives"])[0]
    cli_derivative = cast(list[dict[str, object]], cli_inspected["source_material_derivatives"])[0]
    assert listed_source["source_material_available"] is True
    assert listed_source["ocr_available"] is True
    assert derivative["artifact_id"] == artifact_id
    assert derivative["derivative_role"] == "ocr_text"
    assert derivative["evidence_role"] == "source_attached_text_derivative_not_independent_evidence"
    assert derivative["citation_target"] == {"target_type": "source", "target_id": source_id}
    assert derivative["ocr"]["engine"] == "existing_text"
    assert inspect_derivative["path"].endswith("SRC-OCR.txt")
    assert cli_derivative["artifact_id"] == artifact_id


def test_draft_artifact_edit_requires_prior_read_hash_and_versions(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        artifact_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/draft.md",
            artifact_type="draft",
            trust_status=TrustStatus.CANDIDATE,
            content="hello world",
        )
        ctx = ToolContext(conn=conn, matter_scope="alpha", actor="draft-worker")
        with pytest.raises(ToolValidationError, match="read before editing"):
            _ = invoke_tool(
                "EditDraftArtifact",
                {"artifact_id": artifact_id, "old": "hello", "new": "hullo", "read_hash": "x"},
                ctx,
            )
        read = invoke_tool("ReadDraftArtifact", {"artifact_id": artifact_id}, ctx)
        edit = invoke_tool(
            "EditDraftArtifact",
            {
                "artifact_id": artifact_id,
                "old": "hello",
                "new": "hullo",
                "read_hash": read["content_hash"],
            },
            ctx,
        )
        artifact = cast(Mapping[str, object], conn.execute("SELECT content FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone())
        versions = _count(conn, "SELECT COUNT(*) FROM artifact_versions WHERE artifact_id = ?", (artifact_id,))
        edit_events = _count(conn, "SELECT COUNT(*) FROM events WHERE event_type = 'artifact.draft_edited'")

    assert artifact["content"] == "hullo world"
    assert edit["replacements"] == 1
    assert versions == 2
    assert edit_events == 1


def test_draft_artifact_edit_blocks_stale_hash_and_validated_artifacts(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        draft_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/draft.md",
            artifact_type="draft",
            trust_status=TrustStatus.CANDIDATE,
            content="one two",
        )
        validated_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/final.md",
            artifact_type="draft",
            trust_status=TrustStatus.VALIDATED,
            content="final",
        )
        ctx = ToolContext(conn=conn, matter_scope="alpha", actor="draft-worker")
        read = invoke_tool("ReadDraftArtifact", {"artifact_id": draft_id}, ctx)
        _ = conn.execute("UPDATE artifacts SET content = 'changed' WHERE artifact_id = ?", (draft_id,))

        with pytest.raises(ToolValidationError, match="changed since read"):
            _ = invoke_tool(
                "EditDraftArtifact",
                {"artifact_id": draft_id, "old": "one", "new": "two", "read_hash": read["content_hash"]},
                ctx,
            )
        with pytest.raises(ToolPermissionError, match="validated"):
            _ = invoke_tool("ReadDraftArtifact", {"artifact_id": validated_id}, ctx)


def test_draft_artifact_edit_rejects_multiple_matches_unless_replace_all(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        artifact_id = repo.add_artifact(
            conn,
            matter_scope="alpha",
            path="/alpha/draft.md",
            artifact_type="draft",
            trust_status=TrustStatus.CANDIDATE,
            content="rent rent arrears",
        )
        ctx = ToolContext(conn=conn, matter_scope="alpha", actor="draft-worker")
        read = invoke_tool("ReadDraftArtifact", {"artifact_id": artifact_id}, ctx)

        with pytest.raises(ToolValidationError, match="exactly once"):
            _ = invoke_tool(
                "EditDraftArtifact",
                {"artifact_id": artifact_id, "old": "rent", "new": "fee", "read_hash": read["content_hash"]},
                ctx,
            )

        edit = invoke_tool(
            "EditDraftArtifact",
            {"artifact_id": artifact_id, "old": "rent", "new": "fee", "read_hash": read["content_hash"], "replace_all": True},
            ctx,
        )
        artifact = cast(Mapping[str, object], conn.execute("SELECT content FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone())

    assert edit["replacements"] == 2
    assert artifact["content"] == "fee fee arrears"


def test_mutating_tool_invocation_requires_write_permission_and_logs_denial(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        ctx = ToolContext(conn=conn, matter_scope="alpha", actor="operator", permission_mode="read_only")

        with pytest.raises(ToolPermissionError, match="requires write permission"):
            _ = invoke_tool(
                "WriteDraftArtifact",
                {"path": "/alpha/draft.md", "artifact_type": "draft", "title": "Draft", "content": "draft"},
                ctx,
            )

        artifacts = _count(conn, "SELECT COUNT(*) FROM artifacts")
        event = cast(Mapping[str, object], conn.execute("SELECT payload_json FROM events WHERE event_type = 'tool.invoked'").fetchone())
        payload = cast(Mapping[str, object], json.loads(str(event["payload_json"])))

    assert artifacts == 0
    assert payload["tool"] == "WriteDraftArtifact"
    assert payload["status"] == "blocked"
    assert payload["permission_mode"] == "read_only"


def test_tool_result_size_limit_is_enforced_and_audited(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    class OversizedTool(BaseTool):
        metadata = ToolMetadata(
            name="OversizedTool",
            description="Return too much output.",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            read_only=True,
            max_result_size=32,
        )

        def call(self, input_data: Mapping[str, object], ctx: ToolContext) -> dict[str, object]:
            del input_data, ctx
            return {"blob": "x" * 100}

    monkeypatch.setattr(tool_registry, "get_tool", lambda name: OversizedTool())
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        ctx = ToolContext(conn=conn, matter_scope="alpha", actor="operator")

        with pytest.raises(ToolValidationError, match="max_result_size"):
            _ = invoke_tool("OversizedTool", {}, ctx)

        event = cast(Mapping[str, object], conn.execute("SELECT payload_json FROM events WHERE event_type = 'tool.invoked'").fetchone())
        payload = cast(Mapping[str, object], json.loads(str(event["payload_json"])))

    assert payload["tool"] == "OversizedTool"
    assert payload["status"] == "failed"
    assert "max_result_size" in str(payload["error"])


def test_legal_memory_requires_sources_for_evidence_and_stays_matter_scoped(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    assert "evidence_fact" in LEGAL_MEMORY_TYPES
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        with pytest.raises(ValueError, match="source_refs"):
            _ = repo.add_legal_memory(
                conn,
                matter_scope="alpha",
                memory_type="evidence_fact",
                name="Uncited fact",
                description="No source refs",
                content="This must not become memory.",
                confidence=0.5,
                source_refs=[],
            )
        memory_id = repo.add_legal_memory(
            conn,
            matter_scope="alpha",
            memory_type="evidence_fact",
            name="Cited fact",
            description="Has source refs",
            content="This is cited.",
            confidence=0.7,
            source_refs=[{"target_type": "source", "target_id": source_id, "locator": "p.1"}],
        )
        _ = repo.add_legal_memory(
            conn,
            matter_scope="beta",
            memory_type="drafting_preference",
            name="Beta style",
            description="Other matter",
            content="Use short sentences.",
            confidence=1.0,
            source_refs=[],
        )

    assert cli_main(["memory", "list", "--db", str(db_path), "--matter", "alpha"]) == 0
    output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    memories = cast(list[Mapping[str, object]], output["memories"])
    assert [item["memory_id"] for item in memories] == [memory_id]
