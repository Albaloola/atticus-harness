"""Internal lifecycle hooks for legal safety and audit checks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import sqlite3

from atticus.db import repo


@dataclass(frozen=True)
class HookOutcome:
    event_name: str
    allowed: bool
    severity: str
    message: str
    details: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "event_name": self.event_name,
            "allowed": self.allowed,
            "severity": self.severity,
            "message": self.message,
            "details": self.details,
        }


def run_hooks(
    conn: sqlite3.Connection,
    *,
    event_name: str,
    matter_scope: str,
    payload: Mapping[str, object] | None = None,
) -> list[HookOutcome]:
    """Run built-in Python hooks and persist every outcome.

    Hooks are deliberately internal and data-only. They do not execute shell
    commands, provider calls, email, filing, upload, or any other external
    action. They exist to make Atticus fail closed at lifecycle boundaries.
    """

    hook_payload = dict(payload or {})
    outcomes: list[HookOutcome] = []
    outcomes.extend(_external_action_hook(event_name, hook_payload))
    outcomes.extend(_cross_matter_hook(event_name, matter_scope, hook_payload))
    outcomes.extend(_staleness_hook(event_name, hook_payload))
    outcomes.extend(_final_draft_review_hook(event_name, hook_payload))
    if not outcomes:
        outcomes.append(
            HookOutcome(
                event_name=event_name,
                allowed=True,
                severity="info",
                message="no blocking lifecycle hook matched",
                details={},
            )
        )
    for outcome in outcomes:
        _ = repo.record_hook_invocation(
            conn,
            hook_event=event_name,
            matter_scope=matter_scope,
            allowed=outcome.allowed,
            severity=outcome.severity,
            message=outcome.message,
            details=outcome.details,
        )
    return outcomes


def _external_action_hook(event_name: str, payload: Mapping[str, object]) -> list[HookOutcome]:
    action_type = str(payload.get("action_type") or payload.get("external_action_type") or "")
    has_external_request = bool(payload.get("external_action_request") or payload.get("external_action_requests"))
    dangerous_action = action_type.lower() in {"email", "file", "filing", "upload", "contact", "message", "court"}
    if event_name == "ExternalActionBlocked" or has_external_request or dangerous_action:
        return [
            HookOutcome(
                event_name=event_name,
                allowed=False,
                severity="blocker",
                message="external legal actions are blocked by Atticus lifecycle hooks",
                details={"action_type": action_type, "requested_by": str(payload.get("requested_by") or "")},
            )
        ]
    return []


def _cross_matter_hook(
    event_name: str,
    matter_scope: str,
    payload: Mapping[str, object],
) -> list[HookOutcome]:
    authorized = str(payload.get("authorized_matter_scope") or payload.get("matter_scope") or "")
    if authorized and authorized != matter_scope:
        return [
            HookOutcome(
                event_name=event_name,
                allowed=False,
                severity="blocker",
                message="cross-matter context is blocked",
                details={"matter_scope": matter_scope, "authorized_matter_scope": authorized},
            )
        ]
    return []


def _staleness_hook(event_name: str, payload: Mapping[str, object]) -> list[HookOutcome]:
    stale_source_ids = _string_list(payload.get("stale_source_ids"))
    stale_artifact_ids = _string_list(payload.get("stale_artifact_ids"))
    if bool(payload.get("source_stale")) and not stale_source_ids:
        stale_source_ids = ["unknown"]
    if not stale_source_ids and not stale_artifact_ids:
        return []
    return [
        HookOutcome(
            event_name=event_name,
            allowed=True,
            severity="warning",
            message="stale evidence is present and must be flagged in the work product",
            details={"stale_source_ids": stale_source_ids, "stale_artifact_ids": stale_artifact_ids},
        )
    ]


def _final_draft_review_hook(event_name: str, payload: Mapping[str, object]) -> list[HookOutcome]:
    if event_name not in {"PreReduce", "PostCandidate"}:
        return []
    stage = str(payload.get("stage") or "")
    task_type = str(payload.get("task_type") or "")
    if stage != "S9" and task_type not in {"final_quality_gate", "final_draft"}:
        return []
    certifications = _string_list(payload.get("certifications"))
    required = _string_list(payload.get("required_certifications"))
    has_hostile_review = "hostile_review" in certifications or "hostile_review" not in required
    if has_hostile_review:
        return []
    return [
        HookOutcome(
            event_name=event_name,
            allowed=False,
            severity="blocker",
            message="final drafting is blocked until hostile review certification exists",
            details={"stage": stage, "task_type": task_type, "required_certifications": required},
        )
    ]


def _string_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list | tuple):
        return [str(item) for item in value if str(item)]
    return []
