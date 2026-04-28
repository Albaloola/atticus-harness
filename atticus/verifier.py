"""Independent verifier and hostile-review checks for candidate packets."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import replace
import json
import re
import sqlite3
from typing import cast

from atticus.db import repo
from atticus.workers.result_parser import ParsedResultPacket, parse_result

EXTERNAL_ACTION_RE = re.compile(r"\b(i|we|atticus)\s+(have\s+)?(emailed|sent|filed|served|uploaded|contacted)\b", re.I)
VERIFIER_TYPES = frozenset(
    {
        "citation_audit",
        "authority_audit",
        "factual_support_audit",
        "procedural_audit",
        "hostile_opponent_review",
        "privacy_redaction_audit",
        "chronology_consistency_audit",
        "remedy_support_audit",
    }
)


@dataclass(frozen=True)
class VerifierResult:
    verifier_type: str
    candidate_id: str
    passed: bool
    checked_items: list[dict[str, object]]
    unsupported_claims: list[dict[str, object]]
    weak_claims: list[dict[str, object]]
    citation_defects: list[dict[str, object]]
    authority_defects: list[dict[str, object]]
    procedural_defects: list[dict[str, object]]
    overstatement_risks: list[dict[str, object]]
    privacy_redaction_concerns: list[dict[str, object]]
    recommended_fixes: list[str]
    confidence: float
    validation_result_id: int | None = None

    @property
    def defect_types(self) -> list[str]:
        types: list[str] = []
        for bucket in (
            self.unsupported_claims,
            self.weak_claims,
            self.citation_defects,
            self.authority_defects,
            self.procedural_defects,
            self.overstatement_risks,
            self.privacy_redaction_concerns,
        ):
            for item in bucket:
                defect_type = str(item.get("type") or "")
                if defect_type and defect_type not in types:
                    types.append(defect_type)
        return types

    def as_dict(self) -> dict[str, object]:
        return {
            "verifier_type": self.verifier_type,
            "candidate_id": self.candidate_id,
            "passed": self.passed,
            "checked_items": self.checked_items,
            "unsupported_claims": self.unsupported_claims,
            "weak_claims": self.weak_claims,
            "citation_defects": self.citation_defects,
            "authority_defects": self.authority_defects,
            "procedural_defects": self.procedural_defects,
            "overstatement_risks": self.overstatement_risks,
            "privacy_redaction_concerns": self.privacy_redaction_concerns,
            "recommended_fixes": self.recommended_fixes,
            "confidence": self.confidence,
            "validation_result_id": self.validation_result_id,
            "defect_types": self.defect_types,
        }


def verify_candidate(
    conn: sqlite3.Connection,
    *,
    candidate_id: str,
    verifier_type: str,
    write: bool = False,
) -> VerifierResult:
    if verifier_type not in VERIFIER_TYPES:
        raise ValueError(f"unknown verifier type: {verifier_type}")
    row = cast(Mapping[str, object] | None, conn.execute("SELECT * FROM candidate_outputs WHERE candidate_id = ?", (candidate_id,)).fetchone())
    if row is None:
        raise ValueError(f"unknown candidate: {candidate_id}")
    payload = json.loads(str(row["payload_json"]))
    if not isinstance(payload, Mapping):
        raise ValueError("candidate payload must be a JSON object")
    packet = parse_result({str(key): value for key, value in cast(Mapping[object, object], payload).items()})
    result = _verify_packet(candidate_id=candidate_id, verifier_type=verifier_type, packet=packet)
    validation_result_id: int | None = None
    if write:
        validation_result_id = repo.record_validation(
            conn,
            target_type="candidate",
            target_id=candidate_id,
            gate_name=f"verifier:{verifier_type}",
            passed=result.passed,
            details=result.as_dict(),
            severity="info" if result.passed else "error",
        )
    if validation_result_id is None:
        return result
    return replace(result, validation_result_id=validation_result_id)


def _verify_packet(*, candidate_id: str, verifier_type: str, packet: ParsedResultPacket) -> VerifierResult:
    checked: list[dict[str, object]] = []
    unsupported: list[dict[str, object]] = []
    weak: list[dict[str, object]] = []
    citation_defects: list[dict[str, object]] = []
    authority_defects: list[dict[str, object]] = []
    procedural_defects: list[dict[str, object]] = []
    overstatement: list[dict[str, object]] = []
    privacy: list[dict[str, object]] = []
    fixes: list[str] = []

    citation_ids = {str(citation["citation_id"]) for citation in packet.citations}
    for finding in packet.findings:
        checked.append({"type": "finding", "finding_id": finding["finding_id"]})
        finding_type = str(finding["finding_type"])
        ids = [str(item) for item in cast(list[object], finding["citation_ids"])]
        if finding_type in {"fact", "law", "procedure", "risk", "contradiction"} and not ids:
            unsupported.append({"type": "missing_citation", "finding_id": finding["finding_id"], "text": finding["text"]})
            fixes.append(f"Add a citation or mark finding {finding['finding_id']} as uncertain research.")
        missing = sorted(set(ids) - citation_ids)
        if missing:
            citation_defects.append({"type": "missing_citation_id", "finding_id": finding["finding_id"], "missing": missing})
        if float(str(finding["confidence"])) >= 0.95 and str(finding["reasoning_status"]) in {"inferred", "uncertain", "needs_research"}:
            overstatement.append({"type": "overconfident_uncertain_finding", "finding_id": finding["finding_id"]})

    for citation in packet.citations:
        checked.append({"type": "citation", "citation_id": citation["citation_id"]})
        if citation["target_type"] == "authority" and verifier_type in {"authority_audit", "hostile_opponent_review"}:
            locator = str(citation.get("locator") or "")
            if not locator:
                authority_defects.append({"type": "authority_locator_missing", "citation_id": citation["citation_id"]})

    for artifact in packet.proposed_artifacts:
        content = str(artifact.get("content") or "")
        checked.append({"type": "proposed_artifact", "path": artifact.get("path")})
        if EXTERNAL_ACTION_RE.search(content):
            procedural_defects.append({"type": "external_action_language", "path": artifact.get("path")})
            fixes.append("Replace any claim that Atticus filed, sent, served, uploaded, emailed, or contacted anyone with draft-only wording.")
        if verifier_type in {"privacy_redaction_audit", "hostile_opponent_review"} and any(term in content.lower() for term in ("password", "api key", "secret key")):
            privacy.append({"type": "possible_secret_or_credential", "path": artifact.get("path")})

    passed = not any((unsupported, weak, citation_defects, authority_defects, procedural_defects, overstatement, privacy))
    return VerifierResult(
        verifier_type=verifier_type,
        candidate_id=candidate_id,
        passed=passed,
        checked_items=checked,
        unsupported_claims=unsupported,
        weak_claims=weak,
        citation_defects=citation_defects,
        authority_defects=authority_defects,
        procedural_defects=procedural_defects,
        overstatement_risks=overstatement,
        privacy_redaction_concerns=privacy,
        recommended_fixes=fixes,
        confidence=0.75 if checked else 0.25,
    )
