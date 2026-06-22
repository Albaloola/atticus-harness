"""Prompt-prefix cache planning helpers."""

from __future__ import annotations

import hashlib


def stable_prefix_id(parts: list[str]) -> str:
    material = "\n\n".join(parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
