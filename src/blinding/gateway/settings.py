"""Gateway settings, read from the environment with the BLINDING_ prefix.

API keys reach this model from three places: a `BLINDING_API_KEYS` environment
variable, a command line flag, or a caller that passes keyword arguments. Each
place can carry the keys as a JSON array or as a comma separated string, so
`parse_api_keys` owns the parsing and every path calls that function.

The model is frozen because the gateway reads the keys once at startup and
shares the parsed value with the auth and rate-limit middleware.
"""

from __future__ import annotations

import json
from typing import Any, Final

from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    EnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8080
DEFAULT_RATE_LIMIT_RPS: Final = 100.0


def parse_api_keys(raw: str) -> frozenset[str]:
    """Parse a JSON array or a comma separated list of API keys.

    Surrounding whitespace and blank entries are dropped. An empty string
    parses to an empty set: the settings model accepts that, and `create_app`
    refuses to start on it, because a gateway with no keys admits every caller.
    """
    text = raw.strip()
    if not text:
        return frozenset()

    decoded: Any = None
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError:
        decoded = None

    # A JSON array is the explicit form. Every other string falls back to CSV,
    # including a single bare key, which is a one field CSV row.
    if isinstance(decoded, list):
        if not all(isinstance(item, str) for item in decoded):
            raise ValueError("api_keys JSON array must hold strings")
        return frozenset(item.strip() for item in decoded if item.strip())

    return frozenset(part.strip() for part in text.split(",") if part.strip())


class _GatewayEnvSource(EnvSettingsSource):
    """Environment source that parses API keys before the field validator runs.

    pydantic-settings decodes a complex field with `json.loads` before any
    validator sees the value, so a CSV key set would fail as invalid JSON at the
    source. Parsing in `prepare_field_value` keeps the CSV form working; the
    field validator on the model still handles direct construction and
    `model_validate` calls.
    """

    def prepare_field_value(
        self,
        field_name: str,
        field: FieldInfo,
        value: Any,
        value_is_complex: bool,
    ) -> Any:
        if field_name == "api_keys" and isinstance(value, str):
            return parse_api_keys(value)
        return super().prepare_field_value(field_name, field, value, value_is_complex)


class GatewaySettings(BaseSettings):
    """Immutable gateway configuration."""

    model_config = SettingsConfigDict(
        env_prefix="BLINDING_",
        frozen=True,
        extra="ignore",
    )

    api_keys: frozenset[str] = frozenset()
    rate_limit_rps: float = DEFAULT_RATE_LIMIT_RPS
    llm_url: str
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT

    @field_validator("api_keys", mode="before")
    @classmethod
    def _parse_key_text(cls, value: Any) -> Any:
        """Accept the CSV and JSON text forms when a caller passes a string.

        The command line builds its settings from a dict whose disabled flags
        hold None, so None means "no keys given" here. The empty set is allowed
        and `create_app` refuses to start on it.
        """
        if value is None:
            return frozenset()
        if isinstance(value, str):
            return parse_api_keys(value)
        return value

    @field_validator("llm_url")
    @classmethod
    def _require_llm_url(cls, value: str) -> str:
        """An empty upstream URL would produce an invalid proxy target."""
        if not value.strip():
            raise ValueError("llm_url must not be empty")
        return value

    @field_validator("rate_limit_rps")
    @classmethod
    def _require_positive_rate(cls, value: float) -> float:
        """The token bucket divides by the rate, so zero fails at startup."""
        if value <= 0:
            raise ValueError("rate_limit_rps must be positive")
        return value

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Read the environment through the API-key aware source."""
        return (init_settings, _GatewayEnvSource(settings_cls))


def settings_from_env() -> GatewaySettings:
    """Build settings from the BLINDING_* environment.

    pydantic-settings fills the required `llm_url` from the environment, which a
    type checker cannot see. The ignore keeps `llm_url` required without giving
    the field a placeholder default.
    """
    return GatewaySettings()  # pyright: ignore[reportCallIssue]
