"""Forge command line interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from forge.audit.writer import latest_audit
from forge.daemon import loop as run_loop
from forge.loop.run_one import run_one
from forge.memory.dream import run_dreamer
from forge.state import ensure_forge_dirs, read_state, request_stop, resume
from forge.worktrees.manager import run_git


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="forge")
    sub = parser.add_subparsers(dest="command", required=True)
    _repo_cmd(sub.add_parser("init", help="initialize Forge files"), required=False)
    _repo_cmd(sub.add_parser("status", help="show Forge status"), required=False)
    run_one_cmd = _repo_cmd(sub.add_parser("run-one", help="run one autonomous improvement"))
    _runtime_args(run_one_cmd)
    loop_cmd = _repo_cmd(sub.add_parser("loop", help="repeat run-one until stopped"))
    _runtime_args(loop_cmd)
    _ = loop_cmd.add_argument("--max-iterations", type=int)
    _ = loop_cmd.add_argument("--delay-seconds", type=int)
    _repo_cmd(sub.add_parser("stop", help="create .forge/STOP"))
    _repo_cmd(sub.add_parser("resume", help="remove .forge/STOP"))
    audit = _repo_cmd(sub.add_parser("audit", help="show audit report"))
    _ = audit.add_argument("--last", action="store_true")
    _repo_cmd(sub.add_parser("branches", help="list forge branches"))
    _repo_cmd(sub.add_parser("cleanup", help="prune stale worktrees"))
    _repo_cmd(sub.add_parser("dream", help="update memory/backlog from audits"))
    _repo_cmd(sub.add_parser("gui", help="run Arch-native Forge GTK app"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _main(args)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        return 2


def _main(args: argparse.Namespace) -> int:
    repo = Path(args.repo or ".").resolve()
    if args.command == "init":
        ensure_forge_dirs(repo)
        _ensure_text(repo / "FORGE_MISSION.md", "# Forge Mission\n\nLocal autonomous branch factory.\n")
        _ensure_text(repo / "FORGE_BACKLOG.md", "# Forge Backlog\n\n- [ ] Add the first small safe improvement.\n")
        print_json({"initialized": str(repo), "state": read_state(repo)})
        return 0
    if args.command == "status":
        print_json({"repo": str(repo), "state": read_state(repo), "branches": _branches(repo)})
        return 0
    if args.command == "run-one":
        packet = run_one(repo, policy=args.policy, engine_command=args.engine_command, shell_engine_command=args.shell_engine_command, offline_review=args.offline_review)
        print_json(packet.as_dict())
        return 0 if packet.final_decision == "committed" else 2
    if args.command == "loop":
        result = run_loop(repo, policy=args.policy, engine_command=args.engine_command, shell_engine_command=args.shell_engine_command, offline_review=args.offline_review, delay_seconds=args.delay_seconds, max_iterations=args.max_iterations)
        print_json(result)
        return 0
    if args.command == "stop":
        request_stop(repo)
        print_json({"stopped": True, "repo": str(repo)})
        return 0
    if args.command == "resume":
        resume(repo)
        print_json({"resumed": True, "repo": str(repo)})
        return 0
    if args.command == "audit":
        report = latest_audit(repo)
        print(report.read_text(encoding="utf-8") if report else json.dumps({"audit": None}))
        return 0
    if args.command == "branches":
        print_json({"branches": _branches(repo)})
        return 0
    if args.command == "cleanup":
        proc = run_git(repo, ["worktree", "prune"], check=False)
        print_json({"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
        return proc.returncode
    if args.command == "dream":
        print_json(run_dreamer(repo))
        return 0
    if args.command == "gui":
        return _run_native_gui(repo)
    raise ValueError(f"unknown command {args.command}")


def _repo_cmd(parser: argparse.ArgumentParser, *, required: bool = True) -> argparse.ArgumentParser:
    _ = parser.add_argument("--repo", required=required, help="target git repository")
    return parser


def _runtime_args(parser: argparse.ArgumentParser) -> None:
    _ = parser.add_argument("--policy", default="default")
    _ = parser.add_argument("--engine-command")
    _ = parser.add_argument("--shell-engine-command")
    _ = parser.add_argument("--offline-review", action="store_true")


def _ensure_text(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def _branches(repo: Path) -> list[str]:
    proc = run_git(repo, ["branch", "--list", "forge/*"], check=False)
    return [line.strip().lstrip("* ").strip() for line in proc.stdout.splitlines() if line.strip()]


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def _run_native_gui(repo: Path) -> int:
    app_dir = Path(__file__).resolve().parent.parent / "forge-app"
    if not app_dir.exists():
        raise RuntimeError(f"native Forge app not found: {app_dir}")
    proc = subprocess.run(["npm", "run", "app", "--", "--repo", str(repo)], cwd=app_dir, text=True)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
