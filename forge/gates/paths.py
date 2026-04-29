"""Path safety gates."""

from __future__ import annotations

from fnmatch import fnmatch

from forge.audit.packet import GateResult


def check_paths(changed_files: list[str], forbidden_paths: list[str], allowed_paths: list[str] | None = None) -> GateResult:
    violations: list[str] = []
    allowed = allowed_paths or []
    for path in changed_files:
        normalized = path.replace("\\", "/")
        if allowed and not any(_allowed_matches(normalized, pattern.replace("\\", "/")) for pattern in allowed):
            violations.append(f"{path} is outside allowed paths: {', '.join(allowed)}")
        for pattern in forbidden_paths:
            pat = pattern.replace("\\", "/")
            if _matches(normalized, pat):
                violations.append(f"{path} matches {pattern}")
    return GateResult(name="path safety", passed=not violations, details="\n".join(violations) if violations else "no forbidden paths changed")


def _matches(path: str, pattern: str) -> bool:
    if pattern in {"", ".", "./", "**"}:
        return True
    if pattern.endswith("/"):
        return path.startswith(pattern) or f"/{pattern}" in path
    return fnmatch(path, pattern) or fnmatch(path.rsplit("/", 1)[-1], pattern)


def _allowed_matches(path: str, pattern: str) -> bool:
    if pattern in {"", ".", "./", "**"}:
        return True
    if pattern.endswith("/"):
        return path.startswith(pattern)
    return path == pattern or fnmatch(path, pattern)
