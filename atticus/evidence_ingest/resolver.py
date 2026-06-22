"""Evidence resolution module.

Produces AI-driven resolution plan from analysis results.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atticus.tools.registry import ToolContext


def resolve_analysis_results(
    workspace: Path,
    context: ToolContext,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Generate resolution plan from analysis results.

    Args:
        workspace: Workspace root path.
        context: ToolContext for tool invocations.
        provider: AI provider name (optional).
        model: AI model name (optional).

    Returns:
        Dictionary with resolution plan.
    """
    from atticus.evidence_ingest.provenance import ProvenanceLogger
    from atticus.evidence_ingest.prompts import RESOLVE_SYSTEM_PROMPT
    from atticus.evidence_ingest.normaliser import normalise_analysis_result

    provenance = ProvenanceLogger(workspace, context)

    # Load analysis results
    analysis_path = workspace / "02-registers" / "analysis_results.json"
    if not analysis_path.exists():
        raise FileNotFoundError(f"Analysis results not found: {analysis_path}")

    import json

    with open(analysis_path, "r", encoding="utf-8") as f:
        analysis_data = json.load(f)

    analyses = analysis_data.get("analyses", [])

    # Normalise all analyses
    normalised_analyses: list[dict[str, Any]] = []
    all_warnings: list[str] = []

    for analysis in analyses:
        normalised, warnings = normalise_analysis_result(analysis)
        normalised_analyses.append(normalised)
        all_warnings.extend(warnings)

    # Build resolution plan
    # In a real implementation, this would call the AI provider
    # For now, produce a basic resolution plan
    sources: list[dict[str, Any]] = []

    for i, analysis in enumerate(normalised_analyses):
        source_id = f"NAP-SRC-{i:04d}"
        category = analysis.get("suggested_category", "other").lower()
        human_readable = analysis.get("human_readable_name", "Unknown")
        # Construct stored_path with category prefix (lowercased for normalisation)
        safe_filename = human_readable.replace("/", "-").replace("\\", "-")
        ext = Path(analysis.get("file", "unknown")).suffix or ".pdf"
        stored_path = f"{category}/{source_id} - {safe_filename}{ext}".lower()

        source = {
            "source_id": source_id,
            "original_path": analysis.get("file", ""),
            "stored_path": stored_path,
            "sha256": analysis.get("sha256", ""),
            "document_type": analysis.get("document_type", "other"),
            "category": category,
            "human_readable_name": human_readable,
            "description": analysis.get("description", ""),
            "duplicate_of": None,
            "part_of_series": None,
        }
        sources.append(source)

    # SHA-256 based duplicate detection
    sha_map: dict[str, str] = {}  # sha256 -> source_id (keeper)
    for source in sources:
        sha = source.get("sha256", "")
        if not sha:
            continue
        if sha in sha_map:
            source["duplicate_of"] = sha_map[sha]
        else:
            sha_map[sha] = source["source_id"]

    resolution_plan = {
        "sources": sources,
        "duplicate_groups": [],
        "truncation_groups": [],
        "recategorisations": [],
        "renames": [],
        "needs_human_review": [],
        "normalisation_warnings": all_warnings,
    }

    # Write resolution plan
    output_path = workspace / "02-registers" / "resolution_plan.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(resolution_plan, f, indent=2, default=str)

    provenance.log("resolve", source_count=len(sources), warnings=len(all_warnings))

    return resolution_plan


def save_resolution_plan(workspace: Path, resolution_plan: dict[str, Any]) -> Path:
    """Save resolution plan to workspace.

    Args:
        workspace: Workspace root path.
        resolution_plan: Resolution plan dictionary to save.

    Returns:
        Path to the saved resolution plan file.
    """
    import json

    output_path = workspace / "02-registers" / "resolution_plan.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(resolution_plan, f, indent=2, default=str)

    return output_path


def load_resolution_plan(workspace: Path) -> dict[str, Any]:
    """Load resolution plan from workspace.

    Args:
        workspace: Workspace root path.

    Returns:
        Resolution plan dictionary.

    Raises:
        FileNotFoundError: If resolution plan file doesn't exist.
    """
    import json

    input_path = workspace / "02-registers" / "resolution_plan.json"
    if not input_path.exists():
        raise FileNotFoundError(f"Resolution plan not found: {input_path}")

    with open(input_path, "r", encoding="utf-8") as f:
        return json.load(f)
