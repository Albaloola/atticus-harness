from __future__ import annotations

from collections.abc import Callable

import pytest

from atticus.providers.deepseek import (
    ENV_ALLOW_HELD_MODELS_FOR_LIVE,
    ENV_ENABLE_HELD_OPENROUTER_MODELS,
)
from atticus.providers.openrouter import OpenRouterError
from atticus.providers.openrouter_failover import (
    OpenRouterFailoverConfig,
    OpenRouterFailoverHardError,
    OpenRouterModelFailover,
    classify_openrouter_failure,
    clear_shared_failover_clients_for_tests,
    is_429_error,
    openrouter_client_for_policy,
    openrouter_models_for_policy,
    safe_openrouter_error_message,
    shared_failover_cache_keys_for_tests,
)

JsonObject = dict[str, object]
MODEL_A = "deepseek/deepseek-v4-flash"
MODEL_B = "deepseek/deepseek-v4-pro"


class SequencedOpenRouterClient:
    def __init__(self, outcomes: list[JsonObject | Exception]) -> None:
        self.outcomes: list[JsonObject | Exception] = list(outcomes)
        self.calls: list[str] = []

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int,
        temperature: float,
    ) -> JsonObject:
        del messages, max_tokens, temperature
        self.calls.append(model)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        response = dict(outcome)
        _ = response.setdefault("provider", "openrouter")
        _ = response.setdefault("model", model)
        _ = response.setdefault("content", {"ok": True})
        _ = response.setdefault(
            "usage", {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        )
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
            models=(MODEL_A, MODEL_B),
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
    client = SequencedOpenRouterClient(
        [OpenRouterError("rate limit", status_code=429), {"model": MODEL_B}]
    )
    response = _failover(client).chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )

    assert client.calls == [MODEL_A, MODEL_B]
    assert response["requested_model"] == MODEL_B
    assert response["model"] == MODEL_B


def test_openrouter_failover_returns_first_model_success():
    client = SequencedOpenRouterClient([{"model": MODEL_A}])
    response = _failover(client).chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )

    assert client.calls == [MODEL_A]
    assert response["requested_model"] == MODEL_A


def test_openrouter_failover_wraps_from_end_to_first_model():
    client = SequencedOpenRouterClient(
        [OpenRouterError("provider unavailable", status_code=503), {"model": MODEL_A}]
    )
    failover = _failover(client)
    failover.current_index = 1

    response = failover.chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )

    assert client.calls == [MODEL_B, MODEL_A]
    assert response["requested_model"] == MODEL_A


def test_openrouter_failover_hard_errors_do_not_rotate():
    client = SequencedOpenRouterClient(
        [OpenRouterError("OPENROUTER_API_KEY is required")]
    )

    with pytest.raises(OpenRouterFailoverHardError, match="OPENROUTER_API_KEY"):
        _ = _failover(client).chat_json(
            model="ignored", messages=[{"role": "user", "content": "{}"}]
        )

    assert client.calls == [MODEL_A]


def test_openrouter_failover_fails_closed_on_usage_and_provider_metadata_errors():
    for error in (
        OpenRouterError("OpenRouter usage metadata must be a JSON object"),
        OpenRouterError(
            "OpenRouter response missing provider/model metadata required for fallback detection"
        ),
    ):
        recoverable, reason = classify_openrouter_failure(error)
        assert recoverable is False
        assert reason

    client = SequencedOpenRouterClient(
        [{"usage": ["not", "metadata"]}, {"model": MODEL_B}]
    )

    with pytest.raises(OpenRouterFailoverHardError, match="usage metadata"):
        _ = _failover(client).chat_json(
            model="ignored", messages=[{"role": "user", "content": "{}"}]
        )

    assert client.calls == [MODEL_A]


def test_openrouter_failover_cools_down_after_failed_cycles_and_continues_from_first_model():
    sleeps: list[float] = []
    events: list[JsonObject] = []
    client = SequencedOpenRouterClient(
        [
            OpenRouterError("rate limit", status_code=429),
            OpenRouterError("rate limit", status_code=429),
            OpenRouterError("rate limit", status_code=429),
            OpenRouterError("rate limit", status_code=429),
            {"model": MODEL_A},
        ]
    )

    response = _failover(
        client, max_failed_cycles=2, event_sink=events.append, sleep=sleeps.append
    ).chat_json(
        model="ignored",
        messages=[{"role": "user", "content": "{}"}],
    )

    assert client.calls == [MODEL_A, MODEL_B, MODEL_A, MODEL_B, MODEL_A]
    assert response["requested_model"] == MODEL_A
    assert sleeps == [3.0]
    assert any(event["event"] == "cooldown_start" for event in events)
    assert any(
        event["event"] == "cooldown_end" and event["model"] == MODEL_A
        for event in events
    )
    assert events[-1]["event"] == "model_success"


def test_openrouter_failover_bounded_attempt_guard_is_explicit():
    client = SequencedOpenRouterClient(
        [OpenRouterError("rate limit", status_code=429) for _ in range(3)]
    )

    with pytest.raises(
        OpenRouterError, match="max_total_attempts exceeded after 3 attempts"
    ):
        _ = _failover(client, max_failed_cycles=1).chat_json(
            model="ignored",
            messages=[{"role": "user", "content": "{}"}],
            max_total_attempts=3,
        )

    assert client.calls == [MODEL_A, MODEL_B, MODEL_A]


def test_openrouter_failover_is_bounded_by_default():
    client = SequencedOpenRouterClient(
        [OpenRouterError("rate limit", status_code=429) for _ in range(4)]
    )

    with pytest.raises(
        OpenRouterError, match="max_total_attempts exceeded after 4 attempts"
    ):
        _ = _failover(client, max_failed_cycles=1).chat_json(
            model="ignored", messages=[{"role": "user", "content": "{}"}]
        )

    assert client.calls == [MODEL_A, MODEL_B, MODEL_A, MODEL_B]


def test_openrouter_failover_retains_successful_model_for_next_request():
    client = SequencedOpenRouterClient(
        [
            OpenRouterError("rate limit", status_code=429),
            {"model": MODEL_B},
            {"model": MODEL_B},
        ]
    )
    failover = _failover(client)

    first = failover.chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )
    second = failover.chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )

    assert client.calls == [MODEL_A, MODEL_B, MODEL_B]
    assert first["requested_model"] == MODEL_B
    assert second["requested_model"] == MODEL_B


def test_openrouter_failover_enabled_by_env_uses_ordered_requested_model_list():
    models = openrouter_models_for_policy(
        {"provider": "openrouter", "model": "ignored-model"},
        env={
            "ATTICUS_OPENROUTER_FAILOVER_ENABLED": "1",
            "ATTICUS_OPENROUTER_FAILOVER_MODELS": f"{MODEL_B}, {MODEL_A}",
        },
    )

    assert models == (MODEL_B, MODEL_A)


def test_openrouter_failover_enabled_by_env_requires_explicit_model_list():
    with pytest.raises(
        OpenRouterFailoverHardError, match="explicit OpenRouter failover models"
    ):
        _ = openrouter_models_for_policy(
            {"provider": "openrouter", "model": "deepseek/deepseek-v4-pro"},
            env={"ATTICUS_OPENROUTER_FAILOVER_ENABLED": "1"},
        )


def test_openrouter_failover_rejects_unknown_models_in_policy_and_env() -> None:
    with pytest.raises(OpenRouterFailoverHardError, match="unknown or unsupported"):
        _ = openrouter_models_for_policy(
            {
                "provider": "openrouter",
                "openrouter_failover": {"enabled": True, "models": ["unknown/model"]},
            },
            env={},
        )


def test_openrouter_failover_held_models_require_live_opt_in() -> None:
    held_model = "qwen/qwen3-coder:free"
    policy = {
        "provider": "openrouter",
        "openrouter_failover": {"enabled": True, "models": [held_model]},
    }

    live_models = openrouter_models_for_policy(policy, env={}, live=True)
    assert live_models == (held_model,)

    non_live_models = openrouter_models_for_policy(policy, env={}, live=False)
    assert non_live_models == (held_model,)

    live_models_again = openrouter_models_for_policy(
        policy,
        env={
            "ATTICUS_ENABLE_HELD_OPENROUTER_MODELS": "1",
            "ATTICUS_ALLOW_HELD_MODELS_FOR_LIVE": "1",
        },
        live=True,
    )
    assert live_models_again == (held_model,)
    with pytest.raises(OpenRouterFailoverHardError, match="unknown or unsupported"):
        _ = openrouter_models_for_policy(
            {"provider": "openrouter", "model": MODEL_A},
            env={
                "ATTICUS_OPENROUTER_FAILOVER_ENABLED": "1",
                "ATTICUS_OPENROUTER_FAILOVER_MODELS": "unknown/model",
            },
        )


@pytest.mark.parametrize("models", [{"bad": "shape"}, [], [MODEL_A, ""], "  ,  "])
def test_openrouter_failover_rejects_malformed_explicit_model_config(
    models: object,
) -> None:
    with pytest.raises(OpenRouterFailoverHardError, match="models"):
        _ = openrouter_models_for_policy(
            {
                "provider": "openrouter",
                "openrouter_failover": {"enabled": True, "models": models},
            },
            env={},
        )


def test_openrouter_failover_rejects_non_finite_timing_values() -> None:
    with pytest.raises(OpenRouterFailoverHardError, match="finite"):
        _ = openrouter_models_for_policy(
            {
                "provider": "openrouter",
                "openrouter_failover": {
                    "enabled": True,
                    "models": [MODEL_A],
                    "cooldown_seconds": "NaN",
                },
            },
            env={},
        )


def test_openrouter_failover_classifier_defaults_unknown_errors_to_hard_failure():
    recoverable, reason = classify_openrouter_failure(
        OpenRouterError("unknown provider failure")
    )

    assert recoverable is False
    assert reason == "unknown provider failure"


def test_openrouter_failover_does_not_cache_raw_api_key_in_cache_keys():
    clear_shared_failover_clients_for_tests()
    raw_value = "fake-openrouter-value-not-in-cache-key"
    _ = openrouter_client_for_policy(
        {
            "provider": "openrouter",
            "openrouter_failover": {
                "enabled": True,
                "models": [MODEL_A],
                "cooldown_seconds": 0,
            },
        },
        env={"OPENROUTER_API_KEY": raw_value},
    )

    cache_keys = shared_failover_cache_keys_for_tests()
    assert cache_keys
    assert all(raw_value not in repr(key) for key in cache_keys)


def test_safe_openrouter_error_message_removes_provider_body():
    error = OpenRouterError(
        "OpenRouter HTTP 401: secret diagnostic body",
        status_code=401,
        body="secret diagnostic body",
    )

    assert safe_openrouter_error_message(error) == "OpenRouter HTTP 401"


def test_is_429_error_detection():
    assert is_429_error(OpenRouterError("rate limit", status_code=429))
    assert is_429_error(OpenRouterError("too many requests", status_code=429))
    assert is_429_error(
        OpenRouterError("rate limited", status_code=200, body="rate_limit exceeded")
    )
    assert not is_429_error(OpenRouterError("server error", status_code=500))
    assert not is_429_error(OpenRouterError("provider unavailable", status_code=503))


def test_429_exponential_backoff_with_jitter_disabled():
    sleeps: list[float] = []
    client = SequencedOpenRouterClient(
        [OpenRouterError("rate limit", status_code=429) for _ in range(7)]
        + [{"model": MODEL_A}]
    )

    failover = OpenRouterModelFailover(
        config=OpenRouterFailoverConfig(
            provider="openrouter",
            models=(MODEL_A, MODEL_B),
            max_failed_cycles=4,
            cooldown_seconds=3.0,
            backoff_seconds=0.25,
            jitter_seconds=0.0,
        ),
        client=client,
        sleep=sleeps.append,
    )

    response = failover.chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )

    assert response["model"] == MODEL_A
    assert len(sleeps) == 7
    assert sleeps[0] == 0.25
    assert sleeps[1] == pytest.approx(0.5)
    assert sleeps[2] == pytest.approx(1.0)
    assert sleeps[3] == pytest.approx(2.0)
    assert sleeps[4] == pytest.approx(4.0)
    assert sleeps[5] == pytest.approx(8.0)
    assert sleeps[6] == pytest.approx(16.0)


def test_429_backoff_capped_at_max():
    sleeps: list[float] = []
    client = SequencedOpenRouterClient(
        [OpenRouterError("rate limit", status_code=429) for _ in range(15)]
        + [{"model": MODEL_A}]
    )

    failover = OpenRouterModelFailover(
        config=OpenRouterFailoverConfig(
            provider="openrouter",
            models=(MODEL_A, MODEL_B),
            max_failed_cycles=8,
            cooldown_seconds=3.0,
            backoff_seconds=1.0,
            jitter_seconds=0.0,
        ),
        client=client,
        sleep=sleeps.append,
    )

    response = failover.chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )

    assert response["model"] == MODEL_A
    for delay in sleeps:
        assert delay <= 60.0


def test_429_persistent_eventually_exhausts_max_attempts():
    client = SequencedOpenRouterClient(
        [OpenRouterError("rate limit", status_code=429) for _ in range(10)]
    )

    failover = _failover(client, max_failed_cycles=2)

    with pytest.raises(OpenRouterError, match="max_total_attempts exceeded"):
        _ = failover.chat_json(
            model="ignored",
            messages=[{"role": "user", "content": "{}"}],
            max_total_attempts=6,
        )

    assert client.calls == [MODEL_A, MODEL_B, MODEL_A, MODEL_B, MODEL_A, MODEL_B]


def test_non_429_recoverable_flat_backoff():
    sleeps: list[float] = []
    client = SequencedOpenRouterClient(
        [
            OpenRouterError("provider unavailable", status_code=503),
            OpenRouterError("server error", status_code=500),
            {"model": MODEL_A},
        ]
    )

    failover = OpenRouterModelFailover(
        config=OpenRouterFailoverConfig(
            provider="openrouter",
            models=(MODEL_A, MODEL_B),
            max_failed_cycles=2,
            cooldown_seconds=3.0,
            backoff_seconds=0.25,
            jitter_seconds=0.1,
        ),
        client=client,
        sleep=sleeps.append,
    )

    response = failover.chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )

    assert response["model"] == MODEL_A
    assert len(sleeps) == 2
    for delay in sleeps:
        assert 0.25 <= delay <= 0.35


def test_429_counter_resets_after_success():
    sleeps: list[float] = []
    client = SequencedOpenRouterClient(
        [
            OpenRouterError("rate limit", status_code=429),
            OpenRouterError("rate limit", status_code=429),
            OpenRouterError("rate limit", status_code=429),
            {"model": MODEL_B},
            OpenRouterError("rate limit", status_code=429),
            {"model": MODEL_B},
        ]
    )

    failover = OpenRouterModelFailover(
        config=OpenRouterFailoverConfig(
            provider="openrouter",
            models=(MODEL_A, MODEL_B),
            max_failed_cycles=2,
            cooldown_seconds=3.0,
            backoff_seconds=0.25,
            jitter_seconds=0.0,
        ),
        client=client,
        sleep=sleeps.append,
    )

    first = failover.chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )
    second = failover.chat_json(
        model="ignored", messages=[{"role": "user", "content": "{}"}]
    )

    assert first["model"] == MODEL_B
    assert second["model"] == MODEL_B
    assert len(sleeps) == 4
    assert sleeps[0] == 0.25
    assert sleeps[1] == 0.5
    assert sleeps[2] == 1.0
    assert sleeps[3] == 0.25
