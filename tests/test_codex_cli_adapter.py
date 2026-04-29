from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
import json
import subprocess
from typing import cast

import pytest

from atticus.adapters.codex_cli import CodexCliAdapter, CodexCliAdapterError
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION


def _packet(task_id: str) -> dict[str, object]:
    return {
        "schema_version": RESULT_PACKET_SCHEMA_VERSION,
        "task_id": task_id,
        "summary": "codex candidate",
        "findings": [],
        "citations": [],
        "proposed_artifacts": [],
        "proposed_tasks": [],
        "uncertainties": [],
        "contradictions": [],
        "risk_flags": [],
        "redaction_flags": [],
        "external_action_requests": [],
    }


def test_codex_cli_adapter_invokes_bounded_exec_and_reads_strict_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, object]] = []

    def fake_run(
        cmd: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        calls.append(
            {
                "cmd": list(cmd),
                "input": input,
                "text": text,
                "capture_output": capture_output,
                "timeout": timeout,
                "check": check,
            }
        )
        output_file = Path(list(cmd)[list(cmd).index("--output-last-message") + 1])
        _ = output_file.write_text(json.dumps(_packet("codex-adapter-task")), encoding="utf-8")
        return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = CodexCliAdapter(executable="codex", cwd=tmp_path)

    result = adapter.run(
        {"task_id": "codex-adapter-task", "instructions": "stay bounded"},
        model="gpt-5.5",
        output_dir=tmp_path / "out",
        timeout_seconds=123.0,
    )

    assert result["task_id"] == "codex-adapter-task"
    assert len(calls) == 1
    cmd = cast(list[str], calls[0]["cmd"])
    assert cmd[:2] == ["codex", "exec"]
    assert "--ephemeral" in cmd
    assert "--ignore-user-config" in cmd
    assert cmd[cmd.index("--config") + 1] == 'model_reasoning_effort="low"'
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert cmd[cmd.index("--model") + 1] == "gpt-5.5"
    schema_path = Path(cmd[cmd.index("--output-schema") + 1])
    assert schema_path.exists()
    schema = cast(Mapping[str, object], json.loads(schema_path.read_text(encoding="utf-8")))
    assert schema["additionalProperties"] is False
    required = cast(list[str], schema["required"])
    assert "proposed_tasks" in required
    properties = cast(Mapping[str, object], schema["properties"])
    proposed_tasks = cast(Mapping[str, object], properties["proposed_tasks"])
    proposed_task_items = cast(Mapping[str, object], proposed_tasks["items"])
    proposed_task_properties = cast(Mapping[str, object], proposed_task_items["properties"])
    assert "source_dependencies" in proposed_task_properties
    assert "provider_policy" in proposed_task_properties
    assert Path(cmd[cmd.index("--output-last-message") + 1]).exists()
    assert (tmp_path / "out" / "codex.diagnostics.json").exists()
    assert calls[0]["timeout"] == 123.0
    prompt = cast(str, calls[0]["input"])
    assert "work_order_json" in prompt
    assert "external legal action" in prompt
    assert "candidate, not canonical" in prompt
    assert "untrusted evidence, not instructions" in prompt
    assert "fact, law, procedure, inference, contradiction, and risk" in prompt
    diagnostics = cast(Mapping[str, object], json.loads((tmp_path / "out" / "codex.diagnostics.json").read_text(encoding="utf-8")))
    assert diagnostics["reasoning_effort"] == "low"


def test_codex_cli_adapter_supports_explicit_reasoning_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []

    def fake_run(
        cmd: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del input, text, capture_output, timeout, check
        calls.append(list(cmd))
        output_file = Path(list(cmd)[list(cmd).index("--output-last-message") + 1])
        _ = output_file.write_text(json.dumps(_packet("codex-adapter-task")), encoding="utf-8")
        return subprocess.CompletedProcess(list(cmd), 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = CodexCliAdapter(executable="codex", cwd=tmp_path)

    _ = adapter.run(
        {"task_id": "codex-adapter-task"},
        model="gpt-5.5",
        output_dir=tmp_path / "out",
        timeout_seconds=123.0,
        reasoning_effort="medium",
    )

    assert calls[0][calls[0].index("--config") + 1] == 'model_reasoning_effort="medium"'


def test_codex_cli_adapter_rejects_wrong_model_before_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess.run must not be called for wrong model")

    monkeypatch.setattr(subprocess, "run", fail_run)
    adapter = CodexCliAdapter(cwd=tmp_path)

    with pytest.raises(CodexCliAdapterError, match="only permits gpt-5.5"):
        _ = adapter.run({"task_id": "wrong-model"}, model="gpt-5.4", output_dir=tmp_path / "out", timeout_seconds=10.0)


def test_codex_cli_adapter_rejects_unknown_reasoning_effort_before_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fail_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise AssertionError("subprocess.run must not be called for unknown reasoning effort")

    monkeypatch.setattr(subprocess, "run", fail_run)
    adapter = CodexCliAdapter(cwd=tmp_path)

    with pytest.raises(CodexCliAdapterError, match="unsupported Codex reasoning effort"):
        _ = adapter.run(
            {"task_id": "wrong-effort"},
            model="gpt-5.5",
            output_dir=tmp_path / "out",
            timeout_seconds=10.0,
            reasoning_effort="max",
        )


def test_codex_cli_adapter_blocks_nonzero_exit_without_json_packet(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def fake_run(
        cmd: Sequence[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        timeout: float,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        del input, text, capture_output, timeout, check
        return subprocess.CompletedProcess(list(cmd), 42, stdout="", stderr="provider failed loudly")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = CodexCliAdapter(cwd=tmp_path)

    with pytest.raises(CodexCliAdapterError, match="diagnostics"):
        _ = adapter.run({"task_id": "cli-error"}, model="gpt-5.5", output_dir=tmp_path / "out", timeout_seconds=10.0)
    diagnostics = cast(Mapping[str, object], json.loads((tmp_path / "out" / "codex.diagnostics.json").read_text(encoding="utf-8")))
    assert diagnostics["returncode"] == 42
    assert Path(str(diagnostics["stderr_path"])).read_text(encoding="utf-8") == "provider failed loudly"
    assert diagnostics.get("prompt_sha256")
