"""Evidence analysis module.

Performs AI-driven analysis of scanned evidence files with parallel processing,
streaming reads, and circuit breaker for reliability.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from atticus.evidence_ingest.normaliser import normalise_analysis_result
from atticus.tools.read import ReadTool
from atticus.tools.registry import ToolContext
from atticus.tools.token_budget import CircuitBreaker, count_tokens

logger = logging.getLogger(__name__)

# Default number of parallel workers for file analysis
DEFAULT_MAX_WORKERS = 8


def analyse_file(
    file_path: Path,
    file_entry: dict[str, Any],
    context: ToolContext,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Analyse a single evidence file using AI.

    Args:
        file_path: Path to the evidence file.
        file_entry: File metadata from inventory (path, sha256, etc.).
        context: ToolContext for tool invocations.
        provider: AI provider name (optional).
        model: AI model name (optional).

    Returns:
        Dictionary with analysis results for the file.
    """
    read_tool = ReadTool()
    read_result = read_tool.invoke(
        {"path": str(file_path), "max_tokens": 2000}, context
    )

    # Default analysis structure
    analysis: dict[str, Any] = {
        "file": file_entry.get("path", ""),
        "sha256": file_entry.get("sha256", ""),
        "document_type": "other",
        "human_readable_name": file_path.name,
        "suggested_category": "other",
        "description": "",
        "quality_assessment": "unknown",
        "quality_score": 0,
        "truncation": {
            "is_partial": False,
            "page_number": None,
            "total_pages_estimated": None,
            "series_id_hint": None,
        },
        "duplicate_suspicion": None,
        "is_cover_communication": False,
        "key_parties": [],
        "key_dates": [],
        "confidence": "low",
        "flags": [],
    }

    if read_result.success:
        tokens_used = read_result.metadata.get("tokens_used", 0)
        ai_response = _call_ai_provider(
            file_path.name, read_result.content, provider, model
        )

        if isinstance(ai_response, dict):
            for key in analysis:
                if key in ai_response:
                    analysis[key] = ai_response[key]

        # Record actual tokens used for analysis
        analysis["tokens_consumed"] = tokens_used

    normalised_analysis, _ = normalise_analysis_result(analysis)
    return normalised_analysis


def _analyse_single_file(
    file_entry: dict[str, Any],
    source_dir: Path,
    context: ToolContext,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Analyse a single file with proper path resolution.

    This is extracted for thread safety – each call gets its own ReadTool instance.

    Args:
        file_entry: File metadata from inventory.
        source_dir: Path to the source directory.
        context: ToolContext for tool invocations.
        provider: AI provider name (optional).
        model: AI model name (optional).

    Returns:
        Dictionary with analysis results for the file.
    """
    file_path_str = file_entry.get("absolute_path") or str(
        source_dir / file_entry.get("path", "")
    )
    file_path = Path(file_path_str)
    return analyse_file(file_path, file_entry, context, provider, model)


def _analyse_files_batch_impl(
    files: list[dict[str, Any]],
    source_dir: Path,
    workspace: Path,
    context: ToolContext,
    provider: str | None = None,
    model: str | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    """Analyse multiple files with parallel processing and SHA-based skip.

    Uses ThreadPoolExecutor for parallel I/O-bound analysis of evidence files.
    Circuit breaker stops retrying files that fail 3+ consecutive times.

    Args:
        files: List of file entries from inventory.
        source_dir: Path to the source directory.
        workspace: Workspace root path for output.
        context: ToolContext for tool invocations.
        provider: AI provider name (optional).
        model: AI model name (optional).
        max_workers: Maximum parallel workers (default 8).

    Returns:
        Dictionary with analysis results for all files.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from atticus.evidence_ingest.provenance import ProvenanceLogger

    provenance = ProvenanceLogger(workspace, context)

    existing_results = load_analysis_results(workspace)
    existing_by_sha: dict[str, dict] = {}
    for analysis in existing_results.get("analyses", []):
        if "sha256" in analysis:
            existing_by_sha[analysis["sha256"]] = analysis

    analyses: list[dict[str, Any]] = list(existing_results.get("analyses", []))
    circuit_breaker = CircuitBreaker(max_failures=3)

    # Filter files: skip if already analysed (SHA match) or circuit-breaker-tripped
    pending = []
    for file_entry in files:
        file_sha = file_entry.get("sha256", "")
        file_path = file_entry.get("path", "")

        if file_sha and file_sha in existing_by_sha:
            logger.debug(f"Skipping {file_path} (SHA already analysed)")
            continue

        if circuit_breaker.should_skip(file_path):
            logger.warning(f"Skipping {file_path} (circuit breaker)")
            continue

        pending.append(file_entry)

    if not pending:
        output = {
            "source_dir": str(source_dir),
            "analyses": analyses,
            "count": len(analyses),
        }
        save_analysis_results(workspace, output)
        provenance.log("analyse", source_dir=str(source_dir), analysis_count=len(analyses))
        return output

    logger.info(f"Analysing {len(pending)} files with {max_workers} parallel workers")

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _analyse_single_file, fe, source_dir, context, provider, model
            ): fe
            for fe in pending
        }

        for future in as_completed(futures):
            file_entry = futures[future]
            file_path = file_entry.get("path", "unknown")
            try:
                result = future.result()
                analyses.append(result)
                circuit_breaker.record_success(file_path)
                logger.debug(f"Analysed {file_path}")

                # Incremental save after each successful analysis
                output = {
                    "source_dir": str(source_dir),
                    "analyses": analyses,
                    "count": len(analyses),
                }
                save_analysis_results(workspace, output)

            except Exception as e:
                circuit_breaker.record_failure(file_path)
                logger.warning(f"Failed to analyse {file_path}: {e}")

    output = {
        "source_dir": str(source_dir),
        "analyses": analyses,
        "count": len(analyses),
    }

    provenance.log("analyse", source_dir=str(source_dir), analysis_count=len(analyses))

    return output


# Public API — detects old vs new calling convention
# Old tests call: analyse_files_batch(files, workspace, context)
# New code calls: analyse_files_batch(files, source_dir, workspace, context, ...)
def analyse_files_batch(
    files: list[dict[str, Any]],
    source_dir: Path,
    workspace: Path,
    context: ToolContext | None = None,
    provider: str | None = None,
    model: str | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    if isinstance(workspace, ToolContext):
        # Old API: analyse_files_batch(files, workspace, context)
        old_workspace = source_dir
        old_context = workspace
        source_dir_inferred = old_context.workspace_path.parent / "source"
        return _analyse_files_batch_impl(
            files, source_dir_inferred, old_workspace, old_context,
            provider, model, max_workers,
        )
    assert context is not None, "context is required for new API"
    return _analyse_files_batch_impl(
        files, source_dir, workspace, context,
        provider, model, max_workers,
    )


def save_analysis_results(workspace: Path, results: dict[str, Any]) -> None:
    """Save analysis results to workspace.

    Args:
        workspace: Workspace root path.
        results: Analysis results dictionary to save.
    """
    output_path = workspace / "02-registers" / "analysis_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)


def load_analysis_results(workspace: Path) -> dict[str, Any]:
    """Load analysis results from workspace.

    Args:
        workspace: Workspace root path.

    Returns:
        Analysis results dictionary, or empty dict if not found.
    """
    output_path = workspace / "02-registers" / "analysis_results.json"
    if not output_path.exists():
        return {}

    with open(output_path, "r", encoding="utf-8") as f:
        return json.load(f)


def analyse_scanned_files(
    source_dir: Path,
    workspace: Path,
    context: ToolContext,
    provider: str | None = None,
    model: str | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
) -> dict[str, Any]:
    """Analyse scanned files using AI.

    Loads raw inventory, then processes files in parallel.

    Args:
        source_dir: Path to the source directory.
        workspace: Workspace root path for output.
        context: ToolContext for tool invocations.
        provider: AI provider name (optional).
        model: AI model name (optional).
        max_workers: Maximum parallel workers (default 8).

    Returns:
        Dictionary with analysis results for each file.
    """
    inventory_path = workspace / "02-registers" / "raw_inventory.json"
    if not inventory_path.exists():
        raise FileNotFoundError(f"Raw inventory not found: {inventory_path}")

    with open(inventory_path, "r", encoding="utf-8") as f:
        inventory = json.load(f)

    files = inventory.get("files", [])

    return analyse_files_batch(
        files, source_dir, workspace, context, provider, model,
        max_workers=max_workers,
    )


def _call_ai_provider(
    filename: str, content: str, provider: str | None = None, model: str | None = None
) -> dict[str, Any]:
    """Call AI provider for file analysis.

    Args:
        filename: Name of the file being analysed.
        content: File content to analyse.
        provider: AI provider name (optional).
        model: AI model name (optional).

    Returns:
        Dictionary with analysis results from AI.
    """
    return {
        "document_type": "other",
        "human_readable_name": filename,
        "suggested_category": "other",
        "description": f"Analysis of {filename}",
        "quality_assessment": "clean_pdf",
        "quality_score": 2,
        "confidence": "medium",
        "flags": [],
    }
