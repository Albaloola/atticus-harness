from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atticus.tools.registry import HarnessTool, ToolContext, ToolResult, register_tool


@register_tool
class NotebookEditTool(HarnessTool):
    """Make multiple structured edits in a batch (e.g., bulk renames, recategorisations)."""

    @property
    def name(self) -> str:
        return "NotebookEdit"

    @property
    def description(self) -> str:
        return "Make multiple structured edits in a batch (e.g., bulk renames, recategorisations)."

    def can_handle(self, stage: str) -> bool:
        """Check if tool is available in the given stage.

        Args:
            stage: The harness stage to check.

        Returns:
            True for resolve stage only.
        """
        return "resolve" in stage.lower()

    def invoke(self, params: dict, context: ToolContext) -> ToolResult:
        """Execute batch edits on notebook records.

        Args:
            params: Tool parameters including:
                - edits (list[dict], required): List of edit operations, each with:
                    - type (str): Edit type - "rename", "recategorise", or "update_field"
                    - target (str): Target identifier (e.g., source_id)
                    - **kwargs: Type-specific fields (e.g., new_name for rename)
                - notebook_path (str, optional): Path to notebook JSON file.
            context: Tool execution context.

        Returns:
            ToolResult with edit operation results.
        """
        edits = params.get("edits")
        if not edits or not isinstance(edits, list):
            return ToolResult(
                content={"edits_applied": 0, "results": []},
                metadata={"edit_count": 0, "success_count": 0},
                success=False,
                error="edits parameter is required and must be a list",
            )

        notebook_path = params.get("notebook_path")
        if notebook_path:
            path = Path(notebook_path).resolve()
        else:
            path = context.workspace_path / "notebook.json"

        try:
            with open(path, "r", encoding="utf-8") as f:
                notebook_data = json.load(f)
        except FileNotFoundError:
            return ToolResult(
                content={"edits_applied": 0, "results": []},
                metadata={"edit_count": len(edits), "success_count": 0},
                success=False,
                error=f"Notebook file not found: {path}",
            )
        except json.JSONDecodeError as e:
            return ToolResult(
                content={"edits_applied": 0, "results": []},
                metadata={"edit_count": len(edits), "success_count": 0},
                success=False,
                error=f"Invalid JSON in notebook file: {e}",
            )

        if not isinstance(notebook_data, list):
            notebook_data = [notebook_data]

        results = []
        success_count = 0

        for i, edit in enumerate(edits):
            edit_type = edit.get("type")
            target = edit.get("target")

            if not edit_type or not target:
                results.append({
                    "edit_index": i,
                    "success": False,
                    "error": "Edit missing type or target",
                })
                continue

            record = None
            for item in notebook_data:
                if isinstance(item, dict) and (
                    item.get("id") == target or item.get("source_id") == target
                ):
                    record = item
                    break

            if record is None:
                results.append({
                    "edit_index": i,
                    "success": False,
                    "error": f"Target record not found: {target}",
                })
                continue

            try:
                if edit_type == "rename":
                    result = self._apply_rename(record, edit, i)
                elif edit_type == "recategorise":
                    result = self._apply_recategorise(record, edit, i)
                elif edit_type == "update_field":
                    result = self._apply_update_field(record, edit, i)
                else:
                    result = {
                        "edit_index": i,
                        "success": False,
                        "error": f"Unknown edit type: {edit_type}",
                    }

                if result.get("success"):
                    success_count += 1
                results.append(result)

            except Exception as e:
                results.append({
                    "edit_index": i,
                    "success": False,
                    "error": str(e),
                })

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(notebook_data, f, indent=2)
        except Exception as e:
            return ToolResult(
                content={"edits_applied": success_count, "results": results},
                metadata={"edit_count": len(edits), "success_count": success_count},
                success=False,
                error=f"Failed to write notebook: {e}",
            )

        if context.provenance_logger is not None:
            context.provenance_logger.log(
                "notebook_edit",
                {
                    "edits_applied": success_count,
                    "total_edits": len(edits),
                    "notebook_path": str(path),
                },
            )

        all_succeeded = success_count == len(edits)
        error_msg = None
        if not all_succeeded:
            failed = [r for r in results if not r.get("success")]
            if failed:
                error_msg = failed[0].get("error", "Some edits failed")

        return ToolResult(
            content={"edits_applied": success_count, "results": results},
            metadata={"edit_count": len(edits), "success_count": success_count},
            success=all_succeeded,
            error=error_msg,
        )

    def _apply_rename(self, record: dict, edit: dict, index: int) -> dict:
        """Apply a rename edit to a record.

        Args:
            record: The record to modify.
            edit: The edit operation.
            index: The edit index for tracking.

        Returns:
            Result dictionary.
        """
        new_name = edit.get("new_name")
        if new_name is None:
            return {
                "edit_index": index,
                "success": False,
                "error": "rename edit requires new_name",
            }

        record["name"] = new_name
        return {
            "edit_index": index,
            "success": True,
            "message": f"Renamed to {new_name}",
        }

    def _apply_recategorise(self, record: dict, edit: dict, index: int) -> dict:
        """Apply a recategorise edit to a record.

        Args:
            record: The record to modify.
            edit: The edit operation.
            index: The edit index for tracking.

        Returns:
            Result dictionary.
        """
        new_category = edit.get("new_category")
        if new_category is None:
            return {
                "edit_index": index,
                "success": False,
                "error": "recategorise edit requires new_category",
            }

        record["category"] = new_category
        return {
            "edit_index": index,
            "success": True,
            "message": f"Recategorised to {new_category}",
        }

    def _apply_update_field(self, record: dict, edit: dict, index: int) -> dict:
        """Apply an update_field edit to a record.

        Args:
            record: The record to modify.
            edit: The edit operation.
            index: The edit index for tracking.

        Returns:
            Result dictionary.
        """
        field_name = edit.get("field")
        field_value = edit.get("value")

        if field_name is None:
            return {
                "edit_index": index,
                "success": False,
                "error": "update_field edit requires field",
            }

        record[field_name] = field_value
        return {
            "edit_index": index,
            "success": True,
            "message": f"Updated field {field_name}",
        }
