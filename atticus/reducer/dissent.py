"""Dissent capture placeholder."""

from __future__ import annotations


def dissenting_items(reviews: list[dict]) -> list[dict]:
    return [review for review in reviews if review.get("dissent")]
