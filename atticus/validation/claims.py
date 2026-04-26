"""Claim validation placeholders."""

from __future__ import annotations

from typing import Any


def unsupported_claims(claims: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [claim for claim in claims if not (claim.get("source_ids") or claim.get("citations"))]
