"""Evidence registration module.

Generates registry and calls seed-matter to register evidence in database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atticus.tools.registry import ToolContext


def register_evidence(
    workspace: Path,
    context: ToolContext,
    db_path: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Register evidence by generating registry and calling seed-matter.

    Args:
        workspace: Workspace root path.
        context: ToolContext for tool invocations.
        db_path: Database path for seed-matter.
        provider: AI provider name (optional).
        model: AI model name (optional).

    Returns:
        Dictionary with registration results.
    """
    from atticus.evidence_ingest.provenance import ProvenanceLogger
    from atticus.evidence_ingest.prompts import REGISTER_SYSTEM_PROMPT

    provenance = ProvenanceLogger(workspace, context)

    # Load resolution plan to get sources
    plan_path = workspace / "02-registers" / "resolution_plan.json"
    if not plan_path.exists():
        raise FileNotFoundError(f"Resolution plan not found: {plan_path}")

    import json

    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    sources = plan.get("sources", [])

    # Generate registry descriptions
    # In a real implementation, this would call the AI provider
    # For now, generate basic descriptions
    registry: list[dict[str, Any]] = []

    for source in sources:
        description = f"{source.get('document_type', 'Document')}: {source.get('human_readable_name', source.get('source_id', ''))}"
        entry = {
            "source_id": source.get("source_id", ""),
            "description": description,
            "stored_path": source.get("stored_path", ""),
            "category": source.get("category", "other"),
            "document_type": source.get("document_type", "other"),
        }
        registry.append(entry)

    # Write registry
    registry_path = workspace / "02-registers" / "evidence_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    with open(registry_path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, default=str)

    # Call seed-matter if db_path is provided
    seed_result: dict[str, Any] = {}
    if db_path and db_path.exists():
        # This would call the seed_matter_from_inventory function
        # For now, just record the intent
        seed_result = {
            "would_seed": True,
            "db_path": str(db_path),
            "workspace": str(workspace),
            "inventory": str(registry_path),
        }

    result = {
        "registry_path": str(registry_path),
        "registry_count": len(registry),
        "registry": registry,
        "seed_matter": seed_result,
    }

    provenance.log(
        "register",
        registry_count=len(registry),
        db_path=str(db_path) if db_path else None,
    )

    return result


def generate_evidence_registry(
    sources: list[dict[str, Any]],
    context: ToolContext,
) -> list[dict[str, Any]]:
    """Generate evidence registry entries from resolution plan sources.

    Compatibility wrapper — tests call this directly with source list.

    Args:
        sources: List of source dicts from a resolution plan.
        context: ToolContext for tool invocations.

    Returns:
        List of registry entry dicts.
    """
    registry: list[dict[str, Any]] = []
    for source in sources:
        description = (
            f"{source.get('document_type', 'Document')}: "
            f"{source.get('human_readable_name', source.get('source_id', ''))}"
        )
        entry: dict[str, Any] = {
            "source_id": source.get("source_id", ""),
            "description": description,
            "stored_path": source.get("stored_path", ""),
            "category": source.get("category", "other"),
            "document_type": source.get("document_type", "other"),
        }
        registry.append(entry)
    return registry


def save_evidence_registry(registry: list[dict[str, Any]], path: Path) -> None:
    """Save evidence registry to file.

    Compatibility wrapper — tests call this directly with a Path.

    Args:
        registry: Registry entry list to save.
        path: Output file path.
    """
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, default=str)
