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
from atticus.context.sections import UNTRUSTED_EVIDENCE_BOUNDARY
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION, result_packet_json_schema


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
            f"candidate result packet matching {RESULT_PACKET_SCHEMA_VERSION} and the supplied output schema. "
            "Your answer is candidate, not canonical; reducers decide what becomes trusted. Use only the matter-scoped "
            f"context in the work order. {UNTRUSTED_EVIDENCE_BOUNDARY} "
            "Separate fact, law, procedure, inference, contradiction, and risk. Cite every "
            "factual, legal, procedural, contradiction, or risk finding to an allowed context target, or mark it "
            "uncertain or needs_research. For extracted/OCR source_materials, cite target_type='source' with the source_id; "
            "do not cite generated extraction artifacts unless citation_targets explicitly allows the artifact. "
            "Every proposed_artifacts[].path must be a relative candidate path like candidate/<task_id>/result.md; "
            "never return an absolute filesystem path such as LOCAL_PATH_REDACTED or a path containing '..'. "
            "Draft artifacts, draft_complaint artifacts, and redacted_draft artifacts must contain complete replacement text; "
            "do not use placeholders such as '[remaining unchanged]', '[conclusion unchanged]', or omitted sections. "
            "If a complete draft cannot fit, return a drafting_note or scoped follow-up task instead of a partial draft artifact. "
            "For redaction verification, treat the original unredacted artifact as comparison evidence only; an identifier in "
            "the original is not a privacy defect unless the same unsafe identifier remains in the redacted target artifact. "
            "If citation_ids is empty, never label a fact, law, procedure, risk, or contradiction as supported; "
            "use reasoning_status='uncertain' or 'needs_research', or use finding_type='drafting_note' for task limitations. "
            "If uncertainties, contradictions, risk_flags, or redaction_flags include citation_ids, every id must exist in citations. "
            "Supported law findings must cite at least one allowed target_type='authority'; matter sources can support facts "
            "about what happened, but they cannot by themselves prove a legal rule. "
            "When auditing a draft, citation, or redaction issue, cite the draft/review artifact that contains the defect; "
            "if the missing or fabricated target itself is absent from context, do not use an uncited supported contradiction. "
            "Negative or absence findings about a reviewed source must cite that reviewed source, or be marked uncertain; "
            "never assert a supported absence finding with empty citation_ids. "
            "Absence of source_materials in this work order only means no task-specific source text was supplied; "
            "it is not proof that the matter has no records, no evidence, or no support. "
            "Do not invent citations, authorities, documents, dates, quotes, amounts, "
            "admissions, deadlines, remedies, or procedural posture. Flag stale evidence, weak support, contradictions, "
            "privacy/redaction concerns, and missing certifications. Do not write canonical artifacts or memory. "
            "Do not send, file, serve, upload, email, contact, message, or perform any external legal action. "
            "If uncertain, use the uncertainties array and propose follow-up tasks rather than hiding uncertainty.\n\n"
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
    return result_packet_json_schema()


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
