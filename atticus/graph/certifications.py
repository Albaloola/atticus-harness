"""Certification graph helpers."""

from __future__ import annotations

import sqlite3

from typing import cast
from atticus.db import repo


class CertificationBlocked(RuntimeError):
    """Raised when a certification lacks a passing validation gate."""


def certify_subject(
    conn: sqlite3.Connection,
    *,
    subject_type: str,
    subject_id: str,
    certification_type: str,
    validator: str,
    evidence: dict[str, object] | None = None,
) -> str:
    validation = cast(sqlite3.Row | None, cast(object, conn.execute(
        """
        SELECT validation_result_id
        FROM validation_results
        WHERE target_type = ? AND target_id = ? AND gate_name = ? AND passed = 1
        ORDER BY validation_result_id DESC
        LIMIT 1
        """,
        (subject_type, subject_id, certification_type),
    ).fetchone()))
    if validation is None:
        raise CertificationBlocked(
            f"certification {certification_type!r} for {subject_type}:{subject_id} requires passing validation"
        )
    return repo.add_certification(
        conn,
        subject_type=subject_type,
        subject_id=subject_id,
        certification_type=certification_type,
        validator=validator,
        validation_result_id=int(str(validation["validation_result_id"])),
        evidence=evidence,
    )
