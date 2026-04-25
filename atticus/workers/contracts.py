"""Worker and candidate-output contracts.

Workers are bounded proposers. They may write task-local candidate packets, but
never canonical legal memory. Reducers are the only canonical writers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkerEnvelope:
    task_id: str
    adapter: str
    task_local_output_dir: str
    lease_id: str | None = None
    context_pack_id: str | None = None
    dry_run: bool = True


@dataclass(frozen=True)
class WorkOrder:
    task_id: str
    title: str
    stage: str
    task_type: str
    matter_scope: str
    lease_id: str | None
    context_pack_id: str | None
    instructions: str
    source_dependencies: list[str] = field(default_factory=list)
    artifact_dependencies: list[str] = field(default_factory=list)
    required_certifications: list[dict[str, Any]] = field(default_factory=list)
    validation_gates: list[str] = field(default_factory=list)
    provider_policy: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "title": self.title,
            "stage": self.stage,
            "task_type": self.task_type,
            "matter_scope": self.matter_scope,
            "lease_id": self.lease_id,
            "context_pack_id": self.context_pack_id,
            "instructions": self.instructions,
            "source_dependencies": self.source_dependencies,
            "artifact_dependencies": self.artifact_dependencies,
            "required_certifications": self.required_certifications,
            "validation_gates": self.validation_gates,
            "provider_policy": self.provider_policy,
        }


REQUIRED_RESULT_PACKET_KEYS = {"task_id", "summary", "findings", "citations", "proposed_artifacts"}
