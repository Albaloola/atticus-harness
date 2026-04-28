"""Task primitives used by the scheduler and tests."""

from __future__ import annotations

from dataclasses import dataclass, field

from atticus.core.policies import LegalStage, TaskStatus


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    title: str
    task_type: str
    matter_scope: str = "atticus"
    stage: LegalStage = LegalStage.S0_SOURCE_INVENTORY
    status: TaskStatus = TaskStatus.QUEUED
    source_dependencies: list[str] = field(default_factory=list)
    artifact_dependencies: list[str] = field(default_factory=list)
    task_dependencies: list[str] = field(default_factory=list)
    matter_dependencies: list[str] = field(default_factory=list)
    required_certifications: list[dict[str, object]] = field(default_factory=list)
    validation_gates: list[str] = field(default_factory=list)
    staleness_rules: dict[str, object] = field(default_factory=dict)
    provider_policy: dict[str, object] = field(default_factory=dict)
    cost_limit_usd: float | None = None
    expected_value: float = 0.0
