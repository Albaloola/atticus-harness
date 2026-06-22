"""Central configuration hub for the Atticus harness.

Loads from ``~/.atticus/config.json`` (or ``ATTICUS_CONFIG_PATH`` env override).
Schema-validated configuration model with thread-safe read/write.
All harness config options flow through this single module so the operator
never has to hunt for secret levers in the codebase.

Config sections:
    models (tier→model_id mappings for flash_worker / pro_orchestrator / codex_exact)
    skills (skill_id→active bool)
    providers (failover settings, API key presence flags)
    budget (matter→limit_usd, budget tracking — never hard-blocks)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import json
import os
import threading
from pathlib import Path
from typing import cast


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG_DIR = Path.home() / ".atticus"
_DEFAULT_CONFIG_PATH = _DEFAULT_CONFIG_DIR / "config.json"
_CONFIG_VERSION = 2

_MODEL_TIERS = ("flash_worker", "pro_orchestrator", "codex_exact")

# Tier → fallback model mapping (OpenRouter model IDs)
_DEFAULT_MODELS: dict[str, str] = {
    "flash_worker": "deepseek/deepseek-v4-flash",
    "pro_orchestrator": "deepseek/deepseek-v4-pro",
    "codex_exact": "gpt-5.5",
}

_DEFAULT_PROVIDER_SETTINGS: dict[str, object] = {
    "openrouter_failover_enabled": True,
    "openrouter_max_failed_cycles": 5,
    "openrouter_cooldown_seconds": 300.0,
    "codex_timeout_seconds": 180.0,
    "allow_live_providers": False,
}


# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------

_CONFIG_SCHEMA: dict[str, object] = {
    "type": "object",
    "required": ["version"],
    "properties": {
        "version": {"type": "integer", "minimum": 1},
        "models": {
            "type": "object",
            "additionalProperties": {"type": "string"},
        },
        "skills": {
            "type": "object",
            "additionalProperties": {"type": "boolean"},
        },
        "providers": {
            "type": "object",
        },
        "budget": {
            "type": "object",
            "additionalProperties": {"type": "number", "minimum": 0},
        },
    },
}


class ConfigError(ValueError):
    """Raised when configuration is invalid."""


# ---------------------------------------------------------------------------
# Config data model
# ---------------------------------------------------------------------------


@dataclass
class AtticusConfig:
    """Full harness configuration snapshot."""

    version: int = _CONFIG_VERSION
    models: dict[str, str] = field(default_factory=lambda: dict(_DEFAULT_MODELS))
    skills: dict[str, bool] = field(default_factory=dict)
    providers: dict[str, object] = field(default_factory=lambda: dict(_DEFAULT_PROVIDER_SETTINGS))
    budget: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        """Export as a JSON-safe dict for control panel / TUI consumption."""
        return {
            "version": self.version,
            "models": dict(sorted(self.models.items())),
            "skills": dict(sorted(self.skills.items())),
            "providers": dict(sorted(self.providers.items())),
            "budget": dict(sorted(self.budget.items())),
        }

    def get_model_for_tier(self, tier: str) -> str | None:
        """Return the configured model for a tier, or None."""
        return self.models.get(tier)

    def set_model_for_tier(self, tier: str, model_id: str) -> None:
        """Set the model for a tier."""
        if tier not in _MODEL_TIERS:
            raise ConfigError(f"unknown tier {tier!r}; valid: {_MODEL_TIERS}")
        self.models[tier] = model_id

    def get_skill_active(self, skill_id: str) -> bool:
        """Return whether a skill is active (default: True)."""
        return self.skills.get(skill_id, True)

    def set_skill_active(self, skill_id: str, active: bool) -> None:
        """Set whether a skill is active."""
        self.skills[skill_id] = active

    def get_budget_limit(self, matter: str) -> float | None:
        """Return the budget limit for a matter, or None if unset."""
        return self.budget.get(matter)

    def set_budget_limit(self, matter: str, limit_usd: float) -> None:
        """Set the budget limit for a matter (never hard-blocks)."""
        if limit_usd < 0:
            raise ConfigError("budget limit must be non-negative")
        self.budget[matter] = limit_usd


# ---------------------------------------------------------------------------
# Thread-safe config singleton
# ---------------------------------------------------------------------------


class ConfigManager:
    """Thread-safe configuration manager.

    Usage::

        mgr = get_config_manager()
        config = mgr.load()
        config.set_model_for_tier("flash_worker", "deepseek/deepseek-v4-flash")
        mgr.save(config)
    """

    def __init__(self, config_path: Path | str | None = None) -> None:
        self._path = Path(config_path) if config_path else self._resolve_path()
        self._lock = threading.Lock()
        self._cache: AtticusConfig | None = None

    @staticmethod
    def _resolve_path() -> Path:
        return Path(os.environ.get("ATTICUS_CONFIG_PATH", str(_DEFAULT_CONFIG_PATH)))

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> AtticusConfig:
        """Load configuration from disk, falling back to defaults.

        Thread-safe: returns a fresh copy on each call.
        """
        with self._lock:
            return self._load_impl()

    def _load_impl(self) -> AtticusConfig:
        """Internal load without lock (caller must hold lock)."""
        if self._cache is not None:
            return _copy_config(self._cache)

        raw = self._read_file()
        if raw is None:
            config = self._defaults()
        else:
            config = self._parse(raw)
            config = self._migrate(config)
            config = self._validate_and_fill(config)

        self._cache = config
        return _copy_config(config)

    def save(self, config: AtticusConfig) -> None:
        """Persist configuration to disk immediately.

        Thread-safe: serialises writes.
        """
        with self._lock:
            self._cache = _copy_config(config)
            self._write_file(config)

    def invalidate_cache(self) -> None:
        """Clear the in-memory cache so next load re-reads disk."""
        with self._lock:
            self._cache = None

    # -- file I/O --

    def _read_file(self) -> dict[str, object] | None:
        try:
            text = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ConfigError(f"cannot read config {self._path}: {exc}") from exc

        if not text.strip():
            return None

        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config {self._path} is not valid JSON: {exc}") from exc

        if not isinstance(raw, Mapping):
            raise ConfigError("config root must be a JSON object")
        return {str(k): v for k, v in cast(Mapping[object, object], raw).items()}

    def _write_file(self, config: AtticusConfig) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = config.as_dict()
        text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        self._path.write_text(text, encoding="utf-8")

    # -- schema validation --

    def _validate_and_fill(self, raw: dict[str, object]) -> AtticusConfig:
        """Validate against schema and build an AtticusConfig, filling defaults."""
        version = _int_or(raw.get("version"), _CONFIG_VERSION)

        models_raw = raw.get("models")
        if models_raw is not None and not isinstance(models_raw, Mapping):
            raise ConfigError("config.models must be an object")
        models = dict(_DEFAULT_MODELS)
        if models_raw is not None:
            for k, v in cast(Mapping[object, object], models_raw).items():
                models[str(k)] = str(v)

        skills_raw = raw.get("skills")
        if skills_raw is not None and not isinstance(skills_raw, Mapping):
            raise ConfigError("config.skills must be an object")
        skills: dict[str, bool] = {}
        if skills_raw is not None:
            for k, v in cast(Mapping[object, object], skills_raw).items():
                skills[str(k)] = bool(v)

        providers_raw = raw.get("providers")
        if providers_raw is not None and not isinstance(providers_raw, Mapping):
            raise ConfigError("config.providers must be an object")
        providers = dict(_DEFAULT_PROVIDER_SETTINGS)
        if providers_raw is not None:
            for k, v in cast(Mapping[object, object], providers_raw).items():
                providers[str(k)] = v

        budget_raw = raw.get("budget")
        if budget_raw is not None and not isinstance(budget_raw, Mapping):
            raise ConfigError("config.budget must be an object")
        budget: dict[str, float] = {}
        if budget_raw is not None:
            for k, v in cast(Mapping[object, object], budget_raw).items():
                budget[str(k)] = float(str(v))

        return AtticusConfig(
            version=version,
            models=models,
            skills=skills,
            providers=providers,
            budget=budget,
        )

    def _parse(self, raw: dict[str, object]) -> dict[str, object]:
        """Parse and shallow-validate raw JSON into a dict."""
        if not isinstance(raw, Mapping):
            raise ConfigError("config root must be a JSON object")
        return {str(k): v for k, v in cast(Mapping[object, object], raw).items()}

    def _migrate(self, raw: dict[str, object]) -> dict[str, object]:
        """Apply config migrations for version changes."""
        version = _int_or(raw.get("version"), 1)

        if version < 2:
            # v1→v2: add tier model defaults if missing
            raw = dict(raw)
            raw["version"] = 2
            models = raw.get("models")
            if isinstance(models, dict):
                models = dict(models)
                for tier, default_model in _DEFAULT_MODELS.items():
                    if tier not in models:
                        models[tier] = default_model
                raw["models"] = models

        return raw

    def _defaults(self) -> AtticusConfig:
        return AtticusConfig(
            version=_CONFIG_VERSION,
            models=dict(_DEFAULT_MODELS),
            skills={},
            providers=dict(_DEFAULT_PROVIDER_SETTINGS),
            budget={},
        )


# ---------------------------------------------------------------------------
# Helper data for TUI config screens
# ---------------------------------------------------------------------------

# OpenRouter models available for each tier (derived from the model policy defaults)
_AVAILABLE_OPENROUTER_MODELS: list[dict[str, str]] = [
    {"id": "deepseek/deepseek-v4-flash", "label": "DeepSeek V4 Flash (fast, cheap)"},
    {"id": "deepseek/deepseek-v4-pro", "label": "DeepSeek V4 Pro (reasoning)"},
    {"id": "deepseek/deepseek-v4-ultra", "label": "DeepSeek V4 Ultra (max)"},
    {"id": "deepseek/deepseek-chat", "label": "DeepSeek Chat (stable)"},
    {"id": "deepseek/deepseek-r1", "label": "DeepSeek R1 (reasoning)"},
    {"id": "google/gemini-2.5-flash", "label": "Gemini 2.5 Flash"},
    {"id": "google/gemini-2.5-pro", "label": "Gemini 2.5 Pro"},
    {"id": "anthropic/claude-sonnet-4", "label": "Claude Sonnet 4"},
    {"id": "anthropic/claude-opus-4", "label": "Claude Opus 4"},
    {"id": "openai/gpt-4o", "label": "GPT-4o"},
    {"id": "openai/gpt-4.1-mini", "label": "GPT-4.1 Mini"},
    {"id": "meta-llama/llama-4-maverick", "label": "Llama 4 Maverick"},
    {"id": "qwen/qwen3-235b-a22b", "label": "Qwen 3 235B"},
]


def get_available_models() -> list[dict[str, str]]:
    """Return the list of known OpenRouter models available for selection."""
    return _AVAILABLE_OPENROUTER_MODELS


def validate_model_id(model_id: str) -> bool:
    """Check whether a model ID is in the registry."""
    for entry in _AVAILABLE_OPENROUTER_MODELS:
        if entry["id"] == model_id:
            return True
    return False


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_config_manager: ConfigManager | None = None


def get_config_manager(*, config_path: Path | str | None = None) -> ConfigManager:
    """Return the global ConfigManager singleton."""
    global _config_manager
    if _config_manager is None:
        _config_manager = ConfigManager(config_path=config_path)
    return _config_manager


# ---------------------------------------------------------------------------
# Convenience functions for operator_control.py / TUI / CLI
# ---------------------------------------------------------------------------


def _copy_config(config: AtticusConfig) -> AtticusConfig:
    """Return a shallow copy of a config."""
    return AtticusConfig(
        version=config.version,
        models=dict(config.models),
        skills=dict(config.skills),
        providers=dict(config.providers),
        budget=dict(config.budget),
    )


def _int_or(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default
