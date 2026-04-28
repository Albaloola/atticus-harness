"""Evidence validation placeholders."""

from __future__ import annotations




def has_source_citation(claim: dict[str, object]) -> bool:
    return bool(claim.get("source_ids") or claim.get("citations"))
