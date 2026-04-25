"""Small local ranking helpers."""

from __future__ import annotations

import re

TOKEN_RE = re.compile(r"[a-zA-Z0-9_./:-]+")


def tokens(text: str) -> set[str]:
    return {t.lower() for t in TOKEN_RE.findall(text)}


def lexical_score(query: str, text: str) -> float:
    q = tokens(query)
    if not q:
        return 0.0
    hay = tokens(text)
    overlap = q & hay
    return len(overlap) / len(q)
