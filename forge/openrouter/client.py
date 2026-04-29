"""Small OpenRouter client using the OpenAI-compatible HTTP API."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from typing import Any, cast
from urllib import error as urllib_error
from urllib import parse, request

from forge.config import MODEL_FLASH, MODEL_FLASH_NITRO


OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_HEADERS = {
    "HTTP-Referer": "https://local.atticus-forge",
    "X-OpenRouter-Title": "Atticus Forge",
}
DEFAULT_PROVIDER: dict[str, object] = {"allow_fallbacks": True, "require_parameters": True, "data_collection": "deny"}
THROUGHPUT_PROVIDER: dict[str, object] = {"sort": "throughput", **DEFAULT_PROVIDER}
PRICE_PROVIDER: dict[str, object] = {"sort": "price", **DEFAULT_PROVIDER}


class OpenRouterClient:
    def __init__(self, *, api_key: str | None = None, base_url: str = OPENROUTER_BASE_URL, timeout: float = 120.0) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat_json(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        temperature: float = 0.1,
        max_tokens: int = 4096,
        provider: dict[str, object] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required")
        payload: dict[str, object] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "provider": provider or DEFAULT_PROVIDER,
        }
        raw = self._post_json("/chat/completions", payload)
        content = _extract_json_content(raw)
        usage = raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}
        return {
            "id": str(raw.get("id") or ""),
            "provider": str(raw.get("provider") or raw.get("provider_name") or ""),
            "model": str(raw.get("model") or model),
            "content": content,
            "usage": dict(cast(Mapping[str, object], usage)),
            "raw": raw,
        }

    def generation_metadata(self, generation_id: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY is required")
        query = parse.urlencode({"id": generation_id})
        req = request.Request(
            f"{self.base_url}/generation?{query}",
            headers={"Authorization": f"Bearer {self.api_key}", **DEFAULT_HEADERS},
            method="GET",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:  # noqa: S310 - fixed HTTPS endpoint
                data = json.loads(response.read().decode("utf-8"))
        except (urllib_error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenRouter generation metadata failed: {exc}") from exc
        if isinstance(data, Mapping) and isinstance(data.get("data"), Mapping):
            return dict(cast(Mapping[str, Any], data["data"]))
        return dict(cast(Mapping[str, Any], data)) if isinstance(data, Mapping) else {}

    def _post_json(self, path: str, payload: dict[str, object]) -> dict[str, Any]:
        req = request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json", **DEFAULT_HEADERS},
            method="POST",
        )
        try:
            with request.urlopen(req, timeout=self.timeout) as response:  # noqa: S310 - fixed HTTPS endpoint
                data = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"OpenRouter HTTP {exc.code}: {body}") from exc
        except (urllib_error.URLError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"OpenRouter request failed: {exc}") from exc
        if not isinstance(data, Mapping):
            raise RuntimeError("OpenRouter response must be a JSON object")
        return dict(cast(Mapping[str, Any], data))


def _extract_json_content(raw: Mapping[str, Any]) -> dict[str, Any]:
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("OpenRouter response missing choices")
    first = choices[0]
    if not isinstance(first, Mapping):
        raise RuntimeError("OpenRouter choice must be an object")
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise RuntimeError("OpenRouter choice missing message")
    content = message.get("content")
    if isinstance(content, Mapping):
        return dict(cast(Mapping[str, Any], content))
    if not isinstance(content, str):
        raise RuntimeError("OpenRouter message content must be JSON text")
    parsed = json.loads(content)
    if not isinstance(parsed, Mapping):
        raise RuntimeError("OpenRouter message content must decode to a JSON object")
    return dict(cast(Mapping[str, Any], parsed))


__all__ = ["MODEL_FLASH", "MODEL_FLASH_NITRO", "OpenRouterClient", "DEFAULT_PROVIDER", "THROUGHPUT_PROVIDER", "PRICE_PROVIDER"]
