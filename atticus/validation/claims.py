"""Claim validation placeholders."""

from __future__ import annotations




def unsupported_claims(claims: list[dict[str, object]]) -> list[dict[str, object]]:
    return [claim for claim in claims if not (claim.get("source_ids") or claim.get("citations"))]
