"""OpenRouter model failover and rotation.

The failover client is provider-layer plumbing only. It returns the same
candidate packet response shape as ``OpenRouterClient`` and never writes
canonical state; reducers and validation gates remain the only canonical path.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
import os
import random
import time
from collections.abc import Callable, Mapping
from typing import Protocol, cast

from atticus.providers.deepseek import known_model
from atticus.providers.openrouter import OpenRouterClient, OpenRouterError, validate_cache_usage_tokens, validate_usage_tokens

FAILOVER_POLICY_KEY = "openrouter_failover"
ENV_FAILOVER_ENABLED = "ATTICUS_OPENROUTER_FAILOVER_ENABLED"
ENV_FAILOVER_MODELS = "ATTICUS_OPENROUTER_FAILOVER_MODELS"
ENV_FAILOVER_MAX_FAILED_CYCLES = "ATTICUS_OPENROUTER_FAILOVER_MAX_FAILED_CYCLES"
ENV_FAILOVER_COOLDOWN_SECONDS = "ATTICUS_OPENROUTER_FAILOVER_COOLDOWN_SECONDS"
ENV_FAILOVER_PER_MODEL_TIMEOUT_SECONDS = "ATTICUS_OPENROUTER_FAILOVER_PER_MODEL_TIMEOUT_SECONDS"
ENV_FAILOVER_BACKOFF_SECONDS = "ATTICUS_OPENROUTER_FAILOVER_BACKOFF_SECONDS"
ENV_FAILOVER_JITTER_SECONDS = "ATTICUS_OPENROUTER_FAILOVER_JITTER_SECONDS"
ENV_FAILOVER_LOG_EVENTS = "ATTICUS_OPENROUTER_FAILOVER_LOG_EVENTS"
OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"

DEFAULT_MAX_FAILED_CYCLES = 5
DEFAULT_COOLDOWN_SECONDS = 300.0
DEFAULT_PER_MODEL_TIMEOUT_SECONDS = 120.0
DEFAULT_BACKOFF_SECONDS = 0.25
DEFAULT_JITTER_SECONDS = 0.25

_LOGGER = logging.getLogger("atticus.providers.openrouter_failover")
_SHARED_FAILOVER_CLIENTS: dict[tuple[object, ...], "OpenRouterModelFailover"] = {}


class ChatJsonClient(Protocol):
    timeout: float

    def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, object]: ...


class OpenRouterFailoverHardError(OpenRouterError):
    """Raised for caller/config/auth errors where rotation cannot help."""


@dataclass(frozen=True)
class OpenRouterFailoverConfig:
    provider: str
    models: tuple[str, ...]
    max_failed_cycles: int = DEFAULT_MAX_FAILED_CYCLES
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS
    per_model_timeout_seconds: float = DEFAULT_PER_MODEL_TIMEOUT_SECONDS
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    jitter_seconds: float = DEFAULT_JITTER_SECONDS
    log_events: bool = False

    def cache_key(self) -> tuple[object, ...]:
        return (
            self.provider,
            self.models,
            self.max_failed_cycles,
            self.cooldown_seconds,
            self.per_model_timeout_seconds,
            self.backoff_seconds,
            self.jitter_seconds,
            self.log_events,
        )


class OpenRouterModelFailover:
    """Stateful OpenRouter chat client that rotates through configured models."""

    def __init__(
        self,
        *,
        config: OpenRouterFailoverConfig,
        client: object | None = None,
        client_factory: Callable[[float], object] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        event_sink: Callable[[dict[str, object]], None] | None = None,
    ) -> None:
        _validate_failover_config(config)
        self.config: OpenRouterFailoverConfig = config
        self.client: object | None = client
        self.client_factory: Callable[[float], object] | None = client_factory
        self.sleep: Callable[[float], None] = sleep
        self.jitter: Callable[[float, float], float] = jitter
        self.event_sink: Callable[[dict[str, object]], None] | None = event_sink
        self.current_index: int = 0

    @property
    def current_model(self) -> str:
        return self.config.models[self.current_index]

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 4096,
        temperature: float = 0.1,
        max_total_attempts: int | None = None,
    ) -> dict[str, object]:
        del model
        self._validate_request(messages=messages)
        if max_total_attempts is not None and max_total_attempts < 1:
            raise OpenRouterFailoverHardError("OpenRouter failover max_total_attempts must be >= 1 when set")
        if max_total_attempts is None:
            max_total_attempts = len(self.config.models) * (self.config.max_failed_cycles + 1)
        failed_cycles = 0
        attempts_in_cycle = 0
        attempt_number = 0

        while True:
            if attempt_number >= max_total_attempts:
                self._emit(
                    "failover_attempt_guard_exceeded",
                    model=self.current_model,
                    attempt_number=attempt_number,
                    failed_cycle_count=failed_cycles,
                    reason=f"max_total_attempts {max_total_attempts} reached",
                )
                raise OpenRouterError(f"OpenRouter failover max_total_attempts exceeded after {attempt_number} attempts across {len(self.config.models)} configured models")
            attempted_model = self.current_model
            attempt_number += 1
            attempts_in_cycle += 1
            self._emit(
                "model_attempt",
                model=attempted_model,
                attempt_number=attempt_number,
                failed_cycle_count=failed_cycles,
            )
            try:
                response = self._call_model(
                    model=attempted_model,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                normalized = self._normalize_success_response(response, requested_model=attempted_model)
            except OpenRouterError as exc:
                recoverable, reason = classify_openrouter_failure(exc)
                if not recoverable:
                    self._emit(
                        "hard_error",
                        model=attempted_model,
                        attempt_number=attempt_number,
                        failed_cycle_count=failed_cycles,
                        reason=reason,
                    )
                    raise OpenRouterFailoverHardError(reason, status_code=getattr(exc, "status_code", None), body=getattr(exc, "body", "")) from exc
                self._emit(
                    "model_failure",
                    model=attempted_model,
                    attempt_number=attempt_number,
                    failed_cycle_count=failed_cycles,
                    reason=reason,
                )
                self._advance_after_failure(
                    attempt_number=attempt_number,
                    failed_cycle_count=failed_cycles,
                    reason=reason,
                )
                if attempts_in_cycle >= len(self.config.models):
                    failed_cycles += 1
                    attempts_in_cycle = 0
                    if failed_cycles >= self.config.max_failed_cycles:
                        self._cooldown(attempt_number=attempt_number, failed_cycle_count=failed_cycles)
                        failed_cycles = 0
                    else:
                        self._sleep_between_failures()
                else:
                    self._sleep_between_failures()
                continue

            self._emit(
                "model_success",
                model=attempted_model,
                attempt_number=attempt_number,
                failed_cycle_count=failed_cycles,
                reason="success",
            )
            return normalized

    def _validate_request(self, *, messages: object) -> None:
        if not isinstance(messages, list) or not messages:
            raise OpenRouterFailoverHardError("OpenRouter failover request must include non-empty messages")
        message_items = cast(list[object], messages)
        for index, message in enumerate(message_items):
            if not isinstance(message, Mapping):
                raise OpenRouterFailoverHardError(f"OpenRouter failover messages[{index}] must be a JSON object")
            message_map = cast(Mapping[object, object], message)
            if not str(message_map.get("content") or "").strip():
                raise OpenRouterFailoverHardError(f"OpenRouter failover messages[{index}] must include non-empty content")

    def _call_model(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> dict[str, object]:
        client = self._client_for_attempt()
        return client.chat_json(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)

    def _client_for_attempt(self) -> ChatJsonClient:
        if self.client_factory is not None:
            return cast(ChatJsonClient, self.client_factory(self.config.per_model_timeout_seconds))
        if self.client is not None:
            client_obj = self.client
            if hasattr(client_obj, "timeout"):
                setattr(client_obj, "timeout", self.config.per_model_timeout_seconds)
            return cast(ChatJsonClient, client_obj)
        return OpenRouterClient(timeout=self.config.per_model_timeout_seconds)

    def _normalize_success_response(self, response: object, *, requested_model: str) -> dict[str, object]:
        if not isinstance(response, Mapping):
            raise OpenRouterError("OpenRouter response must be a JSON object")
        response = cast(Mapping[object, object], response)
        provider = response.get("provider")
        actual_model = response.get("model")
        content = response.get("content")
        usage = response.get("usage")
        if not provider or not actual_model:
            raise OpenRouterError("OpenRouter response missing provider/model metadata required for fallback detection")
        if content in (None, ""):
            raise OpenRouterError("OpenRouter response content was empty")
        if not isinstance(usage, Mapping):
            raise OpenRouterError("OpenRouter usage metadata must be a JSON object")
        usage_dict = _mapping_to_dict(cast(Mapping[object, object], usage))
        _ = validate_usage_tokens(usage_dict)
        _ = validate_cache_usage_tokens(usage_dict)
        normalized = _mapping_to_dict(response)
        normalized["provider"] = str(provider)
        normalized["model"] = str(actual_model)
        normalized["requested_model"] = requested_model
        normalized["usage"] = usage_dict
        return normalized

    def _advance_after_failure(self, *, attempt_number: int, failed_cycle_count: int, reason: str) -> None:
        previous_model = self.current_model
        self.current_index = (self.current_index + 1) % len(self.config.models)
        self._emit(
            "failover_advance",
            model=previous_model,
            next_model=self.current_model,
            attempt_number=attempt_number,
            failed_cycle_count=failed_cycle_count,
            reason=reason,
        )

    def _sleep_between_failures(self) -> None:
        delay = self.config.backoff_seconds
        if self.config.jitter_seconds > 0:
            delay += max(0.0, self.jitter(0.0, self.config.jitter_seconds))
        if delay > 0:
            self.sleep(delay)

    def _cooldown(self, *, attempt_number: int, failed_cycle_count: int) -> None:
        self._emit(
            "cooldown_start",
            model=self.current_model,
            attempt_number=attempt_number,
            failed_cycle_count=failed_cycle_count,
            reason=f"{failed_cycle_count} full model cycles failed",
        )
        if self.config.cooldown_seconds > 0:
            self.sleep(self.config.cooldown_seconds)
        self.current_index = 0
        self._emit(
            "cooldown_end",
            model=self.current_model,
            attempt_number=attempt_number,
            failed_cycle_count=0,
            reason="retrying from first configured model",
        )

    def _emit(self, event: str, **fields: object) -> None:
        payload = {"event": event, "provider": self.config.provider, **fields}
        if self.event_sink is not None:
            self.event_sink(payload)
        if self.config.log_events:
            _LOGGER.info(json.dumps(payload, sort_keys=True))


def clear_shared_failover_clients_for_tests() -> None:
    _SHARED_FAILOVER_CLIENTS.clear()


def shared_failover_cache_keys_for_tests() -> tuple[tuple[object, ...], ...]:
    return tuple(_SHARED_FAILOVER_CLIENTS)


def classify_openrouter_failure(exc: OpenRouterError) -> tuple[bool, str]:
    status_code = getattr(exc, "status_code", None)
    body = str(getattr(exc, "body", "") or "")
    text = f"{exc} {body}".lower()
    reason = safe_openrouter_error_message(exc)
    hard_terms = (
        "api key",
        "api_key",
        "unauthorized",
        "invalid auth",
        "invalid api",
        "permission denied",
        "forbidden",
        "invalid request",
        "request schema",
        "missing messages",
        "missing prompt",
        "context length",
        "context_length",
        "maximum context",
        "too many tokens",
        "input too large",
        "missing provider/model metadata",
        "provider/model metadata",
        "usage metadata",
        "usage field",
    )
    recoverable_terms = (
        "rate limit",
        "rate_limit",
        "too many requests",
        "overload",
        "overloaded",
        "temporarily unavailable",
        "provider unavailable",
        "no endpoints",
        "server error",
        "bad gateway",
        "gateway timeout",
        "timed out",
        "timeout",
        "connection reset",
        "network error",
        "request failed",
        "invalid json",
        "json object",
        "json message",
        "content was empty",
    )
    if status_code in {400, 401, 402, 403, 413, 422} or any(term in text for term in hard_terms):
        return False, reason
    if status_code == 429 or any(term in text for term in recoverable_terms):
        return True, reason
    if status_code is not None and (status_code == 408 or status_code >= 500):
        return True, reason
    return False, reason


def safe_openrouter_error_message(exc: BaseException) -> str:
    if isinstance(exc, OpenRouterError):
        status_code = getattr(exc, "status_code", None)
        if status_code is not None:
            return f"OpenRouter HTTP {status_code}"
    return str(exc)


def openrouter_failover_config_from_policy(
    provider_policy: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
    live: bool = False,
) -> OpenRouterFailoverConfig | None:
    env = env if env is not None else os.environ
    raw = provider_policy.get(FAILOVER_POLICY_KEY)
    raw_config = _mapping_to_dict(cast(Mapping[object, object], raw)) if isinstance(raw, Mapping) else {}
    enabled = _bool_value(raw_config.get("enabled"), default=False)
    if not enabled:
        enabled = _env_bool(env.get(ENV_FAILOVER_ENABLED), default=False)
    if not enabled:
        return None
    provider = str(provider_policy.get("provider") or "openrouter")
    if "models" in raw_config:
        models = _models_value(raw_config.get("models"), source="policy")
    elif ENV_FAILOVER_MODELS in env:
        models = _models_value(env.get(ENV_FAILOVER_MODELS), source="env")
    else:
        raise OpenRouterFailoverHardError(
            f"OpenRouter failover requires explicit OpenRouter failover models; set {ENV_FAILOVER_MODELS} or openrouter_failover.models"
        )
    config = OpenRouterFailoverConfig(
        provider=provider,
        models=models,
        max_failed_cycles=_int_value(raw_config.get("max_failed_cycles"), env.get(ENV_FAILOVER_MAX_FAILED_CYCLES), default=DEFAULT_MAX_FAILED_CYCLES),
        cooldown_seconds=_float_value(raw_config.get("cooldown_seconds"), env.get(ENV_FAILOVER_COOLDOWN_SECONDS), default=DEFAULT_COOLDOWN_SECONDS),
        per_model_timeout_seconds=_float_value(raw_config.get("per_model_timeout_seconds"), env.get(ENV_FAILOVER_PER_MODEL_TIMEOUT_SECONDS), default=DEFAULT_PER_MODEL_TIMEOUT_SECONDS),
        backoff_seconds=_float_value(raw_config.get("backoff_seconds"), env.get(ENV_FAILOVER_BACKOFF_SECONDS), default=DEFAULT_BACKOFF_SECONDS),
        jitter_seconds=_float_value(raw_config.get("jitter_seconds"), env.get(ENV_FAILOVER_JITTER_SECONDS), default=DEFAULT_JITTER_SECONDS),
        log_events=_bool_value(raw_config.get("log_events"), default=_env_bool(env.get(ENV_FAILOVER_LOG_EVENTS), default=False)),
    )
    _validate_failover_config(config, env=env, live=live)
    return config


def openrouter_client_for_policy(
    provider_policy: Mapping[str, object],
    *,
    env: Mapping[str, str] | None = None,
    live: bool = False,
    client: object | None = None,
    event_sink: Callable[[dict[str, object]], None] | None = None,
) -> object:
    config = openrouter_failover_config_from_policy(provider_policy, env=env, live=live)
    if config is None:
        return client
    if client is not None:
        return OpenRouterModelFailover(config=config, client=client, event_sink=event_sink)
    env = env if env is not None else os.environ
    if event_sink is not None:
        return OpenRouterModelFailover(
            config=config,
            client=OpenRouterClient(api_key=env.get(OPENROUTER_KEY_ENV, ""), timeout=config.per_model_timeout_seconds),
            event_sink=event_sink,
        )
    key = config.cache_key()
    failover_client = _SHARED_FAILOVER_CLIENTS.get(key)
    if failover_client is None:
        failover_client = OpenRouterModelFailover(
            config=config,
            client=OpenRouterClient(api_key=env.get(OPENROUTER_KEY_ENV, ""), timeout=config.per_model_timeout_seconds),
        )
        _SHARED_FAILOVER_CLIENTS[key] = failover_client
    return failover_client


def primary_model_for_policy(provider_policy: Mapping[str, object], *, env: Mapping[str, str] | None = None, live: bool = False) -> str:
    models = openrouter_models_for_policy(provider_policy, env=env, live=live)
    return models[0] if models else ""


def openrouter_models_for_policy(provider_policy: Mapping[str, object], *, env: Mapping[str, str] | None = None, live: bool = False) -> tuple[str, ...]:
    config = openrouter_failover_config_from_policy(provider_policy, env=env, live=live)
    if config is not None:
        return config.models
    model = str(provider_policy.get("model") or "").strip()
    return (model,) if model else ()


def _validate_failover_config(config: OpenRouterFailoverConfig, *, env: Mapping[str, str] | None = None, live: bool = False) -> None:
    if config.provider != "openrouter":
        raise OpenRouterFailoverHardError(f"OpenRouter failover requires provider 'openrouter', got {config.provider!r}")
    if not config.models:
        raise OpenRouterFailoverHardError("OpenRouter failover requires at least one model")
    unknown_models = [model for model in config.models if not known_model("openrouter", model, env=env, live=live)]
    if unknown_models:
        raise OpenRouterFailoverHardError(f"OpenRouter failover models are unknown or unsupported: {', '.join(unknown_models)}")
    if config.max_failed_cycles < 1:
        raise OpenRouterFailoverHardError("OpenRouter failover max_failed_cycles must be >= 1")
    if config.cooldown_seconds < 0:
        raise OpenRouterFailoverHardError("OpenRouter failover cooldown_seconds must be non-negative")
    if config.per_model_timeout_seconds <= 0:
        raise OpenRouterFailoverHardError("OpenRouter failover per_model_timeout_seconds must be positive")
    if config.backoff_seconds < 0:
        raise OpenRouterFailoverHardError("OpenRouter failover backoff_seconds must be non-negative")
    if config.jitter_seconds < 0:
        raise OpenRouterFailoverHardError("OpenRouter failover jitter_seconds must be non-negative")


def _models_value(value: object, *, source: str) -> tuple[str, ...]:
    if value is None:
        raise OpenRouterFailoverHardError(f"OpenRouter failover {source} models must be a non-empty string or list")
    if isinstance(value, str):
        parsed_models = tuple(item.strip() for item in value.split(",") if item.strip())
        if not parsed_models:
            raise OpenRouterFailoverHardError(f"OpenRouter failover {source} models must not be empty")
        return parsed_models
    if isinstance(value, list | tuple):
        items = cast(list[object] | tuple[object, ...], value)
        if not items:
            raise OpenRouterFailoverHardError(f"OpenRouter failover {source} models must not be empty")
        models: list[str] = []
        for index, item in enumerate(items):
            if not isinstance(item, str) or not item.strip():
                raise OpenRouterFailoverHardError(f"OpenRouter failover {source} models[{index}] must be a non-empty string")
            models.append(item.strip())
        return tuple(models)
    raise OpenRouterFailoverHardError(f"OpenRouter failover {source} models must be a non-empty string or list")


def _bool_value(value: object, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return _env_bool(str(value), default=default)


def _env_bool(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int_value(primary: object, fallback: object, *, default: int) -> int:
    raw = primary if primary is not None else fallback
    if raw is None:
        return default
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int | str):
        return int(raw)
    raise OpenRouterFailoverHardError(f"OpenRouter failover integer value is invalid: {raw!r}")


def _float_value(primary: object, fallback: object, *, default: float) -> float:
    raw = primary if primary is not None else fallback
    if raw is None:
        return default
    if isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, int | float | str):
        value = float(raw)
        if not math.isfinite(value):
            raise OpenRouterFailoverHardError("OpenRouter failover float value must be finite")
        return value
    raise OpenRouterFailoverHardError(f"OpenRouter failover float value is invalid: {raw!r}")


def _mapping_to_dict(value: Mapping[object, object]) -> dict[str, object]:
    return {str(key): item for key, item in value.items()}
