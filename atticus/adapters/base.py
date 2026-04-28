"""Execution adapter base classes."""

from __future__ import annotations


class AdapterBlocked(RuntimeError):
    """Raised when an adapter action is not allowed."""


class ExecutionAdapter:
    name: str = "base"

    def launch(self, *_args: object, **_kwargs: object) -> None:
        raise AdapterBlocked("adapter launch requires an explicit approved work order")
