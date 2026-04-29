"""Worker and candidate-output contracts.

Workers are bounded proposers. They may write task-local candidate packets, but
never canonical legal memory. Reducers are the only canonical writers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re



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
    required_certifications: list[dict[str, object]] = field(default_factory=list)
    validation_gates: list[str] = field(default_factory=list)
    provider_policy: dict[str, object] = field(default_factory=dict)
    model_decision: dict[str, object] = field(default_factory=dict)
    model_decision_reason: str = ""
    skills: list[dict[str, object]] = field(default_factory=list)
    context_pack: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
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
            "model_decision": self.model_decision,
            "model_decision_reason": self.model_decision_reason,
            "skills": self.skills,
            "context_pack": self.context_pack,
        }


REQUIRED_RESULT_PACKET_KEYS = {
    "schema_version",
    "task_id",
    "summary",
    "findings",
    "citations",
    "proposed_artifacts",
    "proposed_tasks",
    "uncertainties",
    "contradictions",
    "risk_flags",
    "redaction_flags",
    "external_action_requests",
}
_SAFE_COMPONENT_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def safe_path_component(value: str) -> str:
    """Return a deterministic single path component for task-local files."""

    component = _SAFE_COMPONENT_RE.sub("_", value.strip()).strip("._-")
    return component or "task"
