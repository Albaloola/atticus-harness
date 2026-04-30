"""High-level same-matter reuse helpers for follow-up work."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import sqlite3
from typing import cast

from atticus.db import repo
from atticus.retrieval.rank import lexical_score


@dataclass(frozen=True)
class ReuseDecision:
    reusable: bool
    reason: str
    trusted_as_proof: bool = False
    orientation_allowed: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "reusable": self.reusable,
            "reason": self.reason,
            "trusted_as_proof": self.trusted_as_proof,
            "orientation_allowed": self.orientation_allowed,
        }


def validate_reusable_step(conn: sqlite3.Connection, step_id: str) -> ReuseDecision:
    row = conn.execute("SELECT * FROM work_run_steps WHERE work_run_step_id = ?", (step_id,)).fetchone()
    if row is None:
        return ReuseDecision(False, "work-run step not found", orientation_allowed=False)
    step = {str(key): row[key] for key in row.keys()}
    matter_scope = str(step["matter_scope"])
    if str(step["status"]) != "complete":
        return ReuseDecision(False, f"step status is {step['status']}", orientation_allowed=False)
    task_id = _optional_id(step, "task_id")
    if task_id:
        task = conn.execute("SELECT status, matter_scope FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None:
            return ReuseDecision(False, "linked task missing", orientation_allowed=False)
        if str(task["matter_scope"]) != matter_scope:
            return ReuseDecision(False, "linked task belongs to another matter", orientation_allowed=False)
        if str(task["status"]) not in {"complete", "reducer_pending"}:
            return ReuseDecision(False, f"linked task is not complete: {task['status']}", orientation_allowed=True)
    artifact_id = _optional_id(step, "artifact_id")
    if artifact_id:
        artifact_decision = _artifact_reuse_decision(conn, matter_scope=matter_scope, artifact_id=artifact_id)
        if not artifact_decision.reusable:
            return artifact_decision
    candidate_id = _optional_id(step, "candidate_id")
    if candidate_id:
        candidate_decision = _candidate_reuse_decision(conn, matter_scope=matter_scope, candidate_id=candidate_id)
        if not candidate_decision.reusable:
            return candidate_decision
    context_pack_id = _optional_id(step, "context_pack_id")
    if context_pack_id:
        context_decision = _context_pack_reuse_decision(conn, matter_scope=matter_scope, context_pack_id=context_pack_id)
        if not context_decision.reusable:
            return context_decision
    provider_run_id = _optional_id(step, "provider_run_id")
    if provider_run_id:
        provider_decision = _same_matter_target_decision(conn, matter_scope=matter_scope, target_type="provider_run", target_id=provider_run_id)
        if not provider_decision.reusable:
            return provider_decision
    if artifact_id:
        return ReuseDecision(True, "same matter, validated artifact and source dependencies current", trusted_as_proof=True)
    return ReuseDecision(True, "same matter complete step; reuse as orientation unless separately certified", trusted_as_proof=False)


def find_reusable_artifacts(conn: sqlite3.Connection, matter_scope: str, goal: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in conn.execute(
        """
        SELECT artifact_id, matter_scope, path, artifact_type, stage, trust_status, stale, title, content
        FROM artifacts
        WHERE matter_scope = ? AND stale = 0 AND trust_status IN ('validated', 'certified')
        ORDER BY updated_at DESC, artifact_id
        """,
        (matter_scope,),
    ):
        item = dict(cast(Mapping[str, object], row))
        decision = _artifact_reuse_decision(conn, matter_scope=matter_scope, artifact_id=str(item["artifact_id"]))
        if not decision.reusable:
            continue
        item["reuse_decision"] = decision.as_dict()
        rows.append(item)
    return _rank(goal, rows, id_key="artifact_id")


def find_reusable_candidates(conn: sqlite3.Connection, matter_scope: str, goal: str) -> list[dict[str, object]]:
    rows = [
        {
            **dict(cast(Mapping[str, object], row)),
            "trusted_as_proof": False,
            "reuse_note": "candidate-only output may orient follow-up work but is not trusted evidence",
        }
        for row in conn.execute(
            """
            SELECT co.candidate_id, t.matter_scope, t.task_type, t.title, co.status, co.payload_json
            FROM candidate_outputs co
            JOIN tasks t ON t.task_id = co.task_id
            WHERE t.matter_scope = ? AND co.status = 'candidate'
            ORDER BY co.created_at DESC
            """,
            (matter_scope,),
        )
    ]
    return _rank(goal, rows, id_key="candidate_id")


def find_reusable_context_packs(conn: sqlite3.Connection, matter_scope: str, goal: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for row in conn.execute(
        """
        SELECT context_pack_id, matter_scope, task_id, pack_type, fingerprint, estimated_tokens, sections_json
        FROM context_packs
        WHERE matter_scope = ?
        ORDER BY created_at DESC
        LIMIT 50
        """,
        (matter_scope,),
    ):
        item = dict(cast(Mapping[str, object], row))
        decision = _context_pack_reuse_decision(conn, matter_scope=matter_scope, context_pack_id=str(item["context_pack_id"]))
        if not decision.reusable:
            continue
        item["reuse_decision"] = decision.as_dict()
        rows.append(item)
    return _rank(goal, rows, id_key="context_pack_id")


def build_followup_context(conn: sqlite3.Connection, matter_scope: str, question: str) -> dict[str, object]:
    memories = [
        dict(cast(Mapping[str, object], row))
        for row in conn.execute(
            """
            SELECT memory_id, type, name, description, confidence, stale
            FROM legal_memories
            WHERE matter_scope = ? AND status = 'active' AND stale = 0
            ORDER BY type, name
            LIMIT 25
            """,
            (matter_scope,),
        )
    ]
    return {
        "matter_scope": matter_scope,
        "question": question,
        "artifacts": find_reusable_artifacts(conn, matter_scope, question),
        "candidates": find_reusable_candidates(conn, matter_scope, question),
        "context_packs": find_reusable_context_packs(conn, matter_scope, question),
        "memory_orientation": memories,
        "rules": [
            "reuse is same-matter only",
            "validated/certified artifacts may be reused when source snapshots remain current",
            "candidate output and active memory orient work only; neither is proof",
            "provider/model decisions are provenance, not correctness evidence",
        ],
    }


def explain_reuse_decision(conn: sqlite3.Connection, matter_scope: str, records: list[Mapping[str, object]]) -> dict[str, object]:
    del conn
    explanations = []
    for record in records:
        trust = str(record.get("trust_status") or record.get("status") or "")
        trusted_as_proof = trust in {"validated", "certified"}
        orientation_only = record.get("trusted_as_proof") is False
        explanations.append(
            {
                "record_id": str(record.get("artifact_id") or record.get("candidate_id") or record.get("context_pack_id") or ""),
                "reuse_allowed": trusted_as_proof,
                "orientation_allowed": orientation_only,
                "proof_status": "orientation_only" if orientation_only else trust,
                "reason": "same matter and non-stale; recheck citations before legal reliance",
            }
        )
    return {"matter_scope": matter_scope, "reuse_explanations": explanations}


def _artifact_reuse_decision(conn: sqlite3.Connection, *, matter_scope: str, artifact_id: str) -> ReuseDecision:
    row = conn.execute(
        "SELECT matter_scope, stale, trust_status FROM artifacts WHERE artifact_id = ?",
        (artifact_id,),
    ).fetchone()
    if row is None:
        return ReuseDecision(False, "linked artifact missing", orientation_allowed=False)
    if str(row["matter_scope"]) != matter_scope:
        return ReuseDecision(False, "linked artifact belongs to another matter", orientation_allowed=False)
    if int(row["stale"] or 0):
        return ReuseDecision(False, "artifact stale", orientation_allowed=False)
    if str(row["trust_status"]) not in {"validated", "certified"}:
        return ReuseDecision(False, f"artifact trust_status is {row['trust_status']}", orientation_allowed=True)
    stale_source = conn.execute(
        """
        SELECT s.source_id
        FROM artifact_sources ars
        JOIN sources s ON s.source_id = ars.source_id
        WHERE ars.artifact_id = ? AND (s.stale = 1 OR s.matter_scope != ?)
        LIMIT 1
        """,
        (artifact_id, matter_scope),
    ).fetchone()
    if stale_source is not None:
        return ReuseDecision(False, f"artifact source dependency stale or cross-matter: {stale_source['source_id']}", orientation_allowed=False)
    return ReuseDecision(True, "artifact current", trusted_as_proof=True)


def _candidate_reuse_decision(conn: sqlite3.Connection, *, matter_scope: str, candidate_id: str) -> ReuseDecision:
    row = conn.execute(
        """
        SELECT co.status, t.matter_scope
        FROM candidate_outputs co
        JOIN tasks t ON t.task_id = co.task_id
        WHERE co.candidate_id = ?
        """,
        (candidate_id,),
    ).fetchone()
    if row is None:
        return ReuseDecision(False, "linked candidate missing", orientation_allowed=False)
    if str(row["matter_scope"]) != matter_scope:
        return ReuseDecision(False, "linked candidate belongs to another matter", orientation_allowed=False)
    if str(row["status"]) != "reduced":
        return ReuseDecision(False, "candidate-only output is orientation-only and not trusted reusable work", orientation_allowed=True)
    accepted = conn.execute(
        "SELECT 1 FROM reducer_packets WHERE candidate_id = ? AND decision = 'accepted' LIMIT 1",
        (candidate_id,),
    ).fetchone()
    if accepted is None:
        return ReuseDecision(False, "reduced candidate lacks accepted reducer packet", orientation_allowed=True)
    return ReuseDecision(True, "candidate was reduced and accepted", trusted_as_proof=False)


def _same_matter_target_decision(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    target_type: str,
    target_id: str,
) -> ReuseDecision:
    target_matter = repo.matter_scope_for_target(conn, target_type=target_type, target_id=target_id)
    if target_matter is None:
        return ReuseDecision(False, f"linked {target_type} missing", orientation_allowed=False)
    if target_matter != matter_scope:
        return ReuseDecision(False, f"linked {target_type} belongs to another matter", orientation_allowed=False)
    return ReuseDecision(True, f"linked {target_type} is same-matter", trusted_as_proof=False)


def _context_pack_reuse_decision(conn: sqlite3.Connection, *, matter_scope: str, context_pack_id: str) -> ReuseDecision:
    row = conn.execute(
        "SELECT matter_scope, sections_json FROM context_packs WHERE context_pack_id = ?",
        (context_pack_id,),
    ).fetchone()
    if row is None:
        return ReuseDecision(False, "linked context_pack missing", orientation_allowed=False)
    if str(row["matter_scope"]) != matter_scope:
        return ReuseDecision(False, "linked context_pack belongs to another matter", orientation_allowed=False)
    try:
        sections_raw = json.loads(str(row["sections_json"] or "[]"))
        if not isinstance(sections_raw, list):
            return ReuseDecision(False, "context_pack sections JSON is not an array", orientation_allowed=False)
        sections = cast(list[object], sections_raw)
    except (json.JSONDecodeError, TypeError):
        return ReuseDecision(False, "context_pack sections are not valid JSON", orientation_allowed=False)
    problems: list[str] = []
    for material in _source_materials_from_sections(sections):
        source_id = str(material.get("source_id") or "")
        if not source_id:
            continue
        source = conn.execute("SELECT matter_scope, sha256, stale FROM sources WHERE source_id = ?", (source_id,)).fetchone()
        if source is None:
            problems.append(f"source missing: {source_id}")
            continue
        if str(source["matter_scope"]) != matter_scope:
            problems.append(f"source cross-matter: {source_id}")
        if int(source["stale"] or 0):
            problems.append(f"source stale: {source_id}")
        expected_sha = _source_sha_from_material(material)
        if expected_sha and expected_sha != str(source["sha256"] or ""):
            problems.append(f"source hash changed: {source_id}")
        artifact_id = str(material.get("artifact_id") or "")
        if artifact_id:
            artifact = conn.execute("SELECT matter_scope, sha256, stale FROM artifacts WHERE artifact_id = ?", (artifact_id,)).fetchone()
            if artifact is None:
                problems.append(f"extraction artifact missing: {artifact_id}")
            elif str(artifact["matter_scope"]) != matter_scope:
                problems.append(f"extraction artifact cross-matter: {artifact_id}")
            elif int(artifact["stale"] or 0):
                problems.append(f"extraction artifact stale: {artifact_id}")
            else:
                expected_text_sha = _text_sha_from_material(material)
                if expected_text_sha and expected_text_sha != str(artifact["sha256"] or ""):
                    problems.append(f"extraction artifact hash changed: {artifact_id}")
    if problems:
        return ReuseDecision(False, "; ".join(problems), orientation_allowed=False)
    return ReuseDecision(True, "context pack source material is same-matter and current", trusted_as_proof=False)


def _source_materials_from_sections(sections: list[object]) -> list[Mapping[str, object]]:
    for section in sections:
        if not isinstance(section, Mapping) or str(section.get("name") or "") != "source_materials":
            continue
        content = section.get("content")
        if not isinstance(content, list):
            return []
        return [cast(Mapping[str, object], item) for item in content if isinstance(item, Mapping)]
    return []


def _source_sha_from_material(material: Mapping[str, object]) -> str:
    provenance = material.get("source_provenance")
    if isinstance(provenance, Mapping):
        return str(provenance.get("sha256") or "")
    return str(material.get("source_sha256") or "")


def _text_sha_from_material(material: Mapping[str, object]) -> str:
    provenance = material.get("extraction_provenance")
    if isinstance(provenance, Mapping):
        return str(provenance.get("text_sha256") or "")
    return str(material.get("sha256") or "")


def _optional_id(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    return str(value) if value is not None and str(value) else ""


def _rank(goal: str, rows: list[dict[str, object]], *, id_key: str) -> list[dict[str, object]]:
    scored: list[tuple[float, str, dict[str, object]]] = []
    for row in rows:
        text = " ".join(str(value) for value in row.values())
        score = lexical_score(goal, text) if goal else 1.0
        if score <= 0 and goal:
            continue
        scored.append((score, str(row.get(id_key) or ""), row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [row for _, _, row in scored[:10]]
