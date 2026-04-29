"""Secret scanning gates."""

from __future__ import annotations

import re

from forge.audit.packet import GateResult


SECRET_PATTERNS = [
    re.compile(r"(?i)api[_-]?key\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-.]{20,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |)PRIVATE KEY-----"),
    re.compile(r"(?i)password\s*[:=]\s*['\"][^'\"]{8,}"),
    re.compile(r"(?i)(cookie|session[_-]?token)\s*[:=]\s*['\"][^'\"]{12,}"),
]


def scan_diff_for_secrets(diff: str) -> GateResult:
    findings: list[str] = []
    for line in diff.splitlines():
        if not line.startswith("+") or line.startswith("+++"):
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(line):
                findings.append(line[:160])
                break
    return GateResult(name="secret scan", passed=not findings, details="\n".join(findings) if findings else "no obvious secrets in added lines")


def scan_forbidden_commands(text: str) -> GateResult:
    forbidden = ["rm -rf /", "sudo ", "curl | sh", "wget | sh", "chmod 777", "git push --force", "docker run --privileged"]
    findings = [item for item in forbidden if item in text]
    return GateResult(name="forbidden command scan", passed=not findings, details=", ".join(findings) if findings else "no forbidden commands found")
