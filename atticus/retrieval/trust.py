"""Trust and confidence labels for read-only answers."""

from __future__ import annotations


def trust_level(rows: list[dict]) -> str:
    if not rows:
        return "unsupported"
    statuses = {str(row["trust_status"]) for row in rows}
    if "certified" in statuses and not any(row["stale"] for row in rows):
        return "certified-supported"
    if any(row["stale"] for row in rows):
        return "stale-or-mixed"
    if statuses <= {"candidate", "rough_note", "unverified_legacy"}:
        return "candidate-only"
    return "mixed"


def confidence(rows: list[dict]) -> str:
    if not rows:
        return "low"
    non_stale = [r for r in rows if not r["stale"]]
    certified = [r for r in non_stale if r["trust_status"] == "certified"]
    if certified:
        return "medium"
    if len(non_stale) >= 2:
        return "low-medium"
    return "low"
