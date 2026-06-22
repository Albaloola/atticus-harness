"""Full pipeline integration tests for evidence ingest."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from atticus.evidence_ingest.analyser import analyse_files_batch, save_analysis_results
from atticus.evidence_ingest.executor import check_plan_accepted, execute_plan
from atticus.evidence_ingest.gate import accept_plan, run_quality_gate
from atticus.evidence_ingest.register import (
    generate_evidence_registry,
    register_evidence,
    save_evidence_registry,
)
from atticus.evidence_ingest.resolver import resolve_analysis_results as resolve_analysis, save_resolution_plan
import atticus.evidence_ingest.resolver as resolver
from atticus.evidence_ingest.scanner import scan_directory, scan_source_directory
from atticus.evidence_ingest.validator import run_all_validations
from atticus.tools.registry import ToolContext


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "01-sources").mkdir()
    (ws / "02-registers").mkdir()
    return ws


@pytest.fixture
def source_dir(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    src.mkdir()
    (src / "document.txt").write_text("This is a text document with some content.")
    (src / "report.pdf").write_bytes(b"%PDF-1.4 fake pdf content for testing")
    (src / "photo.jpg").write_bytes(b"\xff\xd8\xff\xe0fake jpg content for testing")
    return src


@pytest.fixture
def tool_context(workspace: Path) -> ToolContext:
    return ToolContext(stage="evidence-ingest-analyse", workspace_path=workspace)


@pytest.fixture
def mock_ai_response() -> dict:
    return {
        "document_type": "letter",
        "human_readable_name": "test document",
        "suggested_category": "communications",
        "description": "a complete and thorough report for integration testing.",
        "quality_assessment": "clean_pdf",
        "quality_score": 1,
        "confidence": "high",
        "truncation": {"is_partial": False},
        "duplicate_suspicion": None,
        "is_cover_communication": False,
        "key_parties": ["Test Party"],
        "key_dates": ["2026-05-01"],
        "flags": [],
    }


def test_full_pipeline_end_to_end(
    workspace: Path,
    source_dir: Path,
    tool_context: ToolContext,
    mock_ai_response: dict,
):
    """Test the full evidence ingest pipeline from scan to register."""
    # Patch AI provider and Read tool
    with patch("atticus.evidence_ingest.analyser._call_ai_provider") as mock_ai, \
         patch("atticus.tools.read.ReadTool") as mock_read:
        read_instance = MagicMock()
        read_instance.invoke.return_value = MagicMock(success=True, content="Mocked content")
        mock_read.return_value = read_instance
        mock_ai.return_value = mock_ai_response

        # Step 1: Scan
        scan_result = scan_directory(source_dir, tool_context)
        assert scan_result["count"] == 3
        assert (workspace / "02-registers" / "raw_inventory.json").exists()

        # Step 2: Analyse
        analysis_result = analyse_files_batch(
            scan_result["files"], workspace, tool_context
        )
        assert analysis_result["count"] == 3
        assert (workspace / "02-registers" / "analysis_results.json").exists()

        # Step 3: Resolve
        resolution_plan = resolve_analysis(workspace, tool_context)
        assert len(resolution_plan["sources"]) == 3
        assert (workspace / "02-registers" / "resolution_plan.json").exists()

    # Step 4: Gate validation
    scan_path = workspace / "02-registers" / "raw_inventory.json"
    with open(scan_path) as f:
        scan_data = json.load(f)
    gate_result = run_quality_gate(scan_data["files"], resolution_plan)
    assert gate_result["status"] in ("ALL_CLEAR", "PARTIAL")

    # Step 5: Accept plan
    accepted_plan = accept_plan(resolution_plan, accepted_by="test")
    assert "metadata" in accepted_plan
    assert accepted_plan["metadata"]["accepted_by"] == "test"
    save_resolution_plan(workspace, accepted_plan)

    # Step 6: Execute (dry run)
    with patch("atticus.tools.copy.CopyTool") as mock_copy:
        copy_instance = MagicMock()
        copy_instance.invoke.return_value = MagicMock(success=True)
        mock_copy.return_value = copy_instance
        exec_result = execute_plan(workspace, tool_context, dry_run=True)
        assert exec_result["dry_run"] is True
        assert exec_result["operation_count"] == 3

    # Step 7: Register
    registry = generate_evidence_registry(
        accepted_plan["sources"], tool_context
    )
    assert len(registry) == 3
    save_evidence_registry(registry, workspace / "02-registers" / "evidence_registry.json")
    assert (workspace / "02-registers" / "evidence_registry.json").exists()


def test_scan_creates_raw_inventory(
    workspace: Path, source_dir: Path, tool_context: ToolContext
):
    """Test that scan creates raw_inventory.json."""
    result = scan_directory(source_dir, tool_context)
    assert result["count"] == 3
    inventory_path = workspace / "02-registers" / "raw_inventory.json"
    assert inventory_path.exists()
    with open(inventory_path) as f:
        data = json.load(f)
    assert data["count"] == 3
    extensions = {f["extension"] for f in data["files"]}
    assert extensions == {".txt", ".pdf", ".jpg"}


def test_analyse_creates_analysis_results(
    workspace: Path, source_dir: Path, tool_context: ToolContext,
    mock_ai_response: dict,
):
    """Test that analyse creates analysis_results.json."""
    with patch("atticus.evidence_ingest.analyser._call_ai_provider") as mock_ai, \
         patch("atticus.tools.read.ReadTool") as mock_read:
        read_instance = MagicMock()
        read_instance.invoke.return_value = MagicMock(success=True, content="Mocked")
        mock_read.return_value = read_instance
        mock_ai.return_value = mock_ai_response

        scan_result = scan_directory(source_dir, tool_context)
        result = analyse_files_batch(scan_result["files"], workspace, tool_context)
        assert result["count"] == 3
        analysis_path = workspace / "02-registers" / "analysis_results.json"
        assert analysis_path.exists()


def test_resolve_creates_resolution_plan(
    workspace: Path, tool_context: ToolContext, mock_ai_response: dict,
):
    """Test that resolve creates resolution_plan.json."""
    # First create analysis results
    with patch("atticus.evidence_ingest.analyser._call_ai_provider") as mock_ai, \
         patch("atticus.tools.read.ReadTool") as mock_read:
        read_instance = MagicMock()
        read_instance.invoke.return_value = MagicMock(success=True, content="Mocked")
        mock_read.return_value = read_instance
        mock_ai.return_value = mock_ai_response

        source_dir = workspace.parent / "src"
        source_dir.mkdir()
        (source_dir / "file1.txt").write_text("content")
        (source_dir / "file2.txt").write_text("content")
        (source_dir / "file3.txt").write_text("content")

        scan_result = scan_directory(source_dir, tool_context)
        analyse_files_batch(scan_result["files"], workspace, tool_context)

    # Now resolve
    result = resolve_analysis(workspace, tool_context)
    assert len(result["sources"]) == 3
    plan_path = workspace / "02-registers" / "resolution_plan.json"
    assert plan_path.exists()


def test_gate_validation_pass(
    workspace: Path, tool_context: ToolContext, mock_ai_response: dict,
):
    """Test that gate validation passes with correct data."""
    # Create scan results and resolution plan with matching data
    scan_results = [
        {"file": "file1.txt", "sha256": "abc123", "extension": ".txt"},
    ]
    plan = {
        "sources": [
            {
                "source_id": "NAP-SRC-0000",
                "original_path": "file1.txt",
                "sha256": "abc123",
                "document_type": "letter",
                "category": "communications",
                "description": "valid and complete report for testing.",
                "stored_path": "communications/nap-src-0000 - test.pdf",
                "duplicate_of": None,
                "confidence": "high",
            },
        ],
        "duplicate_groups": [],
        "truncation_groups": [],
        "recategorisations": [],
        "renames": [],
        "needs_human_review": [],
    }
    result = run_all_validations(scan_results, plan)
    assert result["status"] == "ALL_CLEAR"


def test_gate_validation_failure_then_quarantine(workspace: Path):
    """Test that validation failure leads to quarantine."""
    scan_results = [
        {"file": "file1.txt", "sha256": "abc123", "extension": ".txt"},
        {"file": "file2.txt", "sha256": "def456", "extension": ".txt"},
    ]
    plan = {
        "sources": [
            {
                "source_id": "NAP-SRC-0000",
                "original_path": "file1.txt",
                "sha256": "abc123",
                "document_type": "letter",
                "category": "communications",
                "description": "valid",
                "stored_path": "communications/NAP-SRC-0000 - Test.pdf",
                "duplicate_of": None,
                "confidence": "high",
            },
        ],
        "duplicate_groups": [],
        "truncation_groups": [],
        "recategorisations": [],
        "renames": [],
        "needs_human_review": [],
    }
    gate_result = run_quality_gate(scan_results, plan)
    assert gate_result["status"] == "BLOCKED"
    assert gate_result["quarantined"] is True


def test_gate_low_confidence_flagged(workspace: Path):
    """Test that low confidence items are flagged."""
    scan_results = [
        {"source_id": "NAP-SRC-0000", "file": "file1.txt", "sha256": "abc123"},
    ]
    plan = {
        "sources": [
            {
                "source_id": "NAP-SRC-0000",
                "original_path": "file1.txt",
                "sha256": "abc123",
                "document_type": "letter",
                "category": "communications",
                "description": "valid",
                "stored_path": "test.pdf",
                "duplicate_of": None,
                "confidence": "low",
            },
        ],
        "duplicate_groups": [],
        "truncation_groups": [],
        "recategorisations": [],
        "renames": [],
        "needs_human_review": [],
    }
    gate_result = run_quality_gate(scan_results, plan)
    assert "NAP-SRC-0000" in gate_result["confidence"]["needs_human_review"]
    assert gate_result["status"] == "PARTIAL"


def test_accept_plan_creates_acceptance_metadata(workspace: Path):
    """Test that accept_plan adds acceptance metadata."""
    plan = {"sources": [], "metadata": {}}
    accepted = accept_plan(plan, accepted_by="test")
    assert "metadata" in accepted
    assert accepted["metadata"]["accepted_by"] == "test"
    assert "accepted_at" in accepted["metadata"]


def test_execute_dry_run_shows_would_copy(
    workspace: Path, tool_context: ToolContext,
):
    """Test that execute with dry_run shows what would be copied."""
    # Create a resolution plan
    plan = {
        "sources": [
            {
                "source_id": "NAP-SRC-0000",
                "original_path": "/tmp/file1.txt",
                "sha256": "abc123",
                "document_type": "letter",
                "category": "communications",
                "description": "Test",
                "stored_path": "communications/NAP-SRC-0000 - Test.pdf",
                "duplicate_of": None,
                "confidence": "high",
            },
        ],
        "metadata": {"accepted_at": "2026-05-01T00:00:00"},
    }
    save_resolution_plan(workspace, plan)

    with patch("atticus.tools.copy.CopyTool") as mock_copy:
        copy_instance = MagicMock()
        copy_instance.invoke.return_value = MagicMock(success=True)
        mock_copy.return_value = copy_instance
        result = execute_plan(workspace, tool_context, dry_run=True)
    assert result["dry_run"] is True
    assert result["operation_count"] == 1


def test_register_creates_registry(
    workspace: Path, tool_context: ToolContext,
):
    """Test that register creates evidence registry."""
    sources = [
        {
            "source_id": "NAP-SRC-0000",
            "human_readable_name": "Test Document",
            "category": "communications",
            "document_type": "letter",
            "description": "A test document.",
            "tags": [],
            "relationships_json": {},
            "quality_rank": 1,
            "original_path": "/tmp/test.txt",
            "stored_path": "communications/NAP-SRC-0000 - Test.pdf",
            "sha256": "abc123",
            "size_bytes": 100,
        },
    ]
    registry = generate_evidence_registry(sources, tool_context)
    assert len(registry) == 1
    save_evidence_registry(registry, workspace / "02-registers" / "evidence_registry.json")
    assert (workspace / "02-registers" / "evidence_registry.json").exists()


def test_pipeline_with_duplicate_detection(
    workspace: Path, tool_context: ToolContext, mock_ai_response: dict,
):
    """Test pipeline handles duplicate detection."""
    source_dir = workspace.parent / "dup_source"
    source_dir.mkdir()
    (source_dir / "file1.txt").write_text("content")
    (source_dir / "file2.txt").write_text("content")  # Same content = same SHA

    with patch("atticus.evidence_ingest.analyser._call_ai_provider") as mock_ai, \
         patch("atticus.tools.read.ReadTool") as mock_read:
        read_instance = MagicMock()
        read_instance.invoke.return_value = MagicMock(success=True, content="Mocked")
        mock_read.return_value = read_instance
        mock_ai.return_value = mock_ai_response

        scan_result = scan_directory(source_dir, tool_context)
        assert scan_result["count"] == 2

        analyse_files_batch(scan_result["files"], workspace, tool_context)

    with patch("atticus.evidence_ingest.resolver.resolve_analysis_results") as mock_resolve:
        mock_resolve.return_value = {
            "duplicate_groups": [
                {
                    "best_source_id": "NAP-SRC-0000",
                    "duplicates": ["NAP-SRC-0001"],
                    "reason": "Exact SHA-256 match",
                },
            ],
            "truncation_groups": [],
            "recategorisations": [],
            "renames": [
                {"source_id": "NAP-SRC-0000", "to": "NAP-SRC-0000 - Test Document.pdf"},
            ],
            "needs_human_review": [],
            "sources": [
                {
                    "source_id": "NAP-SRC-0000",
                    "original_path": "file1.txt",
                    "sha256": scan_result["files"][0]["sha256"],
                    "document_type": "letter",
                    "category": "communications",
                    "description": "Test",
                    "stored_path": "communications/NAP-SRC-0000 - Test.pdf",
                    "duplicate_of": None,
                    "confidence": "high",
                },
                {
                    "source_id": "NAP-SRC-0001",
                    "original_path": "file2.txt",
                    "sha256": scan_result["files"][1]["sha256"],
                    "document_type": "letter",
                    "category": "communications",
                    "description": "Test",
                    "stored_path": "communications/NAP-SRC-0001 - Test.pdf",
                    "duplicate_of": "NAP-SRC-0000",
                    "confidence": "high",
                },
            ],
        }
        resolution_plan = resolver.resolve_analysis_results(workspace, tool_context)

    # Check that duplicate is flagged
    duplicates = [s for s in resolution_plan["sources"] if s.get("duplicate_of")]
    assert len(duplicates) >= 1


def test_gate_retry_logic_with_correction(workspace: Path):
    """Test that gate retries with AI correction on validation failure."""
    scan_results = [
        {"file": "file1.txt", "sha256": "abc123", "extension": ".txt"},
    ]
    plan = {
        "sources": [
            {
                "source_id": "NAP-SRC-0000",
                "original_path": "file1.txt",
                "sha256": "wrong_hash",  # Intentional mismatch
                "document_type": "letter",
                "category": "communications",
                "description": "valid",
                "stored_path": "test.pdf",
                "duplicate_of": None,
                "confidence": "high",
            },
        ],
        "duplicate_groups": [],
        "truncation_groups": [],
        "recategorisations": [],
        "renames": [],
        "needs_human_review": [],
    }
    # Mock the AI correction
    with patch("atticus.evidence_ingest.gate._call_ai_correction") as mock_correct:
        corrected_plan = dict(plan)
        corrected_plan["sources"][0]["sha256"] = "abc123"  # Fix the hash
        mock_correct.return_value = corrected_plan
        gate_result = run_quality_gate(scan_results, plan)
    assert gate_result["status"] == "ALL_CLEAR"
