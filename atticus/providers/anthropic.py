"""Reserved Anthropic provider metadata.

Anthropic support is intentionally present as policy surface only. Live use
requires explicit environment opt-in and concrete configured model IDs.
"""

from __future__ import annotations

from collections.abc import Mapping
import os

ANTHROPIC_PROVIDER = "anthropic"
ANTHROPIC_OAUTH_PROVIDER = "anthropic-oauth"
ANTHROPIC_RUNTIME = "anthropic"

ENV_ANTHROPIC_API_KEY = "ATTICUS_ANTHROPIC_API_KEY"
ENV_ANTHROPIC_OAUTH_TOKEN = "ATTICUS_ANTHROPIC_OAUTH_TOKEN"
ENV_ENABLE_LIVE_ANTHROPIC = "ATTICUS_ENABLE_LIVE_ANTHROPIC"
ENV_ANTHROPIC_OPUS_MODEL = "ATTICUS_ANTHROPIC_OPUS_MODEL"
ENV_ANTHROPIC_SONNET_MODEL = "ATTICUS_ANTHROPIC_SONNET_MODEL"

ANTHROPIC_OPUS_ALIASES = {"opus", "opus-4.7"}
ANTHROPIC_SONNET_ALIASES = {"sonnet", "sonnet-4.7"}
ANTHROPIC_RESERVED_ALIASES = ANTHROPIC_OPUS_ALIASES | ANTHROPIC_SONNET_ALIASES


def known_anthropic_model(model: str, *, env: Mapping[str, str] | None = None) -> bool:
    if model in ANTHROPIC_RESERVED_ALIASES:
        return True
    env = env if env is not None else os.environ
    configured = {env.get(ENV_ANTHROPIC_OPUS_MODEL, ""), env.get(ENV_ANTHROPIC_SONNET_MODEL, "")}
    return bool(model) and model in configured


def resolve_anthropic_model(model: str, *, env: Mapping[str, str] | None = None) -> str:
    env = env if env is not None else os.environ
    if model in ANTHROPIC_OPUS_ALIASES:
        return str(env.get(ENV_ANTHROPIC_OPUS_MODEL) or "")
    if model in ANTHROPIC_SONNET_ALIASES:
        return str(env.get(ENV_ANTHROPIC_SONNET_MODEL) or "")
    configured = {env.get(ENV_ANTHROPIC_OPUS_MODEL, ""), env.get(ENV_ANTHROPIC_SONNET_MODEL, "")}
    return model if model and model in configured else ""


def safe_anthropic_error_message(exc: BaseException, *, env: Mapping[str, str] | None = None) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    env_values: Mapping[str, str] = env if env is not None else {}
    secrets = (
        os.environ.get(ENV_ANTHROPIC_API_KEY, ""),
        os.environ.get(ENV_ANTHROPIC_OAUTH_TOKEN, ""),
        env_values.get(ENV_ANTHROPIC_API_KEY, ""),
        env_values.get(ENV_ANTHROPIC_OAUTH_TOKEN, ""),
    )
    for value in secrets:
        if value:
            text = text.replace(value, "[redacted]")
    return text[:400]
