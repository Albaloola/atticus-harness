"""Reserved Anthropic adapter surface."""

from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Protocol, cast

from atticus.adapters.base import ExecutionAdapter
from atticus.providers.anthropic import (
    ENV_ANTHROPIC_API_KEY,
    ENV_ANTHROPIC_OAUTH_TOKEN,
    ENV_ENABLE_LIVE_ANTHROPIC,
    resolve_anthropic_model,
    safe_anthropic_error_message,
)


class AnthropicJsonClient(Protocol):
    def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, object]: ...


class DirectAnthropicAdapter(ExecutionAdapter):
    name: str = "direct_anthropic"

    def __init__(self, *, client: object | None = None, env: Mapping[str, str] | None = None) -> None:
        self.client: AnthropicJsonClient | None = cast(AnthropicJsonClient | None, client)
        self.env: Mapping[str, str] | None = env

    def run(self, work_order: dict[str, object], *, model: str, max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, object]:
        env = self.env if self.env is not None else os.environ
        if env.get(ENV_ENABLE_LIVE_ANTHROPIC) != "1":
            raise RuntimeError(f"{ENV_ENABLE_LIVE_ANTHROPIC}=1 is required before live Anthropic work")
        concrete_model = resolve_anthropic_model(model, env=env)
        if not concrete_model:
            raise RuntimeError("live Anthropic work requires a concrete configured model id")
        if not (env.get(ENV_ANTHROPIC_API_KEY) or env.get(ENV_ANTHROPIC_OAUTH_TOKEN)):
            raise RuntimeError("live Anthropic work requires an API key or OAuth token")
        if self.client is None:
            raise RuntimeError("Anthropic live client is not configured")
        messages = [
            {"role": "system", "content": "Return only valid JSON for a bounded Atticus candidate work order."},
            {"role": "user", "content": str(work_order)},
        ]
        try:
            return self.client.chat_json(model=concrete_model, messages=messages, max_tokens=max_tokens, temperature=temperature)
        except Exception as exc:
            raise RuntimeError(f"Anthropic provider call failed: {safe_anthropic_error_message(exc, env=env)}") from None
