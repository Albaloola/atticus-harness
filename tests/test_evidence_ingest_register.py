from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from atticus.evidence_ingest.register import register_evidence
from atticus.tools.registry import ToolContext


def _create_workspace_with_plan(tmp_path: Path, sources: list[dict[str, Any]] | None = None) -> Path:
    """Create a workspace with a resolution plan for testing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    registers_dir = workspace / "02-registers"
    registers_dir.mkdir()

    if sources is None:
        sources = [
            {
                "source_id": "src-001",
                "human_readable_name": "Contract Agreement",
                "document_type": "contract",
                "stored_path": "stored/contract.pdf",
                "category": "legal",
            },
            {
                "source_id": "src-002",
                "human_readable_name": "Email Correspondence",
                "document_type": "email",
                "stored_path": "stored/email.txt",
                "category": "communication",
            },
        ]

    plan = {"sources": sources}
    plan_path = registers_dir / "resolution_plan.json"
    with open(plan_path, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)

    return workspace


def _create_tool_context(workspace: Path) -> ToolContext:
    """Create a ToolContext for testing."""
    return ToolContext(
        stage="evidence-ingest-register",
        workspace_path=workspace,
    )


def test_register_evidence_returns_dict_with_required_keys(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    assert isinstance(result, dict)
    assert "registry_path" in result
    assert "registry_count" in result
    assert "registry" in result
    assert "seed_matter" in result


def test_register_evidence_generates_registry_list_of_dicts(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    registry = result["registry"]
    assert isinstance(registry, list)
    assert len(registry) == 2
    assert all(isinstance(entry, dict) for entry in registry)


def test_register_evidence_registry_has_required_fields(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    registry = result["registry"]
    required_fields = ["source_id", "description", "stored_path", "category", "document_type"]

    for entry in registry:
        for field in required_fields:
            assert field in entry, f"Missing required field: {field}"


def test_register_evidence_generates_descriptions(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    registry = result["registry"]

    assert registry[0]["description"] == "contract: Contract Agreement"
    assert registry[1]["description"] == "email: Email Correspondence"


def test_register_evidence_uses_source_id_when_name_missing(tmp_path: Path):
    sources = [
        {
            "source_id": "src-no-name",
            "document_type": "pdf",
            "stored_path": "stored/doc.pdf",
            "category": "other",
        },
    ]
    workspace = _create_workspace_with_plan(tmp_path, sources=sources)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    assert result["registry"][0]["description"] == "pdf: src-no-name"


def test_register_evidence_saves_registry_to_json(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    registry_path = Path(result["registry_path"])
    assert registry_path.exists()
    assert registry_path.suffix == ".json"

    with open(registry_path, "r", encoding="utf-8") as f:
        saved_data = json.load(f)

    assert isinstance(saved_data, list)
    assert len(saved_data) == 2


def test_register_evidence_json_has_correct_headers(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    registry_path = Path(result["registry_path"])
    with open(registry_path, "r", encoding="utf-8") as f:
        saved_data = json.load(f)

    expected_keys = {"source_id", "description", "stored_path", "category", "document_type"}
    assert set(saved_data[0].keys()) == expected_keys


def test_register_evidence_roundtrip_save_load(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    registry_path = Path(result["registry_path"])
    with open(registry_path, "r", encoding="utf-8") as f:
        saved_registry = json.load(f)

    assert saved_registry == result["registry"]


def test_register_evidence_with_db_path_includes_seed_result(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    db_path = workspace / "test.db"
    db_path.touch()

    result = register_evidence(workspace, context, db_path=db_path)

    seed_result = result["seed_matter"]
    assert seed_result.get("would_seed") is True
    assert "db_path" in seed_result
    assert "workspace" in seed_result
    assert "inventory" in seed_result


def test_register_evidence_without_db_path_seed_result_empty(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    assert result["seed_matter"] == {}


def test_register_evidence_missing_resolution_plan_raises_error(tmp_path: Path):
    workspace = tmp_path / "empty_workspace"
    workspace.mkdir()

    context = _create_tool_context(workspace)

    with pytest.raises(FileNotFoundError) as exc_info:
        register_evidence(workspace, context)

    assert "Resolution plan not found" in str(exc_info.value)


def test_register_evidence_registry_count_matches(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    assert result["registry_count"] == len(result["registry"])
    assert result["registry_count"] == 2


def test_register_evidence_empty_sources_list(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path, sources=[])
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    assert result["registry_count"] == 0
    assert result["registry"] == []


def test_register_evidence_registry_path_in_workspace(tmp_path: Path):
    workspace = _create_workspace_with_plan(tmp_path)
    context = _create_tool_context(workspace)

    result = register_evidence(workspace, context)

    registry_path = Path(result["registry_path"])
    assert registry_path.parent == workspace / "02-registers"
    assert registry_path.name == "evidence_registry.json"
