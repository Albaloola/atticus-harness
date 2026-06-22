"""Evidence execution module.

Executes approved resolution plan to copy, rename, and organise files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from atticus.evidence_ingest.resolver import load_resolution_plan
from atticus.tools.registry import ToolContext


def check_plan_accepted(plan: dict[str, Any]) -> bool:
    """Check if a resolution plan has been accepted.

    Args:
        plan: The resolution plan dictionary.

    Returns:
        True if the plan has an 'accepted_at' timestamp, False otherwise.
    """
    return "accepted_at" in plan or "accepted_at" in plan.get("metadata", {})


def format_filename(filename: str, page_info: dict[str, Any] | None = None) -> str:
    """Format a filename with optional page information.

    Args:
        filename: The original filename.
        page_info: Optional dictionary with 'page' and 'total' keys.

    Returns:
        Formatted filename with page info appended if provided.
    """
    if page_info and "page" in page_info:
        page = page_info["page"]
        total = page_info.get("total", "")
        # Handle different extensions
        for ext in [".pdf", ".jpg", ".jpeg", ".png", ".txt", ".docx", ".eml", ".msg"]:
            if filename.lower().endswith(ext):
                base = filename[: -len(ext)]
                if total:
                    return f"{base}_page_{page}_of_{total}{ext}"
                return f"{base}_page_{page}{ext}"
        # Fallback: append page info
        if total:
            return f"{filename}_page_{page}_of_{total}"
        return f"{filename}_page_{page}"
    return filename


def execute_plan(
    workspace: Path,
    plan_or_context: dict[str, Any] | ToolContext,
    context_or_dry: ToolContext | bool = False,
    dry_run: bool = False,
    **kwargs: Any,
) -> dict[str, Any]:
    """Execute approved resolution plan.

    Args:
        workspace: Workspace root path.
        plan: The approved resolution plan dictionary.
        context: ToolContext for tool invocations.
        dry_run: If True, only simulate execution without making changes.

    Returns:
        Dictionary with execution results.

    Raises:
        RuntimeError: If the plan has not been accepted.
    """
    # Allow context=ToolContext as keyword argument alias for context_or_dry
    if "context" in kwargs and context_or_dry is False:
        context_or_dry = kwargs["context"]

    # Old API: execute_plan(workspace, context, dry_run=True)  — no plan arg
    # New API: execute_plan(workspace, plan, context, dry_run=True)
    if isinstance(plan_or_context, ToolContext):
        context = plan_or_context
        dry_run = bool(context_or_dry) or dry_run
        plan = load_resolution_plan(workspace)
    else:
        plan = plan_or_context
        context = context_or_dry if isinstance(context_or_dry, ToolContext) else None

    if not check_plan_accepted(plan):
        raise RuntimeError("Resolution plan has not been accepted. Accept the plan first.")

    from atticus.evidence_ingest.provenance import ProvenanceLogger

    provenance = ProvenanceLogger(workspace, context) if context else None

    sources = plan.get("sources", [])
    operations: list[dict[str, Any]] = []
    errors: list[str] = []

    from atticus.tools.copy import CopyTool

    copy_tool = CopyTool()

    for source in sources:
        source_id = source.get("source_id", "unknown")
        original_path = source.get("original_path", "")
        stored_path = source.get("stored_path", "")
        duplicate_of = source.get("duplicate_of")

        if duplicate_of:
            dup_path = workspace / "01-sources" / "__duplicates__" / duplicate_of / Path(stored_path).name
            dup_path.parent.mkdir(parents=True, exist_ok=True)

            if not dry_run and context:
                copy_result = copy_tool.invoke(
                    {"src": original_path, "dst": str(dup_path)},
                    context,
                )
                if not copy_result.success:
                    errors.append(f"Failed to copy duplicate {source_id}: {copy_result.error}")
                    continue

            operations.append({
                "source_id": source_id,
                "action": "moved_duplicate",
                "duplicate_of": duplicate_of,
                "to": str(dup_path),
            })
            continue

        dest_path = workspace / "01-sources" / stored_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if not dry_run and context:
            copy_result = copy_tool.invoke(
                {"src": original_path, "dst": str(dest_path)},
                context,
            )
            if not copy_result.success:
                errors.append(f"Failed to copy {source_id}: {copy_result.error}")
                continue

        operations.append({
            "source_id": source_id,
            "action": "copy" if not dry_run else "would_copy",
            "from": original_path,
            "to": str(dest_path),
        })

    result = {
        "dry_run": dry_run,
        "operations": operations,
        "operation_count": len(operations),
        "errors": errors,
        "error_count": len(errors),
    }

    if provenance:
        provenance.log(
            "execute",
            dry_run=dry_run,
            operation_count=len(operations),
            error_count=len(errors),
        )

    return result


def execute_resolution_plan(
    workspace: Path,
    context: ToolContext,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Execute approved resolution plan.

    Args:
        workspace: Workspace root path.
        context: ToolContext for tool invocations.
        dry_run: If True, only simulate execution without making changes.

    Returns:
        Dictionary with execution results.
    """
    # Check if plan is accepted
    accept_path = workspace / "02-registers" / "plan_accepted.flag"
    if not accept_path.exists():
        raise RuntimeError("Resolution plan has not been accepted. Run 'plan accept' first.")

    # Load resolution plan
    plan_path = workspace / "02-registers" / "resolution_plan.json"
    if not plan_path.exists():
        raise FileNotFoundError(f"Resolution plan not found: {plan_path}")

    import json

    with open(plan_path, "r", encoding="utf-8") as f:
        plan = json.load(f)

    # Use the shared execute_plan logic
    return execute_plan(workspace, plan, context, dry_run)
