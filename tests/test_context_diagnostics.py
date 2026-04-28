from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import cast
import json

import pytest

from atticus.cli import main as cli_main
from atticus.context.diagnostics import build_context_diagnostics
from atticus.context.packs import build_context_pack
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def test_context_pack_sections_have_auditable_v2_metadata(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/source.pdf", sha256="a" * 64)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="ctx-v2",
                title="Context v2",
                task_type="extract",
                instructions="Extract only the bounded issue list and preserve uncertainty.",
                matter_scope="alpha",
                source_dependencies=[source_id],
            ),
        )
        pack = build_context_pack(conn, task_id="ctx-v2", persist=False)

    assert pack.sections
    for section in pack.sections:
        assert {"name", "kind", "priority", "cache_scope", "estimated_tokens", "fingerprint", "inclusion_reason"} <= set(section)
    schema_section = next(section for section in pack.sections if section["name"] == "required_output_schema")
    schema_content = cast(Mapping[str, object], schema_section["content"])
    assert schema_content["schema_version"] == RESULT_PACKET_SCHEMA_VERSION
    stable = next(section for section in pack.sections if section["name"] == "stable_prefix")
    assert "candidate, not canonical" in str(stable["content"])
    assert "Facts, law, procedure, inference, risk, contradiction, and uncertainty" in str(stable["content"])
    assert "finding_taxonomy" in schema_content
    task_contract = next(section for section in pack.sections if section["name"] == "task_contract")
    task_content = cast(Mapping[str, object], task_contract["content"])
    assert "preserve uncertainty" in str(task_content["instructions"])


def test_context_diagnostics_reports_stale_dependencies_and_counts(tmp_path: Path):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        source_id = repo.add_source(conn, matter_scope="alpha", path="/alpha/stale.pdf", sha256="a" * 64, stale=True)
        artifact_id = repo.add_artifact(conn, matter_scope="alpha", path="/alpha/draft.md", artifact_type="draft", content="draft", stale=True)
        repo.add_task(
            conn,
            TaskSpec(
                task_id="ctx-diag",
                title="Context diagnostics",
                task_type="extract",
                matter_scope="alpha",
                source_dependencies=[source_id],
                artifact_dependencies=[artifact_id],
                validation_gates=["stale_dependency"],
            ),
        )
        diagnostics = build_context_diagnostics(conn, task_id="ctx-diag")

    assert diagnostics["task_id"] == "ctx-diag"
    assert diagnostics["matter_scope"] == "alpha"
    assert diagnostics["result_schema_version"] == RESULT_PACKET_SCHEMA_VERSION
    assert diagnostics["source_count"] == 1
    assert diagnostics["artifact_count"] == 1
    assert diagnostics["stale_sources"] == [source_id]
    assert diagnostics["stale_artifacts"] == [artifact_id]
    assert diagnostics["validation_gates"] == ["stale_dependency"]
    assert diagnostics["sections"]


def test_context_cli_json_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    with repo.db_connection(db_path) as conn:
        repo.add_task(conn, TaskSpec(task_id="ctx-cli", title="Context CLI", task_type="extract"))

    assert cli_main(["context", "--db", str(db_path), "--task-id", "ctx-cli", "--json"]) == 0
    output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    with repo.db_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM context_packs").fetchone()
        assert row is not None
        context_count = row["n"]

    assert output["task_id"] == "ctx-cli"
    assert output["diagnostic_only"] is True
    assert context_count == 0
