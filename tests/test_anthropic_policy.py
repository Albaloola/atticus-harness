from __future__ import annotations

import pytest

from atticus.adapters.direct_anthropic import DirectAnthropicAdapter
from atticus.providers.anthropic import (
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_OPUS_MODEL,
    ENV_ENABLE_LIVE_ANTHROPIC,
    safe_anthropic_error_message,
)
from atticus.providers.runtime_base import AnthropicRuntime


def test_anthropic_runtime_reserved_policy_is_not_allowed():
    decision = AnthropicRuntime().validate_policy({"provider": "anthropic", "model": "opus", "reserved": True, "enabled": False})

    assert not decision.allowed
    assert decision.result == "reserved"


def test_direct_anthropic_adapter_redacts_injected_env_secret_and_suppresses_cause():
    secret = "sk-test-anthropic-secret"

    class LeakyClient:
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, object]:
            del model, messages, max_tokens, temperature
            raise RuntimeError(f"provider echoed credential {secret}")

    adapter = DirectAnthropicAdapter(
        client=LeakyClient(),
        env={
            ENV_ENABLE_LIVE_ANTHROPIC: "1",
            ENV_ANTHROPIC_API_KEY: secret,
            ENV_ANTHROPIC_OPUS_MODEL: "claude-opus-test",
        },
    )

    with pytest.raises(RuntimeError) as excinfo:
        _ = adapter.run({"task_id": "t"}, model="opus")

    assert secret not in str(excinfo.value)
    assert "[redacted]" in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_direct_anthropic_adapter_blocks_without_live_opt_in_before_client_call():
    class ExplodingClient:
        def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, object]:
            del model, messages, max_tokens, temperature
            raise AssertionError("client must not be called without Anthropic live opt-in")

    adapter = DirectAnthropicAdapter(client=ExplodingClient(), env={})

    with pytest.raises(RuntimeError, match="ATTICUS_ENABLE_LIVE_ANTHROPIC"):
        _ = adapter.run({"task_id": "t"}, model="opus")


def test_safe_anthropic_error_message_redacts_supplied_env_values():
    secret = "oauth-secret"
    message = safe_anthropic_error_message(RuntimeError(f"bad {secret}"), env={ENV_ANTHROPIC_API_KEY: secret})

    assert secret not in message
    assert "[redacted]" in message
