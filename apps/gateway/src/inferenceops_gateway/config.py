"""Gateway runtime configuration, parsed from the environment (todo 11).

Pydantic-settings ``BaseSettings``: every field is overridable by the
same-named environment variable (``MODEL_SERVER_URL``, ``API_KEYS``,
``RATE_LIMIT_RPS``, ``OTEL_ENDPOINT``, ``LLM_BACKEND``). ``API_KEYS`` accepts a
JSON array or a comma-separated string and is parsed into an immutable
``frozenset`` at the boundary, so downstream code (``auth.py``) receives a
typed value and never re-parses. The CSV form needs a custom env source:
pydantic-settings JSON-decodes complex fields at the SOURCE level (before any
validator runs), so a plain string like ``alpha,bravo`` would die as invalid
JSON. Instances are frozen: parsed once at startup, shared immutably.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Final

from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_MODEL_SERVER_URL: Final = "http://localhost:8080"
DEFAULT_RATE_LIMIT_RPS: Final = 100.0

# Default base URL per LLM backend (todo 43). Each backend serves the
# OpenAI-compatible surface (/v1/completions, /v1/chat/completions, /v1/models)
# on its own port; ``llm_base_url`` picks the entry for ``llm_backend`` unless
# ``llm_url`` overrides it (e.g. the host address seen from inside kind, where
# the CPU LLM runs on the host and the gateway runs in-cluster).
LLM_BACKEND_DEFAULT_URLS: Final[Mapping[str, str]] = {
    "llama-cpu": "http://localhost:8001",  # llama.cpp llama-server
    "ollama": "http://localhost:11434",  # ollama serve (OpenAI-compatible)
    "vllm": "http://localhost:8000",  # vLLM OpenAI server (GPU; todo 42)
}


def _parse_api_keys_env(raw: str) -> object:
    """The env value for API_KEYS: a JSON array if it parses, else CSV
    (``a, b ,c`` -> ``["a", "b", "c"]``, blank entries dropped)."""
    try:
        decoded: object = json.loads(raw)
    except json.JSONDecodeError:
        return [part.strip() for part in raw.split(",") if part.strip()]
    return decoded


class _GatewayEnvSource(EnvSettingsSource):
    """Env source that accepts ``API_KEYS`` as CSV in addition to a JSON array."""

    def prepare_field_value(
        self, field_name: str, field: FieldInfo, value: object, value_is_complex: bool
    ) -> object:
        if field_name == "api_keys" and isinstance(value, str):
            return _parse_api_keys_env(value)
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class GatewaySettings(BaseSettings):
    """Immutable, env-overridable gateway configuration."""

    model_config = SettingsConfigDict(frozen=True, extra="ignore")

    host: str = "0.0.0.0"
    port: int = 8080
    model_server_url: str = DEFAULT_MODEL_SERVER_URL
    api_keys: frozenset[str] = frozenset()
    rate_limit_rps: float = DEFAULT_RATE_LIMIT_RPS
    otel_endpoint: str | None = None
    llm_backend: str | None = None  # llama-cpu | ollama | vllm (todo 43); empty = no LLM
    llm_url: str | None = None  # explicit LLM base URL; overrides the backend default

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            _GatewayEnvSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )


def get_settings() -> GatewaySettings:
    """Build the gateway settings from the current environment."""
    return GatewaySettings()


def llm_base_url(settings: GatewaySettings) -> str | None:
    """The LLM backend base URL, or None when no LLM is configured.

    ``llm_url`` wins (the explicit override); otherwise the ``llm_backend``
    selects a default from ``LLM_BACKEND_DEFAULT_URLS``. Empty strings (an
    unset configmap key) count as absent, so a gateway with ``LLM_BACKEND=""``
    has no LLM and its OpenAI routes answer 503.
    """
    if settings.llm_url:
        return settings.llm_url
    if settings.llm_backend:
        return LLM_BACKEND_DEFAULT_URLS.get(settings.llm_backend)
    return None
