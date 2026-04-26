"""Matter-scope authorization helpers."""

from __future__ import annotations

import os
from collections.abc import Mapping


AUTHORIZED_MATTER_ENV = "ATTICUS_AUTHORIZED_MATTER"
DEFAULT_AUTHORIZED_MATTER = "atticus"


class MatterAccessDenied(RuntimeError):
    """Raised when a caller requests a matter outside its authorized scope."""


def authorized_matter_from_env(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    return _normalize_matter_scope(values.get(AUTHORIZED_MATTER_ENV) or DEFAULT_AUTHORIZED_MATTER)


def require_matter_access(requested_matter_scope: str, *, authorized_matter_scope: str = DEFAULT_AUTHORIZED_MATTER) -> str:
    requested = _normalize_matter_scope(requested_matter_scope)
    authorized = _normalize_matter_scope(authorized_matter_scope)
    if requested != authorized:
        raise MatterAccessDenied(f"matter '{requested}' is not authorized for this execution context")
    return requested


def _normalize_matter_scope(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        raise MatterAccessDenied("matter scope must not be empty")
    return normalized
