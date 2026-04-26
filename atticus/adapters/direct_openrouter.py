"""Direct OpenRouter adapter for explicit live provider-backed work."""

from __future__ import annotations

import json
from typing import Any

from atticus.adapters.base import ExecutionAdapter
from atticus.providers.openrouter import OpenRouterClient


class DirectOpenRouterAdapter(ExecutionAdapter):
    name = "direct_openrouter"

    def __init__(self, *, client: Any | None = None) -> None:
        self.client = client or OpenRouterClient()

    def run(self, work_order: dict[str, Any], *, model: str, max_tokens: int = 4096) -> dict[str, Any]:
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a bounded Atticus legal harness worker. Return only valid JSON. "
                    "Workers produce candidate result packets only. Do not claim to file, send, email, upload, "
                    "contact, or perform external legal actions."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "work_order": work_order,
                        "required_result_packet_keys": ["task_id", "summary", "findings", "citations", "proposed_artifacts", "proposed_tasks"],
                    },
                    sort_keys=True,
                ),
            },
        ]
        return self.client.chat_json(model=model, messages=messages, max_tokens=max_tokens, temperature=0.1)
