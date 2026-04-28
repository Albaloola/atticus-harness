"""Bounded Codex CLI adapter for explicit live worker execution.

The adapter is intentionally narrow: it feeds one JSON work order to
``codex exec`` and accepts one strict JSON candidate packet back. Runtime gates
live execution before this adapter is constructed or called.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import subprocess
from typing import cast

from atticus.adapters.base import ExecutionAdapter


LIVE_CODEX_ENV = "ATTICUS_ENABLE_LIVE_CODEX"
CODEX_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


class CodexCliAdapterError(RuntimeError):
    """Raised when the Codex CLI fails to return a usable candidate packet."""


class CodexCliAdapter(ExecutionAdapter):
    name: str = "codex_cli"

    def __init__(self, *, executable: str = "codex", cwd: str | Path | None = None) -> None:
        self.executable: str = executable
        self.cwd: Path = Path(cwd).resolve() if cwd is not None else Path.cwd().resolve()

    def run(
        self,
        work_order: dict[str, object],
        *,
        model: str,
        output_dir: str | Path,
        timeout_seconds: float,
        reasoning_effort: str = "low",
    ) -> dict[str, object]:
        """Run Codex CLI and return the final JSON candidate packet.

        ``codex exec --help`` on this host exposes ``-m/--model``,
        ``--output-schema`` and ``--output-last-message``. Those are the only
        model/output controls this adapter relies on.
        """

        if model != "gpt-5.5":
            raise CodexCliAdapterError(f"Codex CLI adapter only permits gpt-5.5, got {model}")
        if timeout_seconds <= 0:
            raise CodexCliAdapterError("Codex CLI timeout_seconds must be positive")
        if reasoning_effort not in CODEX_REASONING_EFFORTS:
            raise CodexCliAdapterError(f"unsupported Codex reasoning effort: {reasoning_effort}")

        task_dir = Path(output_dir).resolve()
        task_dir.mkdir(parents=True, exist_ok=True)
        work_order_path = task_dir / "work_order.json"
        schema_path = task_dir / "candidate_packet.schema.json"
        result_path = task_dir / "candidate_packet.json"
        stdout_path = task_dir / "codex.stdout.txt"
        stderr_path = task_dir / "codex.stderr.txt"
        diagnostics_path = task_dir / "codex.diagnostics.json"
        _ = work_order_path.write_text(json.dumps(work_order, sort_keys=True, indent=2), encoding="utf-8")
        _ = schema_path.write_text(json.dumps(_candidate_packet_schema(), sort_keys=True, indent=2), encoding="utf-8")

        prompt = (
            "You are a bounded Atticus legal harness worker. Read the work_order JSON below and return exactly one JSON "
            "candidate result packet matching the supplied output schema. Do not write canonical artifacts. Do not send, "
            "file, upload, email, contact, or perform any external legal action. If uncertain, put uncertainty in the "
            "summary/findings rather than acting externally.\n\n"
            f"work_order_json:\n{json.dumps(work_order, sort_keys=True)}\n"
        )
        cmd = [
            self.executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--config",
            f'model_reasoning_effort="{reasoning_effort}"',
            "--color",
            "never",
            "--sandbox",
            "read-only",
            "--cd",
            str(self.cwd),
            "--model",
            model,
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(result_path),
            "-",
        ]
        try:
            completed = subprocess.run(
                cmd,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            _write_diagnostics(
                diagnostics_path=diagnostics_path,
                stdout_path=stdout_path,
                stderr_path=stderr_path,
                cmd=cmd,
                returncode=None,
                stdout=exc.stdout if isinstance(exc.stdout, str) else "",
                stderr=exc.stderr if isinstance(exc.stderr, str) else "",
                timeout_seconds=timeout_seconds,
                reasoning_effort=reasoning_effort,
                prompt=prompt,
                result_path=result_path,
            )
            raise CodexCliAdapterError(f"Codex CLI timed out after {timeout_seconds:.1f}s; diagnostics: {diagnostics_path}") from exc
        _write_diagnostics(
            diagnostics_path=diagnostics_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            cmd=cmd,
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            timeout_seconds=timeout_seconds,
            reasoning_effort=reasoning_effort,
            prompt=prompt,
            result_path=result_path,
        )
        if completed.returncode != 0:
            raise CodexCliAdapterError(f"Codex CLI exited {completed.returncode}; diagnostics: {diagnostics_path}; {_safe_cli_text(completed.stderr or completed.stdout)}")
        if not result_path.exists():
            raise CodexCliAdapterError(f"Codex CLI did not write the required output-last-message file; diagnostics: {diagnostics_path}")
        try:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CodexCliAdapterError(f"Codex CLI output was not strict JSON: {exc}; diagnostics: {diagnostics_path}") from exc
        if not isinstance(payload, Mapping):
            raise CodexCliAdapterError("Codex CLI output must be a JSON object candidate packet")
        return {str(key): value for key, value in cast(Mapping[object, object], payload).items()}


def _candidate_packet_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["task_id", "summary", "findings", "citations", "proposed_artifacts", "proposed_tasks"],
        "properties": {
            "task_id": {"type": "string"},
            "summary": {"type": "string"},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "citation_ids"],
                    "properties": {
                        "text": {"type": "string"},
                        "citation_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["target_type", "target_id", "locator"],
                    "properties": {
                        "target_type": {"type": "string"},
                        "target_id": {"type": "string"},
                        "locator": {"type": "string"},
                    },
                },
            },
            "proposed_artifacts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path", "artifact_type", "stage", "title", "content"],
                    "properties": {
                        "path": {"type": "string"},
                        "artifact_type": {"type": "string"},
                        "stage": {"type": "string"},
                        "title": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            },
            "proposed_tasks": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_id", "title", "task_type", "stage", "matter_scope", "instructions"],
                    "properties": {
                        "task_id": {"type": "string"},
                        "title": {"type": "string"},
                        "task_type": {"type": "string"},
                        "stage": {"type": "string"},
                        "matter_scope": {"type": "string"},
                        "instructions": {"type": "string"},
                        "source_dependencies": {"type": "array", "items": {"type": "string"}},
                        "artifact_dependencies": {"type": "array", "items": {"type": "string"}},
                        "task_dependencies": {"type": "array", "items": {"type": "string"}},
                        "matter_dependencies": {"type": "array", "items": {"type": "string"}},
                        "validation_gates": {"type": "array", "items": {"type": "string"}},
                        "required_certifications": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["subject_type", "subject_id", "certification_type"],
                                "properties": {
                                    "subject_type": {"type": "string"},
                                    "subject_id": {"type": "string"},
                                    "certification_type": {"type": "string"},
                                },
                            },
                        },
                        "provider_policy": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["provider", "model", "allow_fallback", "estimated_cost_usd"],
                            "properties": {
                                "provider": {"type": "string"},
                                "model": {"type": "string"},
                                "allow_fallback": {"type": "boolean"},
                                "estimated_cost_usd": {"type": "number"},
                            },
                        },
                        "expected_value": {"type": "number"},
                        "cost_limit_usd": {"type": "number"},
                    },
                },
            },
        },
    }


def _write_diagnostics(
    *,
    diagnostics_path: Path,
    stdout_path: Path,
    stderr_path: Path,
    cmd: list[str],
    returncode: int | None,
    stdout: str,
    stderr: str,
    timeout_seconds: float,
    reasoning_effort: str,
    prompt: str,
    result_path: Path,
) -> None:
    _ = stdout_path.write_text(stdout, encoding="utf-8")
    _ = stderr_path.write_text(stderr, encoding="utf-8")
    payload = {
        "cmd": cmd,
        "returncode": returncode,
        "timeout_seconds": timeout_seconds,
        "reasoning_effort": reasoning_effort,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "result_path": str(result_path),
        "result_exists": result_path.exists(),
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
    }
    _ = diagnostics_path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")


def _safe_cli_text(text: str, *, limit: int = 400) -> str:
    compact = " ".join(text.split())
    return compact[:limit] if compact else "no CLI error output"
