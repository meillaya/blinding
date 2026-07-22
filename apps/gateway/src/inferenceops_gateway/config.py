"""Gateway runtime configuration (stub; populated from the environment later)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """Immutable gateway runtime configuration."""

    host: str = "0.0.0.0"
    port: int = 8080
    model_server_url: str = "http://localhost:8081"


def load_config() -> GatewayConfig:
    """Build the gateway configuration from environment variables."""
    raise NotImplementedError
