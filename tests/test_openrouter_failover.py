from __future__ import annotations

from collections.abc import Callable

import pytest

from atticus.providers.openrouter import OpenRouterError
from atticus.providers.openrouter_failover import (
    OpenRouterFailoverConfig,
    OpenRouterFailoverHardError,
    OpenRouterModelFailover,
    classify_openrouter_failure,
    clear_shared_failover_clients_for_tests,
    openrouter_client_for_policy,
    openrouter_models_for_policy,
    safe_openrouter_error_message,
    shared_failover_cache_keys_for_tests,
)

JsonObject = dict[str, object]


class SequencedOpenRouterClient:
    def __init__(self, outcomes: list[JsonObject | Exception]) -> None:
        self.outcomes: list[JsonObject | Exception] = list(outcomes)
        self.calls: list[str] = []

    def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int, temperature: float) -> JsonObject:
        del messages, max_tokens, temperature
        self.calls.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        response = dict(outcome)
        _ = response.setdefault("provider", "openrouter")
        _ = response.setdefault("model", model)
        _ = response.setdefault("content", {"ok": True})
        _ = response.setdefault("usage", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        return response


def _failover(
    client: SequencedOpenRouterClient,
    *,
    max_failed_cycles: int = 1,
    event_sink: Callable[[JsonObject], None] | None = None,
    sleep: Callable[[float], None] | None = None,
) -> OpenRouterModelFailover:
    return OpenRouterModelFailover(
        config=OpenRouterFailoverConfig(
            provider="openrouter",
            models=("model-a:free", "model-b:free"),
            max_failed_cycles=max_failed_cycles,
            cooldown_seconds=3.0,
            backoff_seconds=0.0,
            jitter_seconds=0.0,
        ),
        client=client,
        sleep=sleep or (lambda seconds: None),
        event_sink=event_sink,
    )


def test_openrouter_failover_rotates_requested_models_on_recoverable_error():
    client = SequencedOpenRouterClient([OpenRouterError("rate limit", status_code=429), {"model": "model-b:free"}])
    response = _failover(client).chat_json(model="ignored", messages=[{"role": "user", "content": "{}"}])

    assert client.calls == ["model-a:free", "model-b:free"]
    assert response["requested_model"] == "model-b:free"
    assert response["model"] == "model-b:free"


def test_openrouter_failover_returns_first_model_success():
    client = SequencedOpenRouterClient([{"model": "model-a:free"}])
    response = _failover(client).chat_json(model="ignored", messages=[{"role": "user", "content": "{}"}])

    assert client.calls == ["model-a:free"]
    assert response["requested_model"] == "model-a:free"


def test_openrouter_failover_wraps_from_end_to_first_model():
    client = SequencedOpenRouterClient([OpenRouterError("provider unavailable", status_code=503), {"model": "model-a:free"}])
    failover = _failover(client)
    failover.current_index = 1

    response = failover.chat_json(model="ignored", messages=[{"role": "user", "content": "{}"}])

    assert client.calls == ["model-b:free", "model-a:free"]
    assert response["requested_model"] == "model-a:free"


def test_openrouter_failover_hard_errors_do_not_rotate():
    client = SequencedOpenRouterClient([OpenRouterError("OPENROUTER_API_KEY is required")])

    with pytest.raises(OpenRouterFailoverHardError, match="OPENROUTER_API_KEY"):
        _ = _failover(client).chat_json(model="ignored", messages=[{"role": "user", "content": "{}"}])

    assert client.calls == ["model-a:free"]


def test_openrouter_failover_fails_closed_on_usage_and_provider_metadata_errors():
    for error in (
        OpenRouterError("OpenRouter usage metadata must be a JSON object"),
        OpenRouterError("OpenRouter response missing provider/model metadata required for fallback detection"),
    ):
        recoverable, reason = classify_openrouter_failure(error)
        assert recoverable is False
        assert reason

    client = SequencedOpenRouterClient([{"usage": ["not", "metadata"]}, {"model": "model-b:free"}])

    with pytest.raises(OpenRouterFailoverHardError, match="usage metadata"):
        _ = _failover(client).chat_json(model="ignored", messages=[{"role": "user", "content": "{}"}])

    assert client.calls == ["model-a:free"]


def test_openrouter_failover_cools_down_after_failed_cycles_and_continues_from_first_model():
    sleeps: list[float] = []
    events: list[JsonObject] = []
    client = SequencedOpenRouterClient([
        OpenRouterError("rate limit", status_code=429),
        OpenRouterError("rate limit", status_code=429),
        OpenRouterError("rate limit", status_code=429),
        OpenRouterError("rate limit", status_code=429),
        {"model": "model-a:free"},
    ])

    response = _failover(client, max_failed_cycles=2, event_sink=events.append, sleep=sleeps.append).chat_json(
        model="ignored",
        messages=[{"role": "user", "content": "{}"}],
    )

    assert client.calls == ["model-a:free", "model-b:free", "model-a:free", "model-b:free", "model-a:free"]
    assert response["requested_model"] == "model-a:free"
    assert sleeps == [3.0]
    assert any(event["event"] == "cooldown_start" for event in events)
    assert any(event["event"] == "cooldown_end" and event["model"] == "model-a:free" for event in events)
    assert events[-1]["event"] == "model_success"


def test_openrouter_failover_bounded_attempt_guard_is_explicit():
    client = SequencedOpenRouterClient([OpenRouterError("rate limit", status_code=429) for _ in range(3)])

    with pytest.raises(OpenRouterError, match="max_total_attempts exceeded after 3 attempts"):
        _ = _failover(client, max_failed_cycles=1).chat_json(
            model="ignored",
            messages=[{"role": "user", "content": "{}"}],
            max_total_attempts=3,
        )

    assert client.calls == ["model-a:free", "model-b:free", "model-a:free"]


def test_openrouter_failover_retains_successful_model_for_next_request():
    client = SequencedOpenRouterClient([
        OpenRouterError("rate limit", status_code=429),
        {"model": "model-b:free"},
        {"model": "model-b:free"},
    ])
    failover = _failover(client)

    first = failover.chat_json(model="ignored", messages=[{"role": "user", "content": "{}"}])
    second = failover.chat_json(model="ignored", messages=[{"role": "user", "content": "{}"}])

    assert client.calls == ["model-a:free", "model-b:free", "model-b:free"]
    assert first["requested_model"] == "model-b:free"
    assert second["requested_model"] == "model-b:free"


def test_openrouter_failover_enabled_by_env_uses_ordered_requested_model_list():
    models = openrouter_models_for_policy(
        {"provider": "openrouter", "model": "ignored-model"},
        env={
            "ATTICUS_OPENROUTER_FAILOVER_ENABLED": "1",
            "ATTICUS_OPENROUTER_FAILOVER_MODELS": "model-b:free, model-a:free",
        },
    )

    assert models == ("model-b:free", "model-a:free")


def test_openrouter_failover_enabled_by_env_requires_explicit_model_list():
    with pytest.raises(OpenRouterFailoverHardError, match="explicit OpenRouter failover models"):
        _ = openrouter_models_for_policy(
            {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            env={"ATTICUS_OPENROUTER_FAILOVER_ENABLED": "1"},
        )


@pytest.mark.parametrize("models", [{"bad": "shape"}, [], ["model-a:free", ""], "  ,  "])
def test_openrouter_failover_rejects_malformed_explicit_model_config(models: object) -> None:
    with pytest.raises(OpenRouterFailoverHardError, match="models"):
        _ = openrouter_models_for_policy(
            {"provider": "openrouter", "openrouter_failover": {"enabled": True, "models": models}},
            env={},
        )


def test_openrouter_failover_rejects_non_finite_timing_values() -> None:
    with pytest.raises(OpenRouterFailoverHardError, match="finite"):
        _ = openrouter_models_for_policy(
            {
                "provider": "openrouter",
                "openrouter_failover": {
                    "enabled": True,
                    "models": ["model-a:free"],
                    "cooldown_seconds": "NaN",
                },
            },
            env={},
        )


def test_openrouter_failover_classifier_defaults_unknown_errors_to_hard_failure():
    recoverable, reason = classify_openrouter_failure(OpenRouterError("unknown provider failure"))

    assert recoverable is False
    assert reason == "unknown provider failure"


def test_openrouter_failover_does_not_cache_raw_api_key_in_cache_keys():
    clear_shared_failover_clients_for_tests()
    raw_value = "fake-openrouter-value-not-in-cache-key"
    _ = openrouter_client_for_policy(
        {
            "provider": "openrouter",
            "openrouter_failover": {"enabled": True, "models": ["model-a:free"], "cooldown_seconds": 0},
        },
        env={"OPENROUTER_API_KEY": raw_value},
    )

    cache_keys = shared_failover_cache_keys_for_tests()
    assert cache_keys
    assert all(raw_value not in repr(key) for key in cache_keys)


def test_safe_openrouter_error_message_removes_provider_body():
    error = OpenRouterError("OpenRouter HTTP 401: secret diagnostic body", status_code=401, body="secret diagnostic body")

    assert safe_openrouter_error_message(error) == "OpenRouter HTTP 401"
