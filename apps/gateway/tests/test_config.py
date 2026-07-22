"""GatewaySettings: env-driven configuration with typed parsing (todo 11).

Env vars are cleared per test (autouse fixture) so ambient shell configuration
can never leak into an assertion; overrides are then set explicitly.
"""

from __future__ import annotations

import pytest
from inferenceops_gateway.config import GatewaySettings, get_settings
from pydantic import ValidationError

_ENV_VARS = (
    "HOST",
    "PORT",
    "MODEL_SERVER_URL",
    "API_KEYS",
    "RATE_LIMIT_RPS",
    "OTEL_ENDPOINT",
    "LLM_BACKEND",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient gateway env vars."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_defaults_match_spec() -> None:
    cfg = GatewaySettings()
    assert cfg.model_server_url == "http://localhost:8080"
    assert cfg.rate_limit_rps == 100.0
    assert cfg.api_keys == frozenset()
    assert cfg.otel_endpoint is None
    assert cfg.llm_backend is None


def test_env_overrides_every_field(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_SERVER_URL", "http://model-server:8080")
    monkeypatch.setenv("API_KEYS", "alpha, bravo ,charlie")
    monkeypatch.setenv("RATE_LIMIT_RPS", "5.5")
    monkeypatch.setenv("OTEL_ENDPOINT", "http://otel-collector:4318")
    monkeypatch.setenv("LLM_BACKEND", "cpu-llm")
    cfg = get_settings()
    assert cfg.model_server_url == "http://model-server:8080"
    assert cfg.api_keys == frozenset({"alpha", "bravo", "charlie"})
    assert cfg.rate_limit_rps == 5.5
    assert cfg.otel_endpoint == "http://otel-collector:4318"
    assert cfg.llm_backend == "cpu-llm"


def test_api_keys_accepts_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", '["x", "y"]')
    assert GatewaySettings().api_keys == frozenset({"x", "y"})


def test_blank_csv_entries_are_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", "a,, ,b")
    assert GatewaySettings().api_keys == frozenset({"a", "b"})


def test_api_keys_parsed_into_immutable_frozenset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("API_KEYS", "a,b")
    assert isinstance(GatewaySettings().api_keys, frozenset)


def test_settings_are_frozen() -> None:
    cfg = GatewaySettings()
    with pytest.raises(ValidationError):
        cfg.rate_limit_rps = 1.0  # pyright: ignore[reportAttributeAccessOverride]
