"""Local stub adapter for safe harness runtime tests.

The local adapter is intentionally not a legal worker and not a provider-backed
model. It is a deterministic harness exercising adapter used to prove the
schedule -> lease -> work-order -> candidate path without spending money or
launching OpenClaw.
"""

from __future__ import annotations

from typing import Any

from atticus.workers.contracts import safe_path_component


class LocalStubAdapter:
    name = "local_stub"

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            raise ValueError("local_stub requires a task_id")
        task_component = safe_path_component(task_id)

        source_dependencies = list(payload.get("source_dependencies") or [])
        artifact_dependencies = list(payload.get("artifact_dependencies") or [])
        citations = [
            {"target_type": "source", "target_id": source_id, "locator": "work_order.source_dependencies"}
            for source_id in source_dependencies
        ] + [
            {"target_type": "artifact", "target_id": artifact_id, "locator": "work_order.artifact_dependencies"}
            for artifact_id in artifact_dependencies
        ]

        return {
            "task_id": task_id,
            "summary": f"Local stub completed bounded work order for {task_id}.",
            "findings": [
                {
                    "text": "Harness runtime executed a local-only candidate generation path.",
                    "citation_ids": [],
                }
            ],
            "citations": citations,
            "proposed_artifacts": [
                {
                    "path": f"candidate/{task_component}/local_stub_result.json",
                    "artifact_type": "local_stub_result",
                    "stage": str(payload.get("stage") or ""),
                    "title": str(payload.get("title") or f"Local stub result for {task_id}"),
                }
            ],
            "proposed_tasks": [],
        }
