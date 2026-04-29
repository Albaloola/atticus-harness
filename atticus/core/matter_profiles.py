"""Matter-local adaptive profile helpers.

This module is deliberately policy-heavy and execution-light. Profiles may
shape what work is proposed for a matter, but they may not weaken the global
evidence, citation, reducer, human-review, or external-action guardrails.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import sqlite3
from typing import cast

from atticus.core.policies import LegalStage
from atticus.db import repo
from atticus.providers.deepseek import is_held_openrouter_model


DEFAULT_TEMPLATE = "default_s0_s9"
MANDATORY_S8_S9_GATES = {"claim_evidence_support", "citation_integrity", "hostile_review", "privacy_redaction"}


@dataclass(frozen=True)
class MatterProfileAdaptation:
    matter_scope: str
    profile_name: str
    base_template: str
    stages: tuple[dict[str, object], ...]
    reason: str
    diff: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "matter_scope": self.matter_scope,
            "profile_name": self.profile_name,
            "base_template": self.base_template,
            "stages": list(self.stages),
            "reason": self.reason,
            "diff": self.diff,
        }


def create_default_matter_profile(conn: sqlite3.Connection, matter_scope: str) -> str:
    """Create the default S0-S9 profile for one matter if none is active."""

    active = repo.get_active_matter_profile(conn, matter_scope=matter_scope)
    if active is not None:
        return str(active["matter_profile_id"])
    return repo.create_matter_profile(
        conn,
        matter_scope=matter_scope,
        profile_name="Default S0-S9 profile",
        stages=_default_stage_rows(),
        base_template=DEFAULT_TEMPLATE,
        reason="default matter profile initialized",
        requested_by="atticus",
    )


def get_active_matter_profile(conn: sqlite3.Connection, matter_scope: str) -> dict[str, object] | None:
    return repo.get_active_matter_profile(conn, matter_scope=matter_scope)


def propose_matter_profile_adaptation(
    conn: sqlite3.Connection,
    matter_scope: str,
    goal: str,
    evidence_state: Mapping[str, object] | None,
) -> MatterProfileAdaptation:
    """Return a deterministic dry-run profile proposal for one matter."""

    _ = create_default_matter_profile(conn, matter_scope)
    state = evidence_state or {}
    goal_text = " ".join(goal.split()).strip()
    lowered = goal_text.lower()
    enabled = {stage.value for stage in LegalStage}
    reasons: list[str] = []

    if _simple_factual_goal(lowered, state):
        enabled = {"S0", "S1"}
        reasons.append("simple factual/source goal only needs source inventory and extraction QA")
    elif any(term in lowered for term in ("authority", "case law", "statute", "legal research", "procedure")):
        enabled = {"S0", "S1", "S2", "S5", "S6", "S7"}
        reasons.append("authority/procedure goal needs issue routing, authority mapping, and review")
    elif any(term in lowered for term in ("draft", "complaint", "pleading", "filing", "submission", "letter")):
        enabled = {"S0", "S1", "S2", "S5", "S6", "S7", "S8", "S9"}
        reasons.append("draft/final-output goal needs evidence, authority, hostile review, draft, and final gates")

    if int(state.get("contradiction_count") or 0) > 0:
        enabled.update({"S5", "S7"})
        reasons.append("contradictions require issue routing and hostile/contradiction review")
    if bool(state.get("authority_required")):
        enabled.update({"S6", "S7"})
        reasons.append("authority_required adds authority map and review")

    stages = tuple(_stage_row(stage.value, enabled=stage.value in enabled) for stage in LegalStage)
    active = repo.get_active_matter_profile(conn, matter_scope=matter_scope)
    old_enabled = [
        str(stage["stage"])
        for stage in cast(list[Mapping[str, object]], active.get("stages", []) if active else [])
        if bool(stage.get("enabled"))
    ]
    diff = {
        "old_enabled_stages": old_enabled,
        "new_enabled_stages": [stage["stage"] for stage in stages if stage["enabled"]],
        "reasons": reasons or ["default profile retained with matter-local fingerprint"],
    }
    proposal = MatterProfileAdaptation(
        matter_scope=matter_scope,
        profile_name=f"Adaptive profile for {goal_text or 'matter work'}",
        base_template=DEFAULT_TEMPLATE,
        stages=stages,
        reason="; ".join(cast(list[str], diff["reasons"])),
        diff=diff,
    )
    _validate_adaptation(proposal.as_dict())
    return proposal


def apply_matter_profile_adaptation(
    conn: sqlite3.Connection,
    matter_scope: str,
    adaptation: Mapping[str, object],
    *,
    write: bool = False,
) -> dict[str, object]:
    """Validate and optionally activate a matter-local profile adaptation."""

    if str(adaptation.get("matter_scope") or matter_scope) != matter_scope:
        raise ValueError("adaptation matter_scope must match requested matter")
    _validate_adaptation(adaptation)
    stages = _stage_dicts(adaptation.get("stages"))
    payload = {
        "dry_run": not write,
        "matter_scope": matter_scope,
        "profile_name": str(adaptation.get("profile_name") or "Adaptive profile"),
        "reason": str(adaptation.get("reason") or "operator-applied matter profile adaptation"),
        "stages": stages,
        "external_actions": "blocked",
    }
    if not write:
        return payload
    profile_id = repo.create_matter_profile(
        conn,
        matter_scope=matter_scope,
        profile_name=str(payload["profile_name"]),
        stages=stages,
        base_template=str(adaptation.get("base_template") or DEFAULT_TEMPLATE),
        reason=str(payload["reason"]),
    )
    return {**payload, "dry_run": False, "matter_profile_id": profile_id, "active_profile": repo.get_active_matter_profile(conn, matter_scope=matter_scope)}


def reset_matter_profile_to_default(conn: sqlite3.Connection, matter_scope: str, *, write: bool = False) -> dict[str, object]:
    payload = {
        "dry_run": not write,
        "matter_scope": matter_scope,
        "profile_name": "Default S0-S9 profile",
        "stages": _default_stage_rows(),
        "external_actions": "blocked",
    }
    if not write:
        return payload
    profile_id = repo.create_matter_profile(
        conn,
        matter_scope=matter_scope,
        profile_name="Default S0-S9 profile",
        stages=_default_stage_rows(),
        base_template=DEFAULT_TEMPLATE,
        reason="matter-local profile reset to default",
    )
    return {**payload, "dry_run": False, "matter_profile_id": profile_id, "active_profile": repo.get_active_matter_profile(conn, matter_scope=matter_scope)}


def _default_stage_rows() -> list[dict[str, object]]:
    return [_stage_row(stage.value, enabled=True) for stage in LegalStage]


def _stage_row(stage: str, *, enabled: bool) -> dict[str, object]:
    gate_policy: dict[str, object] = {"external_actions_enabled": False}
    if stage in {"S8", "S9"}:
        gate_policy["human_review_required"] = True
        gate_policy["validation_gates"] = sorted(MANDATORY_S8_S9_GATES)
    return {
        "stage": stage,
        "enabled": enabled,
        "gate_policy": gate_policy,
        "worker_policy": {"candidate_only": True, "canonical_writes": "reducer_only"},
        "model_policy": {"high_risk_flash_requires_human_review": True},
    }


def _validate_adaptation(adaptation: Mapping[str, object]) -> None:
    stages = _stage_dicts(adaptation.get("stages"))
    if not stages:
        raise ValueError("matter profile adaptation requires stages")
    for stage in stages:
        stage_name = str(stage.get("stage") or "")
        gate_policy = _json_policy(stage.get("gate_policy") or stage.get("gate_policy_json"))
        worker_policy = _json_policy(stage.get("worker_policy") or stage.get("worker_policy_json"))
        model_policy = _json_policy(stage.get("model_policy") or stage.get("model_policy_json"))
        if bool(gate_policy.get("external_actions_enabled")) or bool(worker_policy.get("external_actions_enabled")):
            raise ValueError("matter profile adaptation cannot enable external actions")
        if gate_policy.get("disable_citation_gates") is True or gate_policy.get("citation_integrity") is False:
            raise ValueError("matter profile adaptation cannot disable citation gates")
        if worker_policy.get("canonical_writes") not in {None, "", "reducer_only"}:
            raise ValueError("matter profile adaptation cannot bypass reducer-only canonical writes")
        model = str(model_policy.get("model") or "")
        if model and is_held_openrouter_model(model):
            raise ValueError("matter profile adaptation cannot route to held/free OpenRouter models")
        if stage_name in {"S8", "S9"}:
            if gate_policy.get("human_review_required") is False:
                raise ValueError("matter profile adaptation cannot remove human review from S8/S9")
            gates = set(_string_list(gate_policy.get("validation_gates")))
            if gates and not gates.intersection({"citation_integrity", "legal_citation_integrity"}):
                raise ValueError("S8/S9 profile validation gates must retain citation integrity")
            if stage_name == "S9" and model_policy.get("decision_tier") == "flash_worker" and gate_policy.get("human_review_required") is not True:
                raise ValueError("S9 cannot route to Flash without required human review")


def _stage_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list | tuple):
        return []
    return [dict(cast(Mapping[str, object], item)) for item in value if isinstance(item, Mapping)]


def _json_policy(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in cast(Mapping[object, object], value).items()}
    if isinstance(value, str) and value.strip():
        loaded = json.loads(value)
        if isinstance(loaded, Mapping):
            return {str(key): item for key, item in cast(Mapping[object, object], loaded).items()}
    return {}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [str(item) for item in value]


def _simple_factual_goal(goal: str, evidence_state: Mapping[str, object]) -> bool:
    if bool(evidence_state.get("authority_required")) or int(evidence_state.get("contradiction_count") or 0) > 0:
        return False
    return any(term in goal for term in ("inventory", "extract", "triage", "classify", "deduplicate", "source"))
