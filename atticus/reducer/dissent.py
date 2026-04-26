"""Dissent capture placeholder."""

from __future__ import annotations

from typing import Any


def dissenting_items(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [review for review in reviews if review.get("dissent")]
