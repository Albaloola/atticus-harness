"""OpenClaw execution adapter boundary.

OpenClaw is intentionally an adapter, not the owner of Atticus harness state.
This module does not resume OpenClaw or start legal workers.
"""

from __future__ import annotations

from atticus.adapters.base import AdapterBlocked, ExecutionAdapter


class OpenClawAdapter(ExecutionAdapter):
    name = "openclaw"

    def launch(self, *_args: object, **_kwargs: object) -> None:
        raise AdapterBlocked("OpenClaw launch is blocked in the foundation; explicit work-order execution is deferred")
