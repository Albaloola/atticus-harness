"""Reducer decision logic and canonical artifact writing."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from atticus.core.policies import TaskStatus, TrustStatus
from atticus.db import repo
from atticus.scheduler.lease import complete_lease, require_active_lease
from atticus.validation.canonical_write_guard import assert_canonical_write_allowed
from atticus.validation.gates import run_validation
from atticus.workers.result_parser import parse_result


class ReductionBlocked(RuntimeError):
    """Raised when a candidate cannot be reduced safely."""


def choose_candidate(candidate_ids: list[str]) -> str | None:
    return candidate_ids[0] if candidate_ids else None


def reduce_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    reducer_lease_id: str,
    writer_role: str = "reducer",
    dry_run: bool = True,
) -> dict[str, Any]:
    candidate = conn.execute(
        "SELECT * FROM candidate_outputs WHERE candidate_id = ?",
        (candidate_id,),
    ).fetchone()
    if candidate is None:
        raise ReductionBlocked(f"unknown candidate: {candidate_id}")
    if candidate["status"] != "candidate":
        raise ReductionBlocked(f"candidate {candidate_id} has status {candidate['status']}")
    require_active_lease(conn, lease_id=reducer_lease_id, task_id=candidate["task_id"])
    assert_canonical_write_allowed(
        writer_role=writer_role,
        target_path=f"canonical://candidate/{candidate_id}",
        conn=conn,
        lease_id=reducer_lease_id,
        task_id=candidate["task_id"],
    )

    packet = parse_result(json.loads(candidate["payload_json"]))
    proposed = packet.proposed_artifacts[0] if packet.proposed_artifacts else {}
    canonical_preview = {
        "candidate_id": candidate_id,
        "task_id": candidate["task_id"],
        "summary": packet.summary,
        "proposed_artifact": proposed,
        "dry_run": dry_run,
    }
    if dry_run:
        return {**canonical_preview, "validations": ["reducer_packet_schema", "canonical_write_authorization"]}

    schema_validation = run_validation(
        conn,
        gate_name="reducer_packet_schema",
        target_type="candidate",
        target_id=candidate_id,
    )
    auth_validation = run_validation(
        conn,
        gate_name="canonical_write_authorization",
        target_type="candidate",
        target_id=candidate_id,
    )
    if not schema_validation.passed or not auth_validation.passed:
        raise ReductionBlocked("candidate failed reducer validations")

    artifact_id = repo.add_artifact(
        conn,
        path=str(proposed.get("path") or f"canonical/{candidate['task_id']}/{candidate_id}.json"),
        artifact_type=str(proposed.get("artifact_type") or "reduced_result"),
        stage=str(proposed.get("stage") or ""),
        trust_status=TrustStatus.VALIDATED,
        title=str(proposed.get("title") or f"Reduced result for {candidate['task_id']}"),
        content=json.dumps(
            {
                "summary": packet.summary,
                "findings": packet.findings,
                "citations": packet.citations,
                "candidate_id": candidate_id,
            },
            sort_keys=True,
            indent=2,
        ),
        produced_by_task_id=candidate["task_id"],
    )
    reducer_packet_id = repo.record_reducer_packet(
        conn,
        candidate_id=candidate_id,
        reducer_lease_id=reducer_lease_id,
        decision="accepted",
        validation_result_id=schema_validation.validation_result_id,
        canonical_artifact_id=artifact_id,
        dissent=[],
    )
    conn.execute(
        "UPDATE candidate_outputs SET status = 'reduced' WHERE candidate_id = ?",
        (candidate_id,),
    )
    complete_lease(conn, lease_id=reducer_lease_id, task_status=TaskStatus.COMPLETE)
    return {
        **canonical_preview,
        "dry_run": False,
        "artifact_id": artifact_id,
        "reducer_packet_id": reducer_packet_id,
    }
