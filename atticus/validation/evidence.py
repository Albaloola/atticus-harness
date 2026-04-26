"""Evidence validation placeholders."""

from __future__ import annotations

from typing import Any


def has_source_citation(claim: dict[str, Any]) -> bool:
    return bool(claim.get("source_ids") or claim.get("citations"))
