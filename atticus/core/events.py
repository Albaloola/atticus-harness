"""Append-only event helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json



def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Event:
    event_type: str
    actor: str
    payload: dict[str, object]
    matter_scope: str = "atticus"

    def canonical_payload(self) -> str:
        return json.dumps(self.payload, sort_keys=True, separators=(",", ":"))

    def hash(self, previous_hash: str = "") -> str:
        material = "|".join(
            [self.event_type, self.actor, self.matter_scope, previous_hash, self.canonical_payload()]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
