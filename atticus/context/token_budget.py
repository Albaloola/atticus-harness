"""Local token estimation for source-material budgeting.

The harness uses provider-reported usage for billing/audit after a call. This
module is the pre-call counterpart: deterministic, local estimates that keep
large evidence sets from being sent to a model as one oversized blob.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import json
import sqlite3
from typing import cast


TEXT_DERIVATIVE_TYPES = {
    "extracted_text",
    "extraction_record",
    "ocr_extract",
    "ocr_text",
    "transcription_record",
    "transcript",
}
DEFAULT_BYTES_PER_TOKEN = 4
DENSE_TEXT_BYTES_PER_TOKEN = 2


@dataclass(frozen=True)
class SourceTokenEstimate:
    source_id: str
    path: str
    source_type: str
    artifact_id: str
    artifact_type: str
    available_chars: int
    estimated_tokens: int
    estimation_basis: str

    def as_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "path": self.path,
            "source_type": self.source_type,
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "available_chars": self.available_chars,
            "estimated_tokens": self.estimated_tokens,
            "estimation_basis": self.estimation_basis,
        }


def estimate_text_tokens(text: str, *, path: str = "") -> int:
    """Return a conservative local token estimate for one text-like payload."""

    if not text:
        return 0
    bytes_per_token = _bytes_per_token_for_path(path)
    return max(1, (len(text.encode("utf-8")) + bytes_per_token - 1) // bytes_per_token)


def estimate_json_tokens(value: object) -> int:
    material = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return estimate_text_tokens(material, path="payload.json")


def source_token_estimates(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_ids: Sequence[str],
) -> list[SourceTokenEstimate]:
    """Estimate available extracted/OCR text tokens for source IDs.

    Estimates are returned in the caller's source order. If a source has no text
    derivative yet, the estimate falls back to source byte size so unknown or
    binary-heavy files are isolated instead of silently packed into a large
    prompt.
    """

    ordered_source_ids = [str(source_id) for source_id in source_ids if str(source_id)]
    if not ordered_source_ids:
        return []
    placeholders = ",".join("?" for _ in ordered_source_ids)
    rows = conn.execute(
        f"""
        SELECT
          s.source_id,
          s.path AS source_path,
          s.source_type,
          s.size_bytes,
          a.artifact_id,
          a.path AS artifact_path,
          a.artifact_type,
          a.content
        FROM sources s
        LEFT JOIN artifact_sources af ON af.source_id = s.source_id
        LEFT JOIN artifacts a
          ON a.artifact_id = af.artifact_id
         AND a.matter_scope = s.matter_scope
         AND a.stale = 0
         AND a.artifact_type IN (
            'extracted_text',
            'extraction_record',
            'ocr_extract',
            'ocr_text',
            'transcription_record',
            'transcript'
         )
        WHERE s.matter_scope = ?
          AND s.source_id IN ({placeholders})
        ORDER BY
          s.source_id,
          CASE a.artifact_type
            WHEN 'extracted_text' THEN 0
            WHEN 'ocr_text' THEN 1
            WHEN 'ocr_extract' THEN 2
            WHEN 'transcript' THEN 3
            ELSE 4
          END,
          a.created_at DESC,
          a.artifact_id
        """,
        (matter_scope, *ordered_source_ids),
    ).fetchall()
    best_by_source: dict[str, Mapping[str, object]] = {}
    for row in rows:
        source_id = str(row["source_id"])
        if source_id not in best_by_source:
            best_by_source[source_id] = cast(Mapping[str, object], row)

    estimates: list[SourceTokenEstimate] = []
    for source_id in ordered_source_ids:
        row = best_by_source.get(source_id)
        if row is None:
            continue
        content = str(row["content"] or "")
        artifact_path = str(row["artifact_path"] or "")
        source_path = str(row["source_path"] or "")
        if content:
            available_chars = len(content)
            estimated_tokens = estimate_text_tokens(content, path=artifact_path or source_path)
            basis = "text_derivative"
        else:
            available_chars = 0
            estimated_tokens = max(1, (_int(_row_value(row, "size_bytes")) + DEFAULT_BYTES_PER_TOKEN - 1) // DEFAULT_BYTES_PER_TOKEN)
            basis = "source_size_fallback"
        estimates.append(
            SourceTokenEstimate(
                source_id=source_id,
                path=source_path,
                source_type=str(row["source_type"] or ""),
                artifact_id=str(row["artifact_id"] or ""),
                artifact_type=str(row["artifact_type"] or ""),
                available_chars=available_chars,
                estimated_tokens=estimated_tokens,
                estimation_basis=basis,
            )
        )
    return estimates


def source_token_estimates_by_id(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_ids: Sequence[str],
) -> dict[str, SourceTokenEstimate]:
    return {estimate.source_id: estimate for estimate in source_token_estimates(conn, matter_scope=matter_scope, source_ids=source_ids)}


def token_balanced_source_bundles(
    source_ids: Sequence[str],
    estimates: Sequence[SourceTokenEstimate],
    *,
    target_tokens: int,
    max_sources_per_bundle: int,
) -> list[list[str]]:
    """Pack sources into deterministic token-balanced bundles.

    Oversized documents become singleton bundles. Smaller documents are packed
    first-fit decreasing and then restored to source-order within each bundle so
    prompts remain easy to audit.
    """

    ordered_ids = [str(source_id) for source_id in source_ids if str(source_id)]
    order_index = {source_id: index for index, source_id in enumerate(ordered_ids)}
    estimate_by_id = {estimate.source_id: estimate for estimate in estimates}
    items = [
        (
            source_id,
            max(1, estimate_by_id.get(source_id, SourceTokenEstimate(source_id, "", "", "", "", 0, 1, "missing_estimate")).estimated_tokens),
        )
        for source_id in ordered_ids
    ]
    bundles: list[dict[str, object]] = []
    for source_id, tokens in sorted(items, key=lambda item: (-item[1], order_index[item[0]], item[0])):
        if tokens >= target_tokens:
            bundles.append({"source_ids": [source_id], "tokens": tokens})
            continue
        selected: dict[str, object] | None = None
        for bundle in bundles:
            bundle_sources = cast(list[str], bundle["source_ids"])
            bundle_tokens = int(bundle["tokens"])
            if len(bundle_sources) >= max_sources_per_bundle:
                continue
            if bundle_tokens + tokens > target_tokens:
                continue
            if selected is None or int(bundle["tokens"]) < int(selected["tokens"]):
                selected = bundle
        if selected is None:
            bundles.append({"source_ids": [source_id], "tokens": tokens})
        else:
            cast(list[str], selected["source_ids"]).append(source_id)
            selected["tokens"] = int(selected["tokens"]) + tokens
    bundle_source_ids = [cast(list[str], bundle["source_ids"]) for bundle in bundles]
    for bundle in bundle_source_ids:
        bundle.sort(key=lambda source_id: order_index[source_id])
    bundle_source_ids.sort(key=lambda bundle: order_index[bundle[0]] if bundle else 0)
    return bundle_source_ids


def bundle_token_total(bundle: Sequence[str], estimates: Sequence[SourceTokenEstimate]) -> int:
    estimate_by_id = {estimate.source_id: estimate.estimated_tokens for estimate in estimates}
    return sum(max(1, estimate_by_id.get(source_id, 1)) for source_id in bundle)


def _bytes_per_token_for_path(path: str) -> int:
    suffix = Path(path).suffix.lower().lstrip(".")
    if suffix in {"json", "jsonl", "jsonc"}:
        return DENSE_TEXT_BYTES_PER_TOKEN
    return DEFAULT_BYTES_PER_TOKEN


def _int(raw: object) -> int:
    try:
        return int(str(raw))
    except (TypeError, ValueError):
        return 0


def _row_value(row: Mapping[str, object], key: str, default: object = "") -> object:
    if hasattr(row, "keys") and key in row.keys():
        return row[key]
    return default
