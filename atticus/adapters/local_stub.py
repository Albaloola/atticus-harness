"""Local stub adapter for safe harness runtime tests.

The local adapter is intentionally not a legal worker and not a provider-backed
model. It is a deterministic harness exercising adapter used to prove the
schedule -> lease -> work-order -> candidate path without spending money or
launching OpenClaw.
"""

from __future__ import annotations

from typing import cast

from atticus.workers.contracts import safe_path_component
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


class LocalStubAdapter:
    name: str = "local_stub"

    def run(self, payload: dict[str, object]) -> dict[str, object]:
        task_id = str(payload.get("task_id") or "")
        if not task_id:
            raise ValueError("local_stub requires a task_id")
        task_component = safe_path_component(task_id)

        source_dependencies_raw = payload.get("source_dependencies")
        source_dependencies = [str(item) for item in cast(list[object], source_dependencies_raw)] if isinstance(source_dependencies_raw, list) else []
        artifact_dependencies_raw = payload.get("artifact_dependencies")
        artifact_dependencies = [str(item) for item in cast(list[object], artifact_dependencies_raw)] if isinstance(artifact_dependencies_raw, list) else []
        citations = [
            {
                "citation_id": f"source-{index + 1}",
                "target_type": "source",
                "target_id": source_id,
                "locator": "work_order.source_dependencies",
                "quoted_text_hash": "",
            }
            for index, source_id in enumerate(source_dependencies)
        ] + [
            {
                "citation_id": f"artifact-{index + 1}",
                "target_type": "artifact",
                "target_id": artifact_id,
                "locator": "work_order.artifact_dependencies",
                "quoted_text_hash": "",
            }
            for index, artifact_id in enumerate(artifact_dependencies)
        ]
        citation_ids = [str(citation["citation_id"]) for citation in citations]

        return {
            "schema_version": RESULT_PACKET_SCHEMA_VERSION,
            "task_id": task_id,
            "summary": f"Local stub completed bounded work order for {task_id}.",
            "findings": [
                {
                    "finding_id": "local-stub-runtime",
                    "text": "Harness runtime executed a local-only candidate generation path.",
                    "finding_type": "drafting_note" if not citation_ids else "fact",
                    "citation_ids": citation_ids,
                    "confidence": 0.0 if not citation_ids else 1.0,
                    "reasoning_status": "uncertain" if not citation_ids else "supported",
                }
            ],
            "citations": citations,
            "proposed_artifacts": [
                {
                    "path": f"candidate/{task_component}/local_stub_result.json",
                    "artifact_type": "local_stub_result",
                    "stage": str(payload.get("stage") or ""),
                    "title": str(payload.get("title") or f"Local stub result for {task_id}"),
                    "content": "{}",
                }
            ],
            "proposed_tasks": [],
            "uncertainties": [],
            "contradictions": [],
            "risk_flags": [],
            "redaction_flags": [],
            "external_action_requests": [],
        }
