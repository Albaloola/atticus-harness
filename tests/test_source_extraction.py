from __future__ import annotations

from collections.abc import Mapping
import json
from pathlib import Path
import sqlite3
from typing import cast
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from atticus.cli import main as cli_main
from atticus.core.policies import LegalStage
from atticus.db import repo


def init_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "atticus.sqlite3"
    repo.initialize_database(db_path)
    return db_path


def _count(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> int:
    row = conn.execute(sql, params).fetchone()
    assert row is not None
    return int(str(row[0]))


def _write_minimal_docx(path: Path, paragraphs: list[str]) -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        for text in paragraphs
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body>"
        "</w:document>"
    )
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        archive.writestr("word/document.xml", document_xml)


def _add_source(conn: sqlite3.Connection, *, matter: str, source_id: str, path: Path, source_type: str = "fixture") -> None:
    _ = repo.add_source(
        conn,
        source_id=source_id,
        matter_scope=matter,
        path=str(path),
        source_type=source_type,
        stage=LegalStage.S0_SOURCE_INVENTORY,
        sha256="a" * 64,
    )


def test_extract_sources_dry_run_does_not_write_files_or_rows(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "matter"
    source_path = workspace / "01-sources" / "SRC-0001 - fixture.docx"
    source_path.parent.mkdir(parents=True)
    _write_minimal_docx(source_path, ["rent difficulty disclosed"])
    with repo.db_connection(db_path) as conn:
        _add_source(conn, matter="alpha", source_id="SRC-0001", path=source_path)

    code = cli_main(
        [
            "extract-sources",
            "--db",
            str(db_path),
            "--matter",
            "alpha",
            "--workspace",
            str(workspace),
        ]
    )
    output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))

    assert code == 0
    assert output["dry_run"] is True
    assert output["sources_selected"] == 1
    assert output["artifacts_created"] == 0
    assert output["would_create_artifacts"] == 1
    assert not (workspace / "03-working" / "extracted-text" / "SRC-0001.txt").exists()
    with repo.db_connection(db_path) as conn:
        assert _count(conn, "SELECT COUNT(*) FROM artifacts WHERE matter_scope = 'alpha'") == 0
        assert _count(conn, "SELECT COUNT(*) FROM extraction_records") == 0
        assert _count(conn, "SELECT COUNT(*) FROM provider_runs") == 0
        assert _count(conn, "SELECT COUNT(*) FROM candidate_outputs") == 0
        assert _count(conn, "SELECT COUNT(*) FROM leases") == 0


def test_extract_sources_write_extracts_docx_and_is_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "matter"
    source_path = workspace / "01-sources" / "SRC-0002 - fixture.docx"
    source_path.parent.mkdir(parents=True)
    _write_minimal_docx(source_path, ["Course risk", "Accommodation arrears"])
    with repo.db_connection(db_path) as conn:
        _add_source(conn, matter="alpha", source_id="SRC-0002", path=source_path)

    args = [
        "extract-sources",
        "--db",
        str(db_path),
        "--matter",
        "alpha",
        "--workspace",
        str(workspace),
        "--write",
    ]
    assert cli_main(args) == 0
    first = cast(Mapping[str, object], json.loads(capsys.readouterr().out))
    assert cli_main(args) == 0
    second = cast(Mapping[str, object], json.loads(capsys.readouterr().out))

    output_path = workspace / "03-working" / "extracted-text" / "SRC-0002.txt"
    assert output_path.exists()
    assert "Course risk" in output_path.read_text(encoding="utf-8")
    assert first["artifacts_created"] == 1
    assert first["extraction_records_created"] == 1
    assert second["artifacts_created"] == 0
    assert second["extraction_records_created"] == 0
    assert second["already_covered"] == 1
    with repo.db_connection(db_path) as conn:
        artifact = cast(Mapping[str, object], conn.execute("SELECT * FROM artifacts WHERE matter_scope = 'alpha'").fetchone())
        extraction = cast(Mapping[str, object], conn.execute("SELECT metadata_json FROM extraction_records WHERE source_id = 'SRC-0002'").fetchone())
        extraction_metadata = cast(Mapping[str, object], json.loads(str(extraction["metadata_json"])))
        assert artifact["artifact_type"] == "extracted_text"
        assert artifact["stage"] == "S1"
        assert artifact["trust_status"] == "candidate"
        assert extraction_metadata["extracted_by"] == "atticus.local_extraction"
        assert extraction_metadata["extractor_tool"] == "python-docx-zip"
        assert extraction_metadata["source_id"] == "SRC-0002"
        assert extraction_metadata["source_sha256"] == "a" * 64
        assert str(extraction_metadata["output_path"]).endswith("03-working/extracted-text/SRC-0002.txt")
        assert _count(conn, "SELECT COUNT(*) FROM artifact_sources WHERE source_id = 'SRC-0002'") == 1
        assert _count(conn, "SELECT COUNT(*) FROM extraction_records WHERE source_id = 'SRC-0002'") == 1
        assert _count(conn, "SELECT COUNT(*) FROM provider_runs") == 0
        assert _count(conn, "SELECT COUNT(*) FROM candidate_outputs") == 0
        assert _count(conn, "SELECT COUNT(*) FROM leases") == 0


def test_extract_sources_registers_existing_ocr_text_for_image(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "matter"
    image_path = workspace / "01-sources" / "SRC-IMG - photo.jpeg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"not a real jpeg; existing OCR should be used")
    ocr_path = workspace / "03-working" / "ocr" / "SRC-IMG.txt"
    ocr_path.parent.mkdir(parents=True)
    ocr_path.write_text("OCR text already created locally", encoding="utf-8")
    with repo.db_connection(db_path) as conn:
        _add_source(conn, matter="alpha", source_id="SRC-IMG", path=image_path, source_type="image")

    assert cli_main(
        [
            "extract-sources",
            "--db",
            str(db_path),
            "--matter",
            "alpha",
            "--workspace",
            str(workspace),
            "--source-id",
            "SRC-IMG",
            "--write",
        ]
    ) == 0
    output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))

    assert output["ocr_records_created"] == 1
    extracted = workspace / "03-working" / "extracted-text" / "SRC-IMG.txt"
    assert extracted.read_text(encoding="utf-8") == "OCR text already created locally\n"
    with repo.db_connection(db_path) as conn:
        ocr = cast(Mapping[str, object], conn.execute("SELECT * FROM ocr_records WHERE source_id = 'SRC-IMG'").fetchone())
        extraction = cast(Mapping[str, object], conn.execute("SELECT * FROM extraction_records WHERE source_id = 'SRC-IMG'").fetchone())
    assert ocr["engine"] == "existing_text"
    assert extraction["method"] == "existing_ocr_text"


def test_extract_sources_missing_file_reports_attention_without_crashing(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    db_path = init_db(tmp_path)
    workspace = tmp_path / "matter"
    workspace.mkdir()
    with repo.db_connection(db_path) as conn:
        _add_source(conn, matter="alpha", source_id="SRC-MISSING", path=workspace / "missing.docx")

    assert cli_main(
        [
            "extract-sources",
            "--db",
            str(db_path),
            "--matter",
            "alpha",
            "--workspace",
            str(workspace),
            "--write",
        ]
    ) == 0
    output = cast(Mapping[str, object], json.loads(capsys.readouterr().out))

    assert output["sources_skipped"] == 1
    skipped = cast(list[Mapping[str, object]], output["skipped"])
    assert skipped[0]["reason"] == "source file missing"
    assert output["human_attention_created"] == 1
    with repo.db_connection(db_path) as conn:
        assert _count(conn, "SELECT COUNT(*) FROM human_attention WHERE target_id = 'SRC-MISSING'") == 1
        assert _count(conn, "SELECT COUNT(*) FROM extraction_records") == 0
