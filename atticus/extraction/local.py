"""Local, no-provider source extraction and OCR repair.

This module converts matter-local source files into candidate extracted-text
artifacts and durable extraction/OCR coverage rows. It never calls model
providers and never creates worker candidates, provider runs, or leases.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from html import unescape
from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import cast
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from atticus.core.events import utc_now
from atticus.core.policies import LegalStage, TrustStatus
from atticus.db import repo
from atticus.workers.contracts import safe_path_component


IMAGE_SUFFIXES = {".jpeg", ".jpg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
DOCX_SUFFIXES = {".docx", ".dotx"}
DOC_SUFFIXES = {".doc", ".dot"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".json", ".xml", ".html", ".htm", ".rtf"}


@dataclass(frozen=True)
class ExtractedText:
    text: str
    method: str
    metadata: dict[str, object] = field(default_factory=dict)
    ocr_engine: str | None = None


@dataclass(frozen=True)
class LocalExtractionResult:
    dry_run: bool
    matter_scope: str
    workspace: str
    sources_selected: int = 0
    sources_extracted: int = 0
    sources_skipped: int = 0
    already_covered: int = 0
    artifacts_created: int = 0
    extraction_records_created: int = 0
    ocr_records_created: int = 0
    would_create_artifacts: int = 0
    human_attention_created: int = 0
    outputs: list[dict[str, object]] = field(default_factory=list)
    skipped: list[dict[str, object]] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "matter_scope": self.matter_scope,
            "workspace": self.workspace,
            "sources_selected": self.sources_selected,
            "sources_extracted": self.sources_extracted,
            "sources_skipped": self.sources_skipped,
            "already_covered": self.already_covered,
            "artifacts_created": self.artifacts_created,
            "extraction_records_created": self.extraction_records_created,
            "ocr_records_created": self.ocr_records_created,
            "would_create_artifacts": self.would_create_artifacts,
            "human_attention_created": self.human_attention_created,
            "outputs": self.outputs,
            "skipped": self.skipped,
        }


class ExtractionUnavailable(RuntimeError):
    """Raised when a local source cannot be safely extracted."""


def repair_source_extractions(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    workspace: str | Path,
    source_ids: Iterable[str] = (),
    dry_run: bool = True,
    timeout_seconds: float = 90.0,
) -> LocalExtractionResult:
    """Repair missing extraction/OCR coverage for matter-local sources."""

    matter_scope = matter_scope.strip()
    if not matter_scope:
        raise ValueError("matter_scope is required")
    workspace_path = Path(workspace).expanduser().resolve()
    if not workspace_path.exists() or not workspace_path.is_dir():
        raise ValueError(f"workspace does not exist: {workspace_path}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")

    rows = _select_sources(conn, matter_scope=matter_scope, source_ids=tuple(source_ids))
    outputs: list[dict[str, object]] = []
    skipped: list[dict[str, object]] = []
    sources_extracted = 0
    sources_skipped = 0
    already_covered = 0
    artifacts_created = 0
    extraction_records_created = 0
    ocr_records_created = 0
    would_create_artifacts = 0
    human_attention_created = 0

    for row in rows:
        source_id = str(row["source_id"])
        if _has_coverage(conn, source_id=source_id, source_sha256=str(row["sha256"])):
            already_covered += 1
            skipped.append({"source_id": source_id, "reason": "already covered"})
            continue

        source_path = _resolve_source_path(workspace_path, str(row["path"]))
        if not source_path.exists() or not source_path.is_file():
            sources_skipped += 1
            item: dict[str, object] = {"source_id": source_id, "path": str(source_path), "reason": "source file missing"}
            skipped.append(item)
            if not dry_run:
                human_attention_created += _record_attention_once(
                    conn,
                    target_id=source_id,
                    reason=f"source extraction skipped: source file missing: {source_path}",
                )
            continue
        if not _is_relative_to(source_path, workspace_path):
            sources_skipped += 1
            item = {"source_id": source_id, "path": str(source_path), "reason": "source outside workspace"}
            skipped.append(item)
            if not dry_run:
                human_attention_created += _record_attention_once(
                    conn,
                    target_id=source_id,
                    reason=f"source extraction skipped: source outside matter workspace: {source_path}",
                )
            continue

        try:
            extracted = extract_text_from_path(
                source_path,
                source_id=source_id,
                workspace=workspace_path,
                timeout_seconds=timeout_seconds,
            )
        except ExtractionUnavailable as exc:
            sources_skipped += 1
            item = {"source_id": source_id, "path": str(source_path), "reason": str(exc)}
            skipped.append(item)
            if not dry_run:
                human_attention_created += _record_attention_once(
                    conn,
                    target_id=source_id,
                    reason=f"source extraction skipped: {exc}",
                )
            continue

        output_path = workspace_path / "03-working" / "extracted-text" / f"{safe_path_component(source_id)}.txt"
        normalized_text = _normalize_text(extracted.text)
        if not normalized_text.strip():
            sources_skipped += 1
            item = {"source_id": source_id, "path": str(source_path), "reason": "no extractable text"}
            skipped.append(item)
            if not dry_run:
                human_attention_created += _record_attention_once(
                    conn,
                    target_id=source_id,
                    reason="source extraction skipped: no extractable text",
                )
            continue

        content_hash = _hash_text(normalized_text)
        artifact_id = _existing_extracted_artifact(
            conn,
            source_id=source_id,
            output_path=output_path,
            content_hash=content_hash,
        )
        if dry_run:
            would_create_artifacts += 0 if artifact_id else 1
        else:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(normalized_text, encoding="utf-8")
            if artifact_id is None:
                new_artifact_id = _available_extracted_artifact_id(conn, source_id=source_id, content_hash=content_hash)
                artifact_id = repo.add_artifact(
                    conn,
                    artifact_id=new_artifact_id,
                    matter_scope=matter_scope,
                    path=str(output_path),
                    artifact_type="extracted_text",
                    stage=LegalStage.S1_EXTRACTION,
                    trust_status=TrustStatus.CANDIDATE,
                    sha256=content_hash,
                    title=f"{source_id} extracted text",
                    content=normalized_text[:200_000],
                    source_ids=[source_id],
                )
                artifacts_created += 1
            extraction_records_created += _ensure_extraction_record(
                conn,
                source_id=source_id,
                artifact_id=artifact_id,
                method=extracted.method,
                confidence=_confidence_for_method(extracted.method),
                metadata={
                    **extracted.metadata,
                    "extracted_by": "atticus.local_extraction",
                    "extractor_tool": extracted.metadata.get("extractor") or extracted.method,
                    "source_id": source_id,
                    "source_path": str(source_path),
                    "source_sha256": row["sha256"],
                    "output_path": str(output_path),
                    "text_sha256": content_hash,
                },
            )
            if extracted.ocr_engine:
                ocr_records_created += _ensure_ocr_record(
                    conn,
                    source_id=source_id,
                    artifact_id=artifact_id,
                    engine=extracted.ocr_engine,
                    metadata={
                        **extracted.metadata,
                        "confidence": _confidence_for_method(extracted.method),
                        "extracted_by": "atticus.local_extraction",
                        "extractor_tool": extracted.ocr_engine or extracted.metadata.get("extractor") or extracted.method,
                        "source_id": source_id,
                        "source_path": str(source_path),
                        "source_sha256": row["sha256"],
                        "output_path": str(output_path),
                        "text_sha256": content_hash,
                    },
                )
            _ = repo.emit_event(
                conn,
                "source.extracted",
                matter_scope=matter_scope,
                payload={
                    "source_id": source_id,
                    "artifact_id": artifact_id,
                    "method": extracted.method,
                    "output_path": str(output_path),
                    "text_sha256": content_hash,
                },
            )
        sources_extracted += 1
        outputs.append(
            {
                "source_id": source_id,
                "source_path": str(source_path),
                "output_path": str(output_path),
                "method": extracted.method,
                "ocr_engine": extracted.ocr_engine,
                "text_sha256": content_hash,
                "text_bytes": len(normalized_text.encode("utf-8")),
                "artifact_id": artifact_id,
            }
        )

    return LocalExtractionResult(
        dry_run=dry_run,
        matter_scope=matter_scope,
        workspace=str(workspace_path),
        sources_selected=len(rows),
        sources_extracted=sources_extracted,
        sources_skipped=sources_skipped,
        already_covered=already_covered,
        artifacts_created=artifacts_created,
        extraction_records_created=extraction_records_created,
        ocr_records_created=ocr_records_created,
        would_create_artifacts=would_create_artifacts,
        human_attention_created=human_attention_created,
        outputs=outputs,
        skipped=skipped,
    )


def extract_text_from_path(
    path: Path,
    *,
    source_id: str,
    workspace: Path,
    timeout_seconds: float,
) -> ExtractedText:
    suffix = path.suffix.lower()
    if suffix in DOCX_SUFFIXES:
        return ExtractedText(_extract_docx(path), "docx_text", {"extractor": "python-docx-zip"})
    if suffix == ".pdf" or _is_pdf(path):
        return ExtractedText(_extract_pdf(path, timeout_seconds=timeout_seconds), "pdf_text", {"extractor": "pdftotext"})
    if suffix in DOC_SUFFIXES:
        text, method = _extract_legacy_doc(path, timeout_seconds=timeout_seconds)
        return ExtractedText(text, method, {"extractor": method})
    if suffix in IMAGE_SUFFIXES:
        return _extract_image(path, source_id=source_id, workspace=workspace, timeout_seconds=timeout_seconds)
    if suffix in TEXT_SUFFIXES or _looks_textual(path):
        text = _read_text_fallback(path)
        if suffix in {".html", ".htm"} or _looks_html(text):
            text = _html_to_text(text)
        return ExtractedText(text, "plain_text", {"extractor": "utf8_text"})
    raise ExtractionUnavailable(f"unsupported source type: {suffix or 'no extension'}")


def _select_sources(
    conn: sqlite3.Connection,
    *,
    matter_scope: str,
    source_ids: tuple[str, ...],
) -> list[Mapping[str, object]]:
    if source_ids:
        placeholders = ",".join("?" for _ in source_ids)
        rows = conn.execute(
            f"""
            SELECT *
            FROM sources
            WHERE matter_scope = ? AND source_id IN ({placeholders})
            ORDER BY source_id
            """,
            (matter_scope, *source_ids),
        ).fetchall()
        found = {str(row["source_id"]) for row in rows}
        missing = sorted(set(source_ids) - found)
        if missing:
            raise KeyError(f"source ids not found for {matter_scope}: {', '.join(missing)}")
        return [cast(Mapping[str, object], row) for row in rows]
    rows = conn.execute(
        """
        SELECT s.*
        FROM sources s
        WHERE s.matter_scope = ?
        ORDER BY s.source_id
        """,
        (matter_scope,),
    ).fetchall()
    return [cast(Mapping[str, object], row) for row in rows]


def _has_coverage(conn: sqlite3.Connection, *, source_id: str, source_sha256: str) -> bool:
    return _has_current_complete_coverage(conn, source_id=source_id, source_sha256=source_sha256)


def _has_current_complete_coverage(conn: sqlite3.Connection, *, source_id: str, source_sha256: str) -> bool:
    for table, id_column in (
        ("extraction_records", "extraction_id"),
        ("ocr_records", "ocr_id"),
        ("transcription_records", "transcription_id"),
    ):
        if _has_current_complete_coverage_for_table(conn, source_id=source_id, source_sha256=source_sha256, table=table, id_column=id_column):
            return True
    return False


def _has_current_complete_coverage_for_table(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    source_sha256: str,
    table: str,
    id_column: str,
) -> bool:
    row = conn.execute(
        f"""
        SELECT {id_column}
        FROM {table} record
        JOIN artifacts a ON a.artifact_id = record.artifact_id
        WHERE record.source_id = ?
          AND record.coverage_status = 'complete'
          AND a.stale = 0
          AND json_extract(record.metadata_json, '$.source_sha256') = ?
        LIMIT 1
        """,
        (source_id, source_sha256),
    ).fetchone()
    return row is not None


def _resolve_source_path(workspace: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (workspace / path).resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _extract_docx(path: Path) -> str:
    try:
        with ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name == "word/document.xml"
                or name.startswith("word/header")
                or name.startswith("word/footer")
                or name.startswith("word/footnotes")
                or name.startswith("word/endnotes")
            ]
            if not names:
                raise ExtractionUnavailable("docx has no document XML")
            sections = [_extract_docx_xml(archive.read(name)) for name in names]
    except BadZipFile as exc:
        raise ExtractionUnavailable(f"docx is not a readable zip package: {exc}") from exc
    return "\n\n".join(section for section in sections if section.strip())


def _extract_docx_xml(data: bytes) -> str:
    root = ElementTree.fromstring(data)
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs: list[str] = []
    for para in root.findall(".//w:p", namespace):
        parts: list[str] = []
        for node in para.iter():
            tag = node.tag.rsplit("}", 1)[-1]
            if tag == "t" and node.text:
                parts.append(node.text)
            elif tag == "tab":
                parts.append("\t")
            elif tag == "br":
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    if paragraphs:
        return "\n".join(paragraphs)
    return "\n".join(text.strip() for text in root.itertext() if text.strip())


def _extract_pdf(path: Path, *, timeout_seconds: float) -> str:
    if shutil.which("pdftotext") is None:
        raise ExtractionUnavailable("pdftotext is not available for PDF extraction")
    completed = _run_local_command(
        ["pdftotext", "-layout", "-enc", "UTF-8", str(path), "-"],
        timeout_seconds=timeout_seconds,
    )
    return completed.stdout


def _extract_legacy_doc(path: Path, *, timeout_seconds: float) -> tuple[str, str]:
    if shutil.which("libreoffice") is not None or shutil.which("soffice") is not None:
        binary = shutil.which("libreoffice") or shutil.which("soffice")
        assert binary is not None
        with tempfile.TemporaryDirectory(prefix="atticus-doc-extract-") as tmp:
            _ = _run_local_command(
                [
                    binary,
                    "--headless",
                    "--convert-to",
                    "txt:Text",
                    "--outdir",
                    tmp,
                    str(path),
                ],
                timeout_seconds=timeout_seconds,
            )
            converted = sorted(Path(tmp).glob("*.txt"))
            if converted:
                return _read_text_fallback(converted[0]), "libreoffice_text"
    if shutil.which("pandoc") is not None:
        completed = _run_local_command(["pandoc", str(path), "-t", "plain"], timeout_seconds=timeout_seconds)
        return completed.stdout, "pandoc_text"
    raise ExtractionUnavailable("no local .doc extractor available")


def _extract_image(path: Path, *, source_id: str, workspace: Path, timeout_seconds: float) -> ExtractedText:
    existing_ocr = workspace / "03-working" / "ocr" / f"{safe_path_component(source_id)}.txt"
    if existing_ocr.exists():
        return ExtractedText(
            _read_text_fallback(existing_ocr),
            "existing_ocr_text",
            {"extractor": "existing_ocr_text", "ocr_path": str(existing_ocr)},
            ocr_engine="existing_text",
        )
    if shutil.which("tesseract") is None:
        raise ExtractionUnavailable("tesseract is not available for image OCR")
    completed = _run_local_command(["tesseract", str(path), "stdout"], timeout_seconds=timeout_seconds)
    return ExtractedText(
        completed.stdout,
        "tesseract_ocr",
        {"extractor": "tesseract"},
        ocr_engine="tesseract",
    )


def _run_local_command(args: list[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionUnavailable(f"local extractor timed out after {timeout_seconds:g}s: {args[0]}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().replace("\n", " ")[:400]
        raise ExtractionUnavailable(f"local extractor failed: {args[0]} exited {completed.returncode}: {detail}")
    return completed


def _is_pdf(path: Path) -> bool:
    try:
        return path.read_bytes()[:5] == b"%PDF-"
    except OSError:
        return False


def _looks_textual(path: Path) -> bool:
    try:
        sample = path.read_bytes()[:4096]
    except OSError:
        return False
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _read_text_fallback(path: Path) -> str:
    data = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"p", "br", "div", "li", "tr"}:
            self.parts.append("\n")


def _html_to_text(text: str) -> str:
    parser = _TextExtractor()
    parser.feed(text)
    return unescape("\n".join(part for part in parser.parts if part.strip()))


def _looks_html(text: str) -> bool:
    prefix = text.lstrip().lower()[:200]
    return prefix.startswith("<!doctype html") or prefix.startswith("<html") or "<body" in prefix


def _normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"\n{4,}", "\n\n\n", normalized)
    return normalized.strip() + "\n"


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _existing_extracted_artifact(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    output_path: Path,
    content_hash: str,
) -> str | None:
    row = conn.execute(
        """
        SELECT a.artifact_id
        FROM artifacts a
        JOIN artifact_sources src ON src.artifact_id = a.artifact_id
        WHERE src.source_id = ?
          AND a.artifact_type = 'extracted_text'
          AND a.path = ?
          AND a.sha256 = ?
        LIMIT 1
        """,
        (source_id, str(output_path), content_hash),
    ).fetchone()
    return str(row["artifact_id"]) if row is not None else None


def _stable_artifact_id(source_id: str) -> str:
    return f"art-extracted-{safe_path_component(source_id)}"


def _available_extracted_artifact_id(conn: sqlite3.Connection, *, source_id: str, content_hash: str) -> str:
    stable = _stable_artifact_id(source_id)
    if conn.execute("SELECT 1 FROM artifacts WHERE artifact_id = ?", (stable,)).fetchone() is None:
        return stable
    return f"{stable}-{content_hash[:12]}"


def _ensure_extraction_record(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    artifact_id: str | None,
    method: str,
    confidence: float,
    metadata: dict[str, object],
) -> int:
    source_sha256 = str(metadata.get("source_sha256") or "")
    if source_sha256 and _has_current_complete_coverage_for_table(
        conn,
        source_id=source_id,
        source_sha256=source_sha256,
        table="extraction_records",
        id_column="extraction_id",
    ):
        return 0
    _ = conn.execute(
        """
        INSERT INTO extraction_records(extraction_id, source_id, artifact_id, method,
          coverage_status, confidence, metadata_json, created_at)
        VALUES (?, ?, ?, ?, 'complete', ?, ?, ?)
        """,
        (f"extract-{uuid4().hex}", source_id, artifact_id, method, confidence, _json(metadata), utc_now()),
    )
    return 1


def _ensure_ocr_record(
    conn: sqlite3.Connection,
    *,
    source_id: str,
    artifact_id: str | None,
    engine: str,
    metadata: dict[str, object],
) -> int:
    source_sha256 = str(metadata.get("source_sha256") or "")
    if source_sha256 and _has_current_complete_coverage_for_table(
        conn,
        source_id=source_id,
        source_sha256=source_sha256,
        table="ocr_records",
        id_column="ocr_id",
    ):
        return 0
    _ = conn.execute(
        """
        INSERT INTO ocr_records(ocr_id, source_id, artifact_id, engine,
          page_count, coverage_status, metadata_json, created_at)
        VALUES (?, ?, ?, ?, 0, 'complete', ?, ?)
        """,
        (f"ocr-{uuid4().hex}", source_id, artifact_id, engine, _json(metadata), utc_now()),
    )
    return 1


def _record_attention_once(conn: sqlite3.Connection, *, target_id: str, reason: str) -> int:
    exists = conn.execute(
        """
        SELECT 1
        FROM human_attention
        WHERE target_type = 'source'
          AND target_id = ?
          AND reason = ?
          AND status = 'open'
        LIMIT 1
        """,
        (target_id, reason),
    ).fetchone()
    if exists:
        return 0
    _ = repo.record_human_attention(
        conn,
        target_type="source",
        target_id=target_id,
        severity="warning",
        reason=reason,
    )
    return 1


def _confidence_for_method(method: str) -> float:
    if method in {"docx_text", "pdf_text", "plain_text", "libreoffice_text", "pandoc_text"}:
        return 0.85
    if method == "existing_ocr_text":
        return 0.75
    if method == "tesseract_ocr":
        return 0.65
    return 0.5


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
