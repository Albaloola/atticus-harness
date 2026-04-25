"""Citation formatting for read-only answers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Citation:
    citation_id: str
    record_type: str
    record_id: str
    path: str
    trust_status: str
    stale: bool
    snippet: str

    def as_dict(self) -> dict[str, object]:
        return {
            "citation_id": self.citation_id,
            "record_type": self.record_type,
            "record_id": self.record_id,
            "path": self.path,
            "trust_status": self.trust_status,
            "stale": self.stale,
            "snippet": self.snippet,
        }
