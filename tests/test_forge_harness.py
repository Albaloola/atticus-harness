from __future__ import annotations

from pathlib import Path
import json
import subprocess
import sys

from forge.audit.packet import AuditPacket, ReviewerVerdict
from forge.config import load_config
from forge.council.judge import judge
from forge.gates.paths import check_paths
from forge.gates.secrets import scan_diff_for_secrets
from forge.loop.harvest import harvest_tasks
from forge.loop.run_one import run_one
from forge.loop.task import TaskPacket
from forge.worktrees.manager import slugify


def test_task_prompt_contains_bounded_rules() -> None:
    task = TaskPacket(
        id="T-0001",
        title="Add focused test",
        reason="A regression needs coverage.",
        allowed_paths=["tests/"],
        forbidden_paths=[".env", "secrets/"],
        required_checks=["python -m pytest"],
    )

    prompt = task.to_builder_prompt()

    assert "You are working inside an isolated git worktree" in prompt
    assert "Only modify allowed paths" in prompt
    assert '"allowed_paths"' in prompt
    assert "python -m pytest" in prompt


def test_policy_gates_reject_forbidden_paths_and_secret_lines() -> None:
    path_gate = check_paths([".env", "atticus/core.py"], [".env", "secrets/"])
    secret_gate = scan_diff_for_secrets('+OPENROUTER_API_KEY="sk-this-is-not-a-real-token"\n')

    assert not path_gate.passed
    assert not secret_gate.passed


def test_path_gate_rejects_files_outside_allowed_paths() -> None:
    gate = check_paths(["package.json"], [".env", "secrets/"], ["FORGE_BACKLOG.md", "tests/"])
    nested_tests = check_paths(["src/tests/example.py"], [], ["tests/"])
    nested_readme = check_paths(["docs/README.md"], [], ["README.md"])

    assert not gate.passed
    assert "outside allowed paths" in gate.details
    assert not nested_tests.passed
    assert not nested_readme.passed


def test_judge_rejects_repair_verdict_without_blocking_issues() -> None:
    verdict = ReviewerVerdict(role="reviewer", verdict="repair", confidence=0.8, risk_level="medium", recommended_repairs=["tighten the diff"])

    result = judge([], [verdict])

    assert result.verdict == "reject"
    assert "Reviewer did not approve" in result.blocking_issues[0]


def test_slugify_produces_safe_branch_component() -> None:
    assert slugify("Add citation validator tests!!!") == "add-citation-validator-tests"
    assert slugify("../") == "task"


def test_harvest_uses_forge_backlog(tmp_path: Path) -> None:
    repo = tmp_path
    (repo / "FORGE_BACKLOG.md").write_text("# Backlog\n\n- [ ] Add a tiny regression test\n", encoding="utf-8")
    config = load_config(repo)

    tasks = harvest_tasks(repo, config)

    assert tasks[0].title == "Add a tiny regression test"
    assert tasks[0].id == "T-0001"


def test_native_app_manifest_exposes_arch_gtk_runner() -> None:
    manifest = json.loads(Path("forge-app/package.json").read_text(encoding="utf-8"))

    assert "gtk4" in manifest["scripts"]["build:native"]
    assert manifest["scripts"]["app"] == "npm run build:native && ./build/forge-gtk"
    assert manifest["devDependencies"] == {}


def test_audit_packet_reserves_usage_and_cost_fields() -> None:
    packet = AuditPacket(iteration_id="T-0001", timestamp_start="2026-04-29T00:00:00+00:00")

    assert packet.usage == {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "total_tokens": 0, "total_cost_usd": 0.0}
    assert packet.cost["prompt_tokens"] == 0
    assert packet.cost["cached_tokens"] == 0


def test_run_one_rejects_post_check_forbidden_mutation(tmp_path: Path, monkeypatch) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "forge@example.test")
    _git(tmp_path, "config", "user.name", "Forge Test")
    (tmp_path / "FORGE_BACKLOG.md").write_text("# Forge Backlog\n\n- [ ] Catch post-check mutation.\n", encoding="utf-8")
    _git(tmp_path, "add", "FORGE_BACKLOG.md")
    _git(tmp_path, "commit", "-m", "seed backlog")
    fake_bin = tmp_path.parent / f"{tmp_path.name}-bin"
    fake_bin.mkdir()
    fake_python = fake_bin / "python"
    fake_python.write_text("#!/bin/sh\nprintf 'OPENROUTER_API_KEY=\"sk-this-is-not-real-token\"\n' > .env\nexit 0\n", encoding="utf-8")
    fake_python.chmod(0o755)
    engine = f"{sys.executable} - <<'PY'\nfrom pathlib import Path\nPath('FORGE_BACKLOG.md').write_text(Path('FORGE_BACKLOG.md').read_text() + '\\n- [x] Engine changed allowed file.\\n')\nPY"
    monkeypatch.setenv("PATH", f"{fake_bin}:{Path('/usr/bin')}:{Path('/bin')}")

    packet = run_one(tmp_path, shell_engine_command=engine, offline_review=True, require_openrouter_key=False)

    assert packet.final_decision == "rejected"
    failed_gates = {gate["name"]: gate for gate in packet.gate_results if not gate["passed"]}
    assert "path safety" in failed_gates
    assert "secret scan" in failed_gates


def test_run_one_rejects_staged_secret(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "forge@example.test")
    _git(tmp_path, "config", "user.name", "Forge Test")
    (tmp_path / "FORGE_BACKLOG.md").write_text("# Forge Backlog\n\n- [ ] Catch staged secret.\n", encoding="utf-8")
    _git(tmp_path, "add", "FORGE_BACKLOG.md")
    _git(tmp_path, "commit", "-m", "seed backlog")
    engine = (
        f"{sys.executable} - <<'PY'\n"
        "from pathlib import Path\n"
        "import subprocess\n"
        "Path('README.md').write_text('OPENROUTER_API_KEY=\\\"sk-this-is-not-real-token\\\"\\n')\n"
        "subprocess.run(['git', 'add', 'README.md'], check=True)\n"
        "Path('FORGE_BACKLOG.md').write_text(Path('FORGE_BACKLOG.md').read_text() + '\\n- [x] Engine changed allowed file.\\n')\n"
        "PY"
    )

    packet = run_one(tmp_path, shell_engine_command=engine, offline_review=True, require_openrouter_key=False)

    assert packet.final_decision == "rejected"
    assert any(gate["name"] == "secret scan" and not gate["passed"] for gate in packet.gate_results)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, text=True, capture_output=True)
