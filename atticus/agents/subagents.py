"""Candidate-only subagent task specification and validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import sqlite3

from atticus.core.policies import LegalStage, TaskStatus
from atticus.core.tasks import TaskSpec
from atticus.db import repo
from atticus.providers.deepseek import is_held_openrouter_model
from atticus.providers.model_decision import ModelDecision


@dataclass(frozen=True)
class SubagentSpec:
    role: str
    task_type: str
    matter_scope: str
    parent_task_id: str
    model_decision: ModelDecision
    allowed_source_ids: tuple[str, ...]
    allowed_artifact_ids: tuple[str, ...]
    tools: tuple[str, ...]
    max_turns: int
    async_allowed: bool
    cache_sharing_group_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "task_type": self.task_type,
            "matter_scope": self.matter_scope,
            "parent_task_id": self.parent_task_id,
            "model_decision": self.model_decision.__dict__,
            "allowed_source_ids": list(self.allowed_source_ids),
            "allowed_artifact_ids": list(self.allowed_artifact_ids),
            "tools": list(self.tools),
            "max_turns": self.max_turns,
            "async_allowed": self.async_allowed,
            "cache_sharing_group_id": self.cache_sharing_group_id,
        }


FORBIDDEN_SUBAGENT_TOOLS = {"canonical_write", "external_action", "send_email", "file_document", "spawn_subagent"}


def validate_subagent_spec(conn: sqlite3.Connection, spec: SubagentSpec) -> None:
    parent_matter = repo.matter_scope_for_target(conn, target_type="task", target_id=spec.parent_task_id)
    if parent_matter != spec.matter_scope:
        raise ValueError("subagent parent task must belong to the same matter")
    parent_row = conn.execute("SELECT task_provenance_json FROM tasks WHERE task_id = ?", (spec.parent_task_id,)).fetchone()
    parent_provenance = _json_object(str(parent_row["task_provenance_json"] or "{}")) if parent_row is not None else {}
    if parent_provenance.get("cache_sharing_group_id") or parent_provenance.get("parent_task_id"):
        raise ValueError("recursive subagent spawning requires explicit orchestrator approval")
    for source_id in spec.allowed_source_ids:
        source_matter = repo.matter_scope_for_target(conn, target_type="source", target_id=source_id)
        if source_matter != spec.matter_scope:
            raise ValueError(f"subagent source {source_id} is outside matter {spec.matter_scope}")
    for artifact_id in spec.allowed_artifact_ids:
        artifact_matter = repo.matter_scope_for_target(conn, target_type="artifact", target_id=artifact_id)
        if artifact_matter != spec.matter_scope:
            raise ValueError(f"subagent artifact {artifact_id} is outside matter {spec.matter_scope}")
    forbidden = sorted(set(spec.tools).intersection(FORBIDDEN_SUBAGENT_TOOLS))
    if forbidden:
        raise ValueError(f"subagent tools are not allowed: {', '.join(forbidden)}")
    if spec.model_decision.fallback_allowed:
        raise ValueError("subagent provider fallback is not allowed")
    if spec.model_decision.decision_tier == "blocked" or spec.model_decision.provider == "blocked" or spec.model_decision.model == "blocked":
        raise ValueError("subagent cannot use a blocked model decision")
    if spec.model_decision.provider in {"anthropic", "anthropic-oauth"} or spec.model_decision.runtime == "anthropic":
        raise ValueError("subagent cannot use reserved Anthropic provider policies")
    if is_held_openrouter_model(spec.model_decision.model):
        raise ValueError("subagent cannot use held/free OpenRouter models")
    if spec.model_decision.model == "deepseek/deepseek-v4-pro" and spec.model_decision.decision_tier != "pro_orchestrator":
        raise ValueError("subagent cannot choose Pro unless the decision layer selected Pro")
    if spec.model_decision.decision_tier == "pro_orchestrator" and spec.model_decision.profile_id == "":
        raise ValueError("subagent cannot choose Pro without a recorded model decision")
    if spec.max_turns < 1:
        raise ValueError("subagent max_turns must be positive")


def create_subagent_task(
    conn: sqlite3.Connection,
    spec: SubagentSpec,
    *,
    directive: str,
    write: bool = False,
) -> dict[str, object]:
    validate_subagent_spec(conn, spec)
    task_id = f"{spec.parent_task_id}-{spec.role}-{spec.cache_sharing_group_id}".replace(" ", "-")
    provider_policy = {
        "provider": spec.model_decision.provider,
        "model": spec.model_decision.model,
        "runtime": spec.model_decision.runtime,
        "allow_fallback": spec.model_decision.fallback_allowed,
        "model_profile_id": spec.model_decision.profile_id,
        "model_decision": spec.model_decision.__dict__,
        "model_decision_reason": spec.model_decision.decision_reason,
    }
    task_payload = {
        "task_id": task_id,
        "matter_scope": spec.matter_scope,
        "parent_task_id": spec.parent_task_id,
        "task_type": spec.task_type,
        "role": spec.role,
        "directive": directive,
        "provider_policy": provider_policy,
        "candidate_only": True,
        "canonical_writes": 0,
    }
    if not write:
        return {"dry_run": True, "task": task_payload}
    repo.add_task(
        conn,
        TaskSpec(
            task_id=task_id,
            matter_scope=spec.matter_scope,
            title=f"Subagent {spec.role}: {spec.task_type}",
            task_type=spec.task_type,
            instructions=(
                "Subagent candidate-only directive. Do not widen matter scope, spawn child subagents, "
                "or mutate canonical state. Directive: " + directive
            ),
            stage=LegalStage.S1_EXTRACTION,
            status=TaskStatus.QUEUED,
            source_dependencies=list(spec.allowed_source_ids),
            artifact_dependencies=list(spec.allowed_artifact_ids),
            provider_policy=provider_policy,
            validation_gates=["cross_matter_isolation"],
        ),
    )
    _ = conn.execute(
        "UPDATE tasks SET parent_task_id = ?, task_provenance_json = ? WHERE task_id = ?",
        (spec.parent_task_id, _json(spec.as_dict()), task_id),
    )
    return {"dry_run": False, "task": task_payload}


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str)


def _json_object(text: str) -> dict[str, object]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return dict(loaded) if isinstance(loaded, Mapping) else {}
