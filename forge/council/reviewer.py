"""General reviewer for Forge diffs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
import json
from typing import Any, cast

from forge.audit.packet import GateResult, ReviewerVerdict
from forge.config import ForgeConfig
from forge.loop.task import TaskPacket
from forge.openrouter.client import DEFAULT_PROVIDER, OpenRouterClient
from forge.openrouter.prompts import FORGE_SYSTEM_POLICY


def review_diff(
    *,
    task: TaskPacket,
    changed_files: list[str],
    diff: str,
    gate_results: list[GateResult],
    config: ForgeConfig,
    client: OpenRouterClient | None = None,
    offline: bool = False,
) -> ReviewerVerdict:
    if offline:
        return heuristic_review(task=task, changed_files=changed_files, diff=diff, gate_results=gate_results)
    client = client or OpenRouterClient()
    profile = config.models["reviewer"]
    payload = {
        "task": task.as_dict(),
        "changed_files": changed_files,
        "diff": diff[-60000:],
        "gate_results": [asdict(result) for result in gate_results],
        "policy": {
            "forbidden_paths": config.forbidden_paths,
            "required_principles": config.required_principles,
            "diff_limits": asdict(config.diff_limits),
        },
        "required_schema": {
            "role": "reviewer",
            "verdict": "approve | repair | reject",
            "confidence": 0.0,
            "risk_level": "low | medium | high",
            "blocking_issues": [],
            "non_blocking_issues": [],
            "recommended_repairs": [],
            "files_of_concern": [],
        },
    }
    response = client.chat_json(
        model=profile.model,
        temperature=profile.temperature,
        max_tokens=profile.max_tokens,
        provider=DEFAULT_PROVIDER,
        messages=[
            {"role": "system", "content": FORGE_SYSTEM_POLICY},
            {"role": "user", "content": json.dumps(payload, sort_keys=True)},
        ],
    )
    verdict = _coerce_verdict(response.get("content", {}), role="reviewer")
    usage = response.get("usage")
    if isinstance(usage, Mapping):
        verdict.usage = dict(cast(Mapping[str, Any], usage))
    return verdict


def heuristic_review(*, task: TaskPacket, changed_files: list[str], diff: str, gate_results: list[GateResult]) -> ReviewerVerdict:
    del task
    blockers: list[str] = []
    if not diff.strip():
        blockers.append("No diff was produced.")
    failed = [result.name for result in gate_results if not result.passed]
    if failed:
        blockers.append(f"Deterministic gates failed: {', '.join(failed)}")
    verdict = "reject" if blockers else "approve"
    return ReviewerVerdict(
        role="reviewer",
        verdict=verdict,
        confidence=0.72,
        risk_level="medium" if len(changed_files) > 4 else "low",
        blocking_issues=blockers,
        non_blocking_issues=[] if blockers else ["Offline heuristic review; no model critique was requested."],
        recommended_repairs=blockers,
        files_of_concern=changed_files if blockers else [],
    )


def _coerce_verdict(raw: object, *, role: str) -> ReviewerVerdict:
    if not isinstance(raw, dict):
        return ReviewerVerdict(role=role, verdict="reject", confidence=0.0, risk_level="high", blocking_issues=["Reviewer returned non-object JSON"])
    verdict = str(raw.get("verdict") or "reject")
    if verdict not in {"approve", "repair", "reject"}:
        verdict = "reject"
    return ReviewerVerdict(
        role=str(raw.get("role") or role),
        verdict=verdict,
        confidence=_float(raw.get("confidence"), 0.0),
        risk_level=str(raw.get("risk_level") or "high"),
        blocking_issues=_string_list(raw.get("blocking_issues")),
        non_blocking_issues=_string_list(raw.get("non_blocking_issues")),
        recommended_repairs=_string_list(raw.get("recommended_repairs")),
        files_of_concern=_string_list(raw.get("files_of_concern")),
    )


def _string_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _float(value: object, default: float) -> float:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return default
