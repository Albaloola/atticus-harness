"""Direct OpenRouter adapter for explicit live provider-backed work."""

from __future__ import annotations

import json
from typing import Protocol, cast

from atticus.adapters.base import ExecutionAdapter
from atticus.providers.openrouter import OpenRouterClient
from atticus.workers.result_parser import RESULT_PACKET_SCHEMA_VERSION, result_packet_json_schema


class ChatJsonClient(Protocol):
    def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, object]: ...


class DirectOpenRouterAdapter(ExecutionAdapter):
    name: str = "direct_openrouter"

    def __init__(self, *, client: object | None = None, timeout_seconds: float | None = None) -> None:
        client_obj = client or OpenRouterClient(timeout=timeout_seconds or 120.0)
        if client is not None and timeout_seconds is not None and hasattr(client_obj, "timeout"):
            setattr(client_obj, "timeout", timeout_seconds)
        self.client: ChatJsonClient = cast(ChatJsonClient, client_obj)

    def run(self, work_order: dict[str, object], *, model: str, max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, object]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a bounded Atticus legal harness worker. Return only valid JSON. "
                    "Workers produce candidate result packets only. Do not claim to file, send, email, upload, "
                    "contact, or perform external legal actions. The JSON content object must exactly follow "
                    f"{RESULT_PACKET_SCHEMA_VERSION}. Every finding must have finding_id, finding_type, "
                    "citation_ids, confidence, and reasoning_status. If evidence is insufficient, use "
                    "uncertainties or propose a follow-up task; do not invent citations."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "work_order": work_order,
                        "required_result_packet_schema": result_packet_json_schema(),
                    },
                    sort_keys=True,
                ),
            },
        ]
        return self.client.chat_json(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature)
