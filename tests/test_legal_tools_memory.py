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
from atticus.tools.base import ToolContext, ToolPermissionError, ToolValidationError
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
