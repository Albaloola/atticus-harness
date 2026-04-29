"""Configuration loading for Forge."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import shlex
from typing import Any


MODEL_FLASH = "deepseek/deepseek-v4-flash"
MODEL_FLASH_NITRO = "deepseek/deepseek-v4-flash:nitro"

DEFAULT_FORBIDDEN_PATHS = [
    ".env",
    ".env.*",
    "secrets/",
    "private/",
    "evidence/",
    "court_bundles/",
    "node_modules/",
    ".git/",
]


@dataclass(frozen=True)
class DiffLimits:
    max_files_changed: int = 8
    max_diff_lines: int = 800
    max_deleted_lines: int = 500
    max_new_dependencies: int = 0


@dataclass(frozen=True)
class LoopConfig:
    delay_seconds: int = 120
    max_iterations_per_day: int = 50
    max_runtime_per_iteration_minutes: int = 45
    max_consecutive_failures: int = 3
    max_repair_attempts: int = 2
    stop_file: str = ".forge/STOP"
    daily_cost_limit_usd: float = 5.0


@dataclass(frozen=True)
class ModelProfile:
    model: str
    temperature: float
    max_tokens: int


@dataclass(frozen=True)
class ForgeConfig:
    policy_name: str = "default"
    forbidden_paths: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN_PATHS))
    sensitive_paths: list[str] = field(default_factory=list)
    required_principles: list[str] = field(default_factory=list)
    required_checks: list[str] = field(default_factory=list)
    optional_checks: list[str] = field(default_factory=list)
    diff_limits: DiffLimits = field(default_factory=DiffLimits)
    loop: LoopConfig = field(default_factory=LoopConfig)
    auto_commit: bool = True
    auto_merge: bool = False
    auto_push: bool = False
    engine_command: list[str] = field(default_factory=list)
    engine_timeout_seconds: int = 2700
    models: dict[str, ModelProfile] = field(default_factory=dict)


def default_models() -> dict[str, ModelProfile]:
    return {
        "architect": ModelProfile(MODEL_FLASH, 0.35, 12000),
        "builder": ModelProfile(MODEL_FLASH_NITRO, 0.20, 24000),
        "reviewer": ModelProfile(MODEL_FLASH, 0.10, 12000),
        "security_reviewer": ModelProfile(MODEL_FLASH, 0.05, 12000),
        "domain_reviewer": ModelProfile(MODEL_FLASH, 0.10, 12000),
        "judge": ModelProfile(MODEL_FLASH, 0.00, 8000),
        "dreamer": ModelProfile(MODEL_FLASH, 0.45, 16000),
    }


def load_config(repo: Path, *, policy: str = "default", engine_command: str | None = None) -> ForgeConfig:
    policy_data = _load_policy_file(policy)
    repo_policy = repo / ".forge" / "policy.yaml"
    if repo_policy.exists():
        policy_data = _merge_dicts(policy_data, _parse_simple_yaml(repo_policy.read_text(encoding="utf-8")))

    checks = [str(item) for item in policy_data.get("required_checks", []) if str(item).strip()]
    if not checks:
        checks = detect_default_checks(repo)

    limits_raw = _as_dict(policy_data.get("diff_limits"))
    limits = DiffLimits(
        max_files_changed=_int(limits_raw.get("max_files_changed"), 8),
        max_diff_lines=_int(limits_raw.get("max_diff_lines"), 800),
        max_deleted_lines=_int(limits_raw.get("max_deleted_lines"), 500),
        max_new_dependencies=_int(limits_raw.get("max_new_dependencies"), 0),
    )
    loop_raw = _as_dict(policy_data.get("loop"))
    loop = LoopConfig(
        delay_seconds=_int(loop_raw.get("delay_seconds"), 120),
        max_iterations_per_day=_int(loop_raw.get("max_iterations_per_day"), 50),
        max_runtime_per_iteration_minutes=_int(loop_raw.get("max_runtime_per_iteration_minutes"), 45),
        max_consecutive_failures=_int(loop_raw.get("max_consecutive_failures"), 3),
        max_repair_attempts=_int(loop_raw.get("max_repair_attempts"), 2),
        stop_file=str(loop_raw.get("stop_file") or ".forge/STOP"),
        daily_cost_limit_usd=_float(loop_raw.get("daily_cost_limit_usd"), 5.0),
    )

    command = engine_command or os.environ.get("FORGE_ENGINE_COMMAND", "")
    parsed_command = shlex.split(command) if command else _default_engine_command()

    return ForgeConfig(
        policy_name=str(policy_data.get("name") or policy),
        forbidden_paths=[str(item) for item in policy_data.get("forbidden_paths", DEFAULT_FORBIDDEN_PATHS)],
        sensitive_paths=[str(item) for item in policy_data.get("sensitive_paths", [])],
        required_principles=[str(item) for item in policy_data.get("required_principles", [])],
        required_checks=checks,
        optional_checks=[str(item) for item in policy_data.get("optional_checks", [])],
        diff_limits=limits,
        loop=loop,
        auto_commit=_bool(policy_data.get("auto_commit"), True),
        auto_merge=_bool(policy_data.get("auto_merge"), False),
        auto_push=_bool(policy_data.get("auto_push"), False),
        engine_command=parsed_command,
        engine_timeout_seconds=_int(policy_data.get("engine_timeout_seconds"), 2700),
        models=default_models(),
    )


def detect_default_checks(repo: Path) -> list[str]:
    checks: list[str] = []
    if (repo / "pyproject.toml").exists():
        checks.append("python -m pytest")
    if (repo / "package.json").exists():
        checks.append("npm test")
    if (repo / "Cargo.toml").exists():
        checks.append("cargo test")
    if (repo / "go.mod").exists():
        checks.append("go test ./...")
    return checks or ["python -m pytest"]


def _default_engine_command() -> list[str]:
    local_openclaw = Path.home() / "open-systeme-Repo 1 Claude Code" / "openclaw.mjs"
    if local_openclaw.exists():
        return ["node", str(local_openclaw)]
    return []


def _load_policy_file(policy: str) -> dict[str, Any]:
    policy_path = Path(__file__).parent / "policies" / f"{policy}.yaml"
    if not policy_path.exists():
        policy_path = Path(__file__).parent / "policies" / "default.yaml"
    return _parse_simple_yaml(policy_path.read_text(encoding="utf-8")) if policy_path.exists() else {}


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    current_key: str | None = None
    current_nested: str | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if indent == 0 and line.endswith(":"):
            current_key = line[:-1]
            root[current_key] = [] if current_key.endswith("paths") or current_key in {"required_checks", "optional_checks", "required_principles"} else {}
            current_nested = None
            continue
        if indent == 0 and ":" in line:
            key, value = line.split(":", 1)
            root[key.strip()] = _parse_scalar(value.strip())
            current_key = key.strip()
            current_nested = None
            continue
        if current_key is None:
            continue
        if line.startswith("- "):
            value = _parse_scalar(line[2:].strip())
            if not isinstance(root.get(current_key), list):
                root[current_key] = []
            root[current_key].append(value)
            continue
        if indent >= 2 and ":" in line:
            key, value = line.split(":", 1)
            if isinstance(root.get(current_key), dict):
                root[current_key][key.strip()] = _parse_scalar(value.strip())
                current_nested = key.strip()
            elif current_nested and isinstance(root.get(current_key), dict):
                root[current_key][current_nested] = {key.strip(): _parse_scalar(value.strip())}
    return root


def _parse_scalar(value: str) -> Any:
    if value in {"", "null", "None"}:
        return ""
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1]
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def _as_dict(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, dict) else {}


def _merge_dicts(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    merged = dict(first)
    for key, value in second.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dicts(merged[key], value)
        else:
            merged[key] = value
    return merged


def _int(value: object, default: int) -> int:
    if value in (None, ""):
        return default
    return int(str(value))


def _float(value: object, default: float) -> float:
    if value in (None, ""):
        return default
    return float(str(value))


def _bool(value: object, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}
