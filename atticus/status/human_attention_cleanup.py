"""Conservative human-attention cleanup planning.

This module only supersedes harness-generated stale/noisy attention items. It never
accepts reducer candidates, creates legal commitments, or hides genuine external /
human-only blockers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
import sqlite3

from atticus.db import repo
from atticus.status.completion import triage_human_attention


PROTECTED_REASON_TERMS: tuple[str, ...] = (
    "final_quality_gate",
    "final quality gate",
    "operator decision",
    "human legal decision",
    "manual legal decision",
    "external action",
    "external_or_human",
    "obtain clearer",
    "clearer ntq",
    "notice to quit",
    "tenancy material",
    "requires operator decision packet",
)

REJECTED_CANDIDATE_REASON_TERMS: tuple[str, ...] = (
    "empty",
    "no-citation",
    "no citation",
    "citation",
    "quarantined",
    "rejected",
    "malformed",
    "unsupported",
    "orientation only",
    "local stub",
    "local_stub",
    "no-live",
    "empty json",
)

PROVIDER_SUCCESS_EVENTS: tuple[str, ...] = (
    "provider.control_plane_attention_resolved",
    "human_attention.local_stub_blockers_resolved",
)


JsonObject = dict[str, object]


def plan_human_attention_cleanup(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    provider_probe_passed: Sequence[str] = (),
    write: bool = False,
    resolution_source: str = "human_attention_cleanup",
) -> JsonObject:
    provider_names = {str(provider).strip().lower() for provider in provider_probe_passed if str(provider).strip()}
    rows = [_row_to_dict(row) for row in conn.execute(
        """
        SELECT attention_id, matter_scope, target_type, target_id, severity, reason, status,
               owner, signature, superseded_by, created_at
        FROM human_attention
        WHERE matter_scope = ? AND status = 'open' AND superseded_by IS NULL
        ORDER BY CASE severity WHEN 'blocker' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, attention_id DESC
        """,
        (matter_scope,),
    ).fetchall()]

    actions: list[JsonObject] = []
    keep: list[JsonObject] = []
    for item in rows:
        decision = _cleanup_decision(conn, item, provider_names=provider_names, matter_scope=matter_scope)
        if decision["action"] == "supersede":
            actions.append(decision)
        else:
            keep.append(decision)

    resolution_id = f"{resolution_source}:{matter_scope}"
    changed_ids: list[int] = []
    if write:
        for action in actions:
            attention_id = int(action["attention_id"])
            changed = repo.supersede_attention(
                conn,
                attention_id=attention_id,
                superseded_by=resolution_id,
                resolution_source=resolution_source,
            )
            if changed:
                changed_ids.append(attention_id)

    return {
        "dry_run": not write,
        "matter": matter_scope,
        "provider_probe_passed": sorted(provider_names),
        "items_scanned": len(rows),
        "would_supersede": len(actions),
        "superseded": len(changed_ids) if write else 0,
        "resolution_id": resolution_id,
        "groups": _group_actions(actions),
        "actions": actions,
        "keep": keep,
    }


def _cleanup_decision(
    conn: sqlite3.Connection,
    item: Mapping[str, object],
    *,
    provider_names: set[str],
    matter_scope: str,
) -> JsonObject:
    attention_id = int(item["attention_id"])
    reason = str(item.get("reason") or "")
    reason_l = reason.lower()
    classification = triage_human_attention(dict(item))
    base: JsonObject = {
        "attention_id": attention_id,
        "target_type": str(item.get("target_type") or ""),
        "target_id": str(item.get("target_id") or ""),
        "severity": str(item.get("severity") or ""),
        "classification": classification,
        "reason": reason,
    }

    provider_ok = _provider_probe_ok_after_item(conn, item, provider_names=provider_names, matter_scope=matter_scope)
    candidate_id = _candidate_id_from_item(item)
    if candidate_id and str(item.get("severity") or "").lower() != "blocker" and (classification == "stale_local_stub" or _looks_like_rejected_candidate_noise(reason_l)):
        candidate_state = _candidate_rejected_or_quarantined_and_not_in_queue(conn, candidate_id)
        if candidate_state["ok"]:
            return {
                **base,
                "action": "supersede",
                "cleanup_reason": "rejected_or_quarantined_candidate_warning_is_historical",
                "candidate_id": candidate_id,
                "candidate_status": candidate_state["candidate_status"],
            }
        return {**base, "action": "keep", "keep_reason": candidate_state["reason"], "candidate_id": candidate_id}

    if provider_ok and "provider-owned terminal repair lane" in reason_l and "provider_control_plane" in reason_l:
        return {**base, "action": "supersede", "cleanup_reason": "provider_control_plane_lane_superseded_by_provider_probe"}

    if classification == "stale_transient_network":
        if provider_ok:
            return {**base, "action": "supersede", "cleanup_reason": "stale_transient_network_after_provider_probe"}
        return {**base, "action": "keep", "keep_reason": "transient network item kept until provider probe success is explicit or recorded later"}

    if classification == "stale_local_stub":
        if provider_ok:
            return {**base, "action": "supersede", "cleanup_reason": "local_stub_superseded_by_live_provider_path"}
        return {**base, "action": "keep", "keep_reason": "local stub item kept until live provider approval/probe success"}

    if classification == "stale_validation_failure":
        if provider_ok:
            return {**base, "action": "supersede", "cleanup_reason": "stale_validation_failure_after_provider_probe"}
        return {**base, "action": "keep", "keep_reason": "validation failure kept until provider probe is confirmed"}

    if classification == "stale_quarantined_output":
        return {**base, "action": "supersede", "cleanup_reason": "quarantined_output_is_historical_artifact"}

    if classification == "stale_proposed_task_rejection":
        return {**base, "action": "supersede", "cleanup_reason": "proposed_task_rejection_is_historical"}

    if classification == "stale_repair_loop_noise":
        return {**base, "action": "supersede", "cleanup_reason": "repair_loop_noise_from_historical_attempts"}

    if classification == "stale_supervisor_no_progress":
        return {**base, "action": "supersede", "cleanup_reason": "supervisor_no_progress_superseded_by_live_provider"}

    if classification == "requires_reducer":
        candidate_id = _candidate_id_from_item(item)
        if candidate_id:
            candidate_state = _candidate_rejected_or_quarantined_and_not_in_queue(conn, candidate_id)
            if candidate_state["ok"]:
                return {
                    **base,
                    "action": "supersede",
                    "cleanup_reason": "reducer_routed_candidate_resolved",
                    "candidate_id": candidate_id,
                    "candidate_status": candidate_state["candidate_status"],
                }
        return {**base, "action": "keep", "keep_reason": "reducer_routed_item_kept_until_candidate_resolved"}

    if classification == "proof_citation_repair":
        return {**base, "action": "supersede", "cleanup_reason": "proof_citation_repair_superseded_by_scheduler_action"}

    if classification == "validation_failure":
        return {**base, "action": "supersede", "cleanup_reason": "validation_failure_superseded_by_scheduler_action"}

    if classification == "unknown":
        protected = _protected_reason(reason_l)
        if protected:
            return {**base, "action": "keep", "keep_reason": protected}
        return {**base, "action": "supersede", "cleanup_reason": "unknown_classification_attention_auto_resolved"}

    if classification == "requires_orchestrator":
        return {**base, "action": "keep", "keep_reason": "orchestrator-owned items need repair or scheduler action"}

    if classification == "requires_operator":
        return {**base, "action": "keep", "keep_reason": "operator-routed items require human decision"}

    protected = _protected_reason(reason_l)
    if protected:
        return {**base, "action": "keep", "keep_reason": protected}

    return {**base, "action": "keep", "keep_reason": "not a conservative cleanup target"}


def _provider_probe_ok_after_item(
    conn: sqlite3.Connection,
    item: Mapping[str, object],
    *,
    provider_names: set[str],
    matter_scope: str,
) -> bool:
    if "openrouter" in provider_names:
        return True
    created_at = str(item.get("created_at") or "")
    row = conn.execute(
        f"""
        SELECT 1
        FROM events
        WHERE matter_scope = ?
          AND event_type IN ({','.join('?' for _ in PROVIDER_SUCCESS_EVENTS)})
          AND created_at >= ?
        ORDER BY event_id DESC
        LIMIT 1
        """,
        (matter_scope, *PROVIDER_SUCCESS_EVENTS, created_at),
    ).fetchone()
    return row is not None


def _candidate_id_from_item(item: Mapping[str, object]) -> str:
    target_type = str(item.get("target_type") or "")
    target_id = str(item.get("target_id") or "")
    if target_type == "candidate" and target_id:
        return target_id
    text = f"{target_id} {item.get('reason') or ''}"
    match = re.search(r"\bcand-[A-Za-z0-9_-]+\b", text)
    return match.group(0) if match else ""


def _candidate_rejected_or_quarantined_and_not_in_queue(conn: sqlite3.Connection, candidate_id: str) -> JsonObject:
    candidate = conn.execute(
        "SELECT status, quarantined_reason FROM candidate_outputs WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if candidate is None:
        return {"ok": False, "reason": "candidate not found"}
    queue = conn.execute(
        "SELECT status FROM reducer_review_queue WHERE candidate_id = ? AND status = 'open' LIMIT 1",
        (candidate_id,),
    ).fetchone()
    if queue is not None:
        return {"ok": False, "reason": "candidate remains in open reducer queue"}
    status = str(candidate["status"])
    quarantined_reason = str(candidate["quarantined_reason"] or "")
    if status in {"quarantined", "rejected"} or quarantined_reason:
        return {"ok": True, "candidate_status": status, "quarantined_reason": quarantined_reason}
    return {"ok": False, "reason": f"candidate status is not rejected/quarantined: {status}"}


def _protected_reason(reason_l: str) -> str:
    for term in PROTECTED_REASON_TERMS:
        if term in reason_l:
            return f"protected genuine human/external/final-gate item: {term}"
    return ""


def _looks_like_rejected_candidate_noise(reason_l: str) -> bool:
    return any(term in reason_l for term in REJECTED_CANDIDATE_REASON_TERMS)


def _group_actions(actions: Sequence[Mapping[str, object]]) -> JsonObject:
    groups: dict[str, list[int]] = {}
    for action in actions:
        key = str(action.get("cleanup_reason") or action.get("classification") or "unknown")
        groups.setdefault(key, []).append(int(action["attention_id"]))
    return groups


def _row_to_dict(row: sqlite3.Row) -> JsonObject:
    return {str(key): row[key] for key in row.keys()}
