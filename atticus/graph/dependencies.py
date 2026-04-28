"""Dependency helpers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from typing import cast



def task_dependencies(task_row: sqlite3.Row) -> tuple[list[str], list[str], list[dict[str, object]]]:
    return (
        _string_list_json(task_row["source_dependencies_json"], field="source_dependencies_json"),
        _string_list_json(task_row["artifact_dependencies_json"], field="artifact_dependencies_json"),
        _mapping_list_json(task_row["required_certifications_json"], field="required_certifications_json"),
    )


def _string_list_json(raw: object, *, field: str) -> list[str]:
    value = json.loads(str(raw or "[]"))
    if not isinstance(value, list):
        raise ValueError(f"{field} must contain a JSON array of strings")
    result: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str):
            raise ValueError(f"{field} must contain a JSON array of strings")
        result.append(item)
    return result


def _mapping_list_json(raw: object, *, field: str) -> list[dict[str, object]]:
    value = json.loads(str(raw or "[]"))
    if not isinstance(value, list):
        raise ValueError(f"{field} must contain a JSON array of objects")
    result: list[dict[str, object]] = []
    for index, item in enumerate(cast(list[object], value)):
        if not isinstance(item, Mapping):
            raise ValueError(f"{field}[{index}] must be a JSON object")
        result.append({str(key): mapped for key, mapped in cast(Mapping[object, object], item).items()})
    return result
