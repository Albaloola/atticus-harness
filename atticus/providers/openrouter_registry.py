"""Dynamic OpenRouter model registry with caching and offline support.

Fetches the full model catalog from OpenRouter's public API and caches it
locally with a 1-hour TTL. All ~400+ models are available without env var gates.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import threading
import time
from pathlib import Path

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_CACHE_DIR = Path.home() / ".atticus"
_CACHE_PATH = _CACHE_DIR / "openrouter_models.json"
_CACHE_TTL_SECONDS = 3600
_REQUEST_TIMEOUT_SECONDS = 30

_cache_lock = threading.Lock()


@dataclass(frozen=True)
class OpenRouterModelEntry:
    id: str
    name: str
    context_length: int
    max_completion_tokens: int
    prompt_price_per_million: float
    completion_price_per_million: float
    is_free: bool
    supports_tools: bool
    supports_structured_outputs: bool
    supports_vision: bool
    tokenizer: str
    modality: str
    expiration_date: str

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "context_length": self.context_length,
            "max_completion_tokens": self.max_completion_tokens,
            "prompt_price_per_million": self.prompt_price_per_million,
            "completion_price_per_million": self.completion_price_per_million,
            "is_free": self.is_free,
            "supports_tools": self.supports_tools,
            "supports_structured_outputs": self.supports_structured_outputs,
            "supports_vision": self.supports_vision,
            "tokenizer": self.tokenizer,
            "modality": self.modality,
            "expiration_date": self.expiration_date,
        }


class OpenRouterRegistry:
    """Singleton registry that fetches, caches, and filters OpenRouter models."""

    def __init__(self) -> None:
        self._models: dict[str, OpenRouterModelEntry] = {}
        self._loaded = False
        self._cache_timestamp: float = 0.0

    def _raw_fetch(self) -> list[dict[str, object]]:
        try:
            import urllib.request
        except ImportError:
            return []

        url = _OPENROUTER_MODELS_URL
        req = urllib.request.Request(url, headers={"User-Agent": "Atticus/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT_SECONDS) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return []

        raw_models = data.get("data")
        if not isinstance(raw_models, list):
            return []
        return [cast_dict(entry) for entry in raw_models if isinstance(entry, Mapping)]

    def _load_cache(self) -> list[dict[str, object]] | None:
        try:
            if not _CACHE_PATH.exists():
                return None
            mtime = _CACHE_PATH.stat().st_mtime
            if time.time() - mtime > _CACHE_TTL_SECONDS:
                return None
            with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            models = raw.get("models")
            self._cache_timestamp = float(raw.get("cached_at", 0.0))
            if isinstance(models, list):
                return [cast_dict(entry) for entry in models if isinstance(entry, Mapping)]
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return None

    def _normalize_model(self, raw: dict[str, object]) -> OpenRouterModelEntry | None:
        model_id = str(raw.get("id") or "").strip()
        if not model_id:
            return None

        name = str(raw.get("name") or model_id)
        context_length = 0
        max_completion_tokens = 0

        top_provider = raw.get("top_provider")
        if isinstance(top_provider, Mapping):
            _cl = top_provider.get("context_length")
            if isinstance(_cl, (int, float)):
                context_length = max(0, int(float(str(_cl))))
            _mct = top_provider.get("max_completion_tokens")
            if isinstance(_mct, (int, float)):
                max_completion_tokens = max(0, int(float(str(_mct))))

        if context_length == 0:
            _cl = raw.get("context_length")
            if isinstance(_cl, (int, float)):
                context_length = max(0, int(float(str(_cl))))

        pricing = raw.get("pricing")
        prompt_price = 0.0
        completion_price = 0.0
        if isinstance(pricing, Mapping):
            try:
                prompt_price = float(str(pricing.get("prompt") or "0")) * 1_000_000
            except (ValueError, TypeError):
                prompt_price = 0.0
            try:
                completion_price = float(str(pricing.get("completion") or "0")) * 1_000_000
            except (ValueError, TypeError):
                completion_price = 0.0

        is_free = model_id.endswith(":free") or (prompt_price == 0.0 and completion_price == 0.0)

        supports_tools = False
        supports_structured_outputs = False
        supports_vision = False
        tokenizer = ""
        modality = "text"

        architecture = raw.get("architecture")
        if isinstance(architecture, Mapping):
            tokenizer = str(architecture.get("tokenizer") or "")

            in_mods = architecture.get("input_modalities")
            if isinstance(in_mods, list):
                in_mods_str = [str(m).lower() for m in in_mods]
                supports_vision = any(
                    m in in_mods_str for m in ("image", "images", "multimodal", "video")
                )

            out_mods = architecture.get("output_modalities")
            out_has_text = True
            if isinstance(out_mods, list):
                out_has_text = "text" in [str(m).lower() for m in out_mods]

            mod = architecture.get("modality")
            if isinstance(mod, str):
                modality = mod.lower()

            if supports_vision and out_has_text:
                modality = "multimodal" if modality == "text" else modality

        supported_params = raw.get("supported_parameters")
        if isinstance(supported_params, list):
            params_set = {str(p).lower() for p in supported_params}
            supports_tools = "tools" in params_set
            supports_structured_outputs = any(
                param in params_set for param in (
                    "structured_outputs", "response_format", "json_schema"
                )
            )

        expiration_date = str(raw.get("expiration_date") or "")

        return OpenRouterModelEntry(
            id=model_id,
            name=name,
            context_length=context_length,
            max_completion_tokens=max_completion_tokens,
            prompt_price_per_million=prompt_price,
            completion_price_per_million=completion_price,
            is_free=is_free,
            supports_tools=supports_tools,
            supports_structured_outputs=supports_structured_outputs,
            supports_vision=supports_vision,
            tokenizer=tokenizer,
            modality=modality,
            expiration_date=expiration_date,
        )

    def _load_stale_cache(self) -> list[dict[str, object]] | None:
        try:
            if not _CACHE_PATH.exists():
                return None
            with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            models = raw.get("models")
            if isinstance(models, list):
                return [cast_dict(entry) for entry in models if isinstance(entry, Mapping)]
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return None

    def _load_internal(self) -> None:
        with _cache_lock:
            entries: list[OpenRouterModelEntry] = []

            cached = self._load_cache()
            if cached:
                for raw in cached:
                    entry = self._normalize_model(raw)
                    if entry is not None:
                        entries.append(entry)

            if not entries:
                try:
                    fetched = self._raw_fetch()
                    if fetched:
                        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
                        cache_data: dict[str, object] = {
                            "cached_at": time.time(),
                            "models": [
                                {
                                    "id": e["id"],
                                    "name": e.get("name", ""),
                                    "context_length": e.get("context_length", 0),
                                    "top_provider": e.get("top_provider", {}),
                                    "pricing": e.get("pricing", {}),
                                    "architecture": e.get("architecture", {}),
                                    "supported_parameters": e.get("supported_parameters", []),
                                    "expiration_date": e.get("expiration_date", ""),
                                }
                                for e in fetched
                            ],
                        }
                        tmp_path = _CACHE_PATH.with_suffix(".tmp")
                        with open(tmp_path, "w", encoding="utf-8") as fh:
                            json.dump(cache_data, fh, sort_keys=True)
                        tmp_path.replace(_CACHE_PATH)
                        self._cache_timestamp = float(str(cache_data["cached_at"]))

                        for raw in fetched:
                            entry = self._normalize_model(raw)
                            if entry is not None:
                                entries.append(entry)
                    else:
                        stale = self._load_stale_cache()
                        if stale:
                            for raw in stale:
                                entry = self._normalize_model(raw)
                                if entry is not None:
                                    entries.append(entry)
                except Exception:
                    stale = self._load_stale_cache()
                    if stale:
                        for raw in stale:
                            entry = self._normalize_model(raw)
                            if entry is not None:
                                entries.append(entry)

            if not entries:
                entries = [
                    OpenRouterModelEntry(
                        id="deepseek/deepseek-v4-flash",
                        name="DeepSeek V4 Flash",
                        context_length=1_048_576,
                        max_completion_tokens=384_000,
                        prompt_price_per_million=0.14,
                        completion_price_per_million=0.28,
                        is_free=False,
                        supports_tools=True,
                        supports_structured_outputs=True,
                        supports_vision=False,
                        tokenizer="",
                        modality="text",
                        expiration_date="",
                    ),
                    OpenRouterModelEntry(
                        id="deepseek/deepseek-v4-pro",
                        name="DeepSeek V4 Pro",
                        context_length=1_048_576,
                        max_completion_tokens=384_000,
                        prompt_price_per_million=0.435,
                        completion_price_per_million=0.87,
                        is_free=False,
                        supports_tools=True,
                        supports_structured_outputs=True,
                        supports_vision=False,
                        tokenizer="",
                        modality="text",
                        expiration_date="",
                    ),
                    OpenRouterModelEntry(
                        id="qwen/qwen3-coder:free",
                        name="Qwen3 Coder (free)",
                        context_length=128_000,
                        max_completion_tokens=32_768,
                        prompt_price_per_million=0.0,
                        completion_price_per_million=0.0,
                        is_free=True,
                        supports_tools=True,
                        supports_structured_outputs=False,
                        supports_vision=False,
                        tokenizer="",
                        modality="text",
                        expiration_date="",
                    ),
                ]

            self._models = {entry.id: entry for entry in entries}
            self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self._load_internal()

    def refresh(self) -> int:
        with _cache_lock:
            self._models.clear()
            self._loaded = False
            self._cache_timestamp = 0.0
            try:
                _CACHE_PATH.unlink(missing_ok=True)
            except OSError:
                pass
        self._ensure_loaded()
        return len(self._models)

    def get_by_id(self, model_id: str) -> OpenRouterModelEntry | None:
        self._ensure_loaded()
        return self._models.get(model_id)

    def has_model(self, model_id: str) -> bool:
        self._ensure_loaded()
        return model_id in self._models

    def list_all(self) -> list[OpenRouterModelEntry]:
        self._ensure_loaded()
        return sorted(self._models.values(), key=lambda e: e.id)

    def free_only(self) -> list[OpenRouterModelEntry]:
        return [e for e in self.list_all() if e.is_free]

    def tools_only(self) -> list[OpenRouterModelEntry]:
        return [e for e in self.list_all() if e.supports_tools]

    def vision_only(self) -> list[OpenRouterModelEntry]:
        return [e for e in self.list_all() if e.supports_vision]

    def min_context(self, min_tokens: int) -> list[OpenRouterModelEntry]:
        return [e for e in self.list_all() if e.context_length >= min_tokens]

    def filter(
        self,
        *,
        free_only: bool = False,
        tools_only: bool = False,
        vision_only: bool = False,
        min_context: int = 0,
        modality: str = "",
        provider_prefix: str = "",
    ) -> list[OpenRouterModelEntry]:
        results = self.list_all()
        if free_only:
            results = [e for e in results if e.is_free]
        if tools_only:
            results = [e for e in results if e.supports_tools]
        if vision_only:
            results = [e for e in results if e.supports_vision]
        if min_context > 0:
            results = [e for e in results if e.context_length >= min_context]
        if modality:
            results = [e for e in results if e.modality == modality.lower()]
        if provider_prefix:
            results = [e for e in results if e.id.startswith(provider_prefix)]
        return results

    def cheapest_capable(
        self,
        *,
        min_context: int = 0,
        supports_tools: bool = False,
        free_only: bool = False,
    ) -> OpenRouterModelEntry | None:
        candidates = self.list_all()
        if free_only:
            candidates = [e for e in candidates if e.is_free]
        if min_context > 0:
            candidates = [e for e in candidates if e.context_length >= min_context]
        if supports_tools:
            candidates = [e for e in candidates if e.supports_tools]

        def _cost_key(entry: OpenRouterModelEntry) -> tuple[int, float]:
            return (0 if entry.is_free else 1, entry.prompt_price_per_million + entry.completion_price_per_million)

        candidates.sort(key=_cost_key)
        return candidates[0] if candidates else None

    def best_reasoning(
        self,
        *,
        min_context: int = 0,
        free_only: bool = False,
    ) -> OpenRouterModelEntry | None:
        candidates = self.list_all()
        if free_only:
            candidates = [e for e in candidates if e.is_free]
        if min_context > 0:
            candidates = [e for e in candidates if e.context_length >= min_context]

        def _quality_key(entry: OpenRouterModelEntry) -> tuple[int, int, int]:
            return (
                0 if entry.supports_tools else 1,
                0 if entry.is_free else 1,
                -entry.context_length,
            )

        candidates.sort(key=_quality_key)
        return candidates[0] if candidates else None

    def pricing_for_model(self, model_id: str) -> tuple[float, float] | None:
        entry = self.get_by_id(model_id)
        if entry is None:
            return None
        return (entry.prompt_price_per_million, entry.completion_price_per_million)

    @property
    def model_count(self) -> int:
        self._ensure_loaded()
        return len(self._models)

    @property
    def cache_age_seconds(self) -> float:
        if self._cache_timestamp > 0:
            return time.time() - self._cache_timestamp
        return -1.0


def cast_dict(obj: Mapping[object, object]) -> dict[str, object]:
    return {str(key): value for key, value in obj.items()}


_registry: OpenRouterRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> OpenRouterRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                _registry = OpenRouterRegistry()
    return _registry


def pricing_from_registry(model_id: str) -> tuple[float, float] | None:
    return get_registry().pricing_for_model(model_id)
