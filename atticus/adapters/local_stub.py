"""Local stub adapter for tests."""

from __future__ import annotations


class LocalStubAdapter:
    name = "local_stub"

    def run(self, payload: dict) -> dict:
        return {"adapter": self.name, "payload": payload}
