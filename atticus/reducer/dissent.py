"""Dissent capture placeholder."""

from __future__ import annotations




def dissenting_items(reviews: list[dict[str, object]]) -> list[dict[str, object]]:
    return [review for review in reviews if review.get("dissent")]
