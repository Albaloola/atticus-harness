"""Worker launcher boundary.

The foundation does not launch real legal workers. Adapters implement this
interface later under explicit work-order control.
"""

from __future__ import annotations


class WorkerLaunchBlocked(RuntimeError):
    """Raised when a live worker launch is attempted in foundation mode."""


class WorkerLauncher:
    def launch(self, *_args: object, **_kwargs: object) -> None:
        raise WorkerLaunchBlocked("live legal workers are not enabled in this harness foundation")
