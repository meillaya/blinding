"""App wiring: middleware ORDER (telemetry -> auth -> rate-limit -> route),
the open /healthz, and env-driven fail-closed defaults (todo 11).

The order proof is behavioural, not structural: under a 1 RPS limit, two
back-to-back KEYLESS requests both return 401 — if rate limiting ran before
auth, the second would drain the shared IP bucket and return 429. The requests
complete in milliseconds, far under the 1 s refill, so the outcome cannot flip
on a slow runner.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest
from fastapi.testclient import TestClient
from inferenceops_gateway.config import GatewaySettings
from inferenceops_gateway.main import create_app
from inferenceops_gateway.telemetry import Telemetry

PAYLOAD: Mapping[str, object] = {"features": [0.1] * 20}
_GATEWAY_ENV_VARS = (
    "MODEL_SERVER_URL",
    "API_KEYS",
    "RATE_LIMIT_RPS",
    "OTEL_ENDPOINT",
    "LLM_BACKEND",
)


def _post_json(
    client: TestClient,
    url: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Typed seam: POST JSON via TestClient, returning a fully-known httpx.Response."""
    return client.post(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        url, json=payload, headers=headers
    )


def _get(client: TestClient, url: str) -> httpx.Response:
    """Typed seam: GET via TestClient, returning a fully-known httpx.Response."""
    return client.get(url)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def test_auth_rejects_before_rate_limit_counts(
    valid_key: str, mock_transport: httpx.MockTransport, telemetry: Telemetry
) -> None:
    settings = GatewaySettings(api_keys=frozenset({valid_key}), rate_limit_rps=1.0)
    with TestClient(create_app(settings, transport=mock_transport, telemetry=telemetry)) as client:
        first = _post_json(client, "/v1/predict", PAYLOAD)  # keyless
        second = _post_json(client, "/v1/predict", PAYLOAD)  # keyless again
        with_key = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
        with_key_again = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
    assert first.status_code == 401
    assert second.status_code == 401  # NOT 429: the limiter never saw keyless traffic
    assert with_key.status_code == 200  # the key's bucket is still full
    assert with_key_again.status_code == 429  # ...and IS limited once authenticated


def test_healthz_is_open_and_exempt_from_every_middleware(
    mock_transport: httpx.MockTransport, telemetry: Telemetry
) -> None:
    # No API keys configured at all; a tight limit: healthz still answers.
    settings = GatewaySettings(api_keys=frozenset(), rate_limit_rps=1.0)
    with TestClient(create_app(settings, transport=mock_transport, telemetry=telemetry)) as client:
        statuses = [_get(client, "/healthz").status_code for _ in range(3)]
    assert statuses == [200, 200, 200]


def test_default_settings_fail_closed(
    monkeypatch: pytest.MonkeyPatch, mock_transport: httpx.MockTransport, telemetry: Telemetry
) -> None:
    """No settings arg -> env-driven defaults; empty API_KEYS rejects every key."""
    for name in _GATEWAY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    with TestClient(create_app(transport=mock_transport, telemetry=telemetry)) as client:
        assert _get(client, "/healthz").status_code == 200
        denied = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": "any-key"})
    assert denied.status_code == 401
