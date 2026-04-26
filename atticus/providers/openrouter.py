"""Minimal OpenRouter client for provider-backed Atticus work."""

from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib import error as urllib_error
from urllib import request as urllib_request

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

Transport = Callable[[urllib_request.Request, float], bytes]


class OpenRouterError(RuntimeError):
    """Raised when an OpenRouter response is unusable."""

    def __init__(self, message: str, *, status_code: int | None = None, body: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


USAGE_TOKEN_FIELDS = (
    "prompt_tokens",
    "input_tokens",
    "completion_tokens",
    "output_tokens",
    "total_tokens",
)


def validate_usage_tokens(usage: dict[str, Any]) -> dict[str, int]:
    """Return normalized token counts after fail-closed scalar validation.

    OpenRouter/OpenAI-style usage metadata is spend telemetry. Missing token
    fields are treated as zero, but any present token field must be a whole
    non-negative integer. Numeric strings are accepted because some adapters
    serialize usage through JSON-ish fixture layers; booleans, floats, nulls,
    containers, negative values, and malformed strings are rejected.
    """

    normalized: dict[str, int] = {}
    for field in USAGE_TOKEN_FIELDS:
        if field not in usage:
            continue
        normalized[field] = _parse_token_count(field, usage[field])
    return {
        "prompt_tokens": normalized.get("prompt_tokens", normalized.get("input_tokens", 0)),
        "completion_tokens": normalized.get("completion_tokens", normalized.get("output_tokens", 0)),
        "total_tokens": normalized.get("total_tokens", 0),
    }


def _parse_token_count(field: str, value: Any) -> int:
    if isinstance(value, bool):
        raise OpenRouterError(f"OpenRouter usage field {field} must be a non-negative integer, not boolean")
    if isinstance(value, int):
        if value < 0:
            raise OpenRouterError(f"OpenRouter usage field {field} must be non-negative")
        return value
    if isinstance(value, str):
        if not value.isdecimal():
            raise OpenRouterError(f"OpenRouter usage field {field} must be a whole non-negative integer string")
        return int(value)
    raise OpenRouterError(f"OpenRouter usage field {field} must be a non-negative integer")


class OpenRouterClient:
    def __init__(self, *, api_key: str | None = None, base_url: str = OPENROUTER_BASE_URL, transport: Transport | None = None, timeout: float = 120.0):
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.transport = transport or self._default_transport
        self.timeout = timeout

    def chat_json(self, *, model: str, messages: list[dict[str, str]], max_tokens: int = 4096, temperature: float = 0.1) -> dict[str, Any]:
        if not self.api_key:
            raise OpenRouterError("OPENROUTER_API_KEY is required")
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
        req = urllib_request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://local.atticus-harness",
                "X-Title": "Atticus Harness",
            },
            method="POST",
        )
        try:
            raw_bytes = self.transport(req, self.timeout)
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:500]
            raise OpenRouterError(f"OpenRouter HTTP {exc.code}: {body}", status_code=exc.code, body=body) from exc
        except urllib_error.URLError as exc:
            raise OpenRouterError(f"OpenRouter request failed: {exc.reason}") from exc
        except (ConnectionResetError, TimeoutError) as exc:
            raise OpenRouterError(f"OpenRouter network error: {exc}") from exc
        try:
            raw = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenRouterError(f"OpenRouter returned invalid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise OpenRouterError("OpenRouter response must be a JSON object")
        try:
            message = raw["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("OpenRouter choice message must be a JSON object")
            content_text = message.get("content") or "{}"
            content = json.loads(content_text) if isinstance(content_text, str) else content_text
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OpenRouterError(f"OpenRouter response did not contain a JSON message: {exc}") from exc
        provider = raw.get("provider") or raw.get("provider_name")
        actual_model = raw.get("model")
        if not provider or not actual_model:
            raise OpenRouterError("OpenRouter response missing provider/model metadata required for fallback detection")
        usage = raw.get("usage")
        if not isinstance(usage, dict):
            raise OpenRouterError("OpenRouter usage metadata must be a JSON object")
        validate_usage_tokens(usage)
        return {
            "provider": str(provider),
            "model": str(actual_model),
            "content": content,
            "usage": dict(usage),
            "raw": raw,
        }

    @staticmethod
    def _default_transport(req: urllib_request.Request, timeout: float) -> bytes:
        with urllib_request.urlopen(req, timeout=timeout) as response:  # noqa: S310 - explicit HTTPS OpenRouter endpoint
            return response.read()
