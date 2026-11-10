"""Gateway tests: auth, rate limiting, proxying, readiness, metrics, settings.

Every upstream is an `httpx.MockTransport` handler and the app is driven through
`httpx.ASGITransport`, so no test opens a socket. The gateway's ASGI lifespan
is not started: `create_app` builds the upstream client without doing I/O, and
the mock transport has nothing to close.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

import httpx
import pytest
from pydantic import ValidationError

from blinding.gateway.app import create_app
from blinding.gateway.settings import GatewaySettings, settings_from_env

VALID_KEY: Final = "test-key-alpha"
OTHER_KEY: Final = "test-key-bravo"
UPSTREAM_URL: Final = "http://llm.test"
MODELS_BODY: Final[dict[str, object]] = {
    "object": "list",
    "data": [{"id": "tiny-model", "object": "model"}],
}
CHAT_BODY: Final[dict[str, object]] = {
    "id": "chatcmpl-test",
    "object": "chat.completion",
    "choices": [],
}


def gateway_settings(**overrides: Any) -> GatewaySettings:
    """Settings for one test, with two known keys and no environment read."""
    values: dict[str, Any] = {
        "api_keys": frozenset({VALID_KEY, OTHER_KEY}),
        "rate_limit_rps": 100.0,
        "llm_url": UPSTREAM_URL,
    }
    values.update(overrides)
    return GatewaySettings(**values)


def recording_transport(seen: list[httpx.Request]) -> httpx.MockTransport:
    """An upstream that records each request and answers models and chat."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/models":
            return httpx.Response(200, json=MODELS_BODY)
        return httpx.Response(200, json=CHAT_BODY)

    return httpx.MockTransport(handler)


def status_transport(status_code: int) -> httpx.MockTransport:
    """An upstream that answers everything with one status and one body."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(status_code, json={"detail": "canned upstream error"})

    return httpx.MockTransport(handler)


def connect_error_transport() -> httpx.MockTransport:
    """An upstream that is not listening."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.MockTransport(handler)


def timeout_transport() -> httpx.MockTransport:
    """An upstream that accepts the connection and never answers."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream timed out", request=request)

    return httpx.MockTransport(handler)


@asynccontextmanager
async def gateway(
    settings: GatewaySettings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[httpx.AsyncClient]:
    """An in-process gateway client over a mocked upstream."""
    cfg = settings if settings is not None else gateway_settings()
    upstream = transport if transport is not None else recording_transport([])
    app = create_app(cfg, transport=upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_healthz_returns_ok_without_a_key() -> None:
    """GET /healthz answers 200 and leaves the upstream alone."""
    seen: list[httpx.Request] = []
    async with gateway(transport=recording_transport(seen)) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert seen == []


@pytest.mark.asyncio
async def test_missing_api_key_returns_401() -> None:
    """A request with no X-API-Key is rejected before the proxy runs."""
    seen: list[httpx.Request] = []
    async with gateway(transport=recording_transport(seen)) as client:
        response = await client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["detail"] == "missing X-API-Key header"
    assert seen == []


@pytest.mark.asyncio
async def test_unknown_api_key_returns_401() -> None:
    """A key outside the configured set is rejected before the proxy runs."""
    seen: list[httpx.Request] = []
    async with gateway(transport=recording_transport(seen)) as client:
        response = await client.get("/v1/models", headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401
    assert response.json()["detail"] == "invalid API key"
    assert seen == []


@pytest.mark.asyncio
async def test_valid_key_proxies_and_strips_api_key() -> None:
    """A known key forwards method, query, and body, and drops X-API-Key."""
    seen: list[httpx.Request] = []
    payload: dict[str, object] = {
        "model": "tiny-model",
        "messages": [{"role": "user", "content": "hi"}],
    }
    async with gateway(transport=recording_transport(seen)) as client:
        response = await client.post(
            "/v1/chat/completions?trace=1",
            json=payload,
            headers={"X-API-Key": VALID_KEY, "Connection": "close"},
        )
    assert response.status_code == 200
    assert response.json() == CHAT_BODY

    assert len(seen) == 1
    forwarded = seen[0]
    assert forwarded.method == "POST"
    assert forwarded.url.path == "/v1/chat/completions"
    assert forwarded.url.query == b"trace=1"
    assert json.loads(forwarded.content) == payload
    assert "x-api-key" not in forwarded.headers
    # The gateway's own httpx client opens a fresh connection, so a Connection
    # header may be present; the caller's "close" value is what must be gone.
    assert forwarded.headers.get("connection") != "close"
    assert forwarded.headers["host"] == "llm.test"


@pytest.mark.asyncio
async def test_upstream_status_and_body_pass_through() -> None:
    """A 404 from the upstream is mirrored, not replaced by a gateway error."""
    async with gateway(transport=status_transport(404)) as client:
        response = await client.get("/v1/models", headers={"X-API-Key": VALID_KEY})
    assert response.status_code == 404
    assert response.json() == {"detail": "canned upstream error"}


@pytest.mark.asyncio
async def test_rate_limit_returns_429_with_retry_after() -> None:
    """A one token bucket admits one call, then sheds the next with Retry-After.

    A second key keeps its own bucket, and health and metrics stay exempt while
    the first key is over its limit.
    """
    settings = gateway_settings(rate_limit_rps=1.0)
    async with gateway(settings, transport=recording_transport([])) as client:
        first = await client.get("/v1/models", headers={"X-API-Key": VALID_KEY})
        second = await client.get("/v1/models", headers={"X-API-Key": VALID_KEY})
        other = await client.get("/v1/models", headers={"X-API-Key": OTHER_KEY})
        health = await client.get("/healthz")
        metrics = await client.get("/metrics")

    assert first.status_code == 200
    assert second.status_code == 429
    retry_after = int(second.headers["Retry-After"])
    assert retry_after >= 1
    assert other.status_code == 200
    assert health.status_code == 200
    assert metrics.status_code == 200


@pytest.mark.asyncio
async def test_auth_runs_before_the_rate_limiter() -> None:
    """A 401 does not consume a token: the rejected call never reaches the bucket."""
    settings = gateway_settings(rate_limit_rps=1.0)
    async with gateway(settings, transport=recording_transport([])) as client:
        rejected = await client.get("/v1/models")
        admitted = await client.get("/v1/models", headers={"X-API-Key": VALID_KEY})
        shed = await client.get("/v1/models", headers={"X-API-Key": VALID_KEY})
    assert rejected.status_code == 401
    assert admitted.status_code == 200
    assert shed.status_code == 429


@pytest.mark.asyncio
async def test_upstream_connect_error_returns_502() -> None:
    """A refused connection becomes a clean 502 JSON body."""
    async with gateway(transport=connect_error_transport()) as client:
        response = await client.get("/v1/models", headers={"X-API-Key": VALID_KEY})
    assert response.status_code == 502
    assert response.json()["detail"] == "upstream unavailable"


@pytest.mark.asyncio
async def test_upstream_timeout_returns_504() -> None:
    """A read timeout becomes a clean 504 JSON body."""
    async with gateway(transport=timeout_transport()) as client:
        response = await client.get("/v1/models", headers={"X-API-Key": VALID_KEY})
    assert response.status_code == 504
    assert response.json()["detail"] == "upstream timeout"


@pytest.mark.asyncio
async def test_readyz_returns_200_when_upstream_answers() -> None:
    """GET /readyz asks the upstream for its model list and reports ready."""
    seen: list[httpx.Request] = []
    async with gateway(transport=recording_transport(seen)) as client:
        response = await client.get("/readyz", headers={"X-API-Key": VALID_KEY})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert seen[0].url.path == "/v1/models"


@pytest.mark.asyncio
async def test_readyz_returns_503_when_upstream_is_down() -> None:
    """A refusing upstream makes the readiness check report unavailable."""
    async with gateway(transport=connect_error_transport()) as client:
        response = await client.get("/readyz", headers={"X-API-Key": VALID_KEY})
    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}


@pytest.mark.asyncio
async def test_readyz_returns_503_on_a_non_200_model_list() -> None:
    """A reachable upstream that answers 500 still counts as not ready."""
    async with gateway(transport=status_transport(500)) as client:
        response = await client.get("/readyz", headers={"X-API-Key": VALID_KEY})
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_readyz_needs_an_api_key() -> None:
    """Readiness stays behind auth; only health and metrics are open."""
    async with gateway() as client:
        response = await client.get("/readyz")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_metrics_contains_counters_for_served_requests() -> None:
    """The scrape exposes the request counter and duration histogram.

    The two served requests appear under their route labels, and the configured
    API key never reaches the exposition text.
    """
    async with gateway(transport=recording_transport([])) as client:
        await client.get("/healthz")
        await client.get("/v1/models", headers={"X-API-Key": VALID_KEY})
        response = await client.get("/metrics")

    text = response.text
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "gateway_requests_total" in text
    assert 'route="/healthz"' in text
    assert 'route="/v1/models"' in text
    assert 'status="200"' in text
    assert "gateway_request_duration_seconds" in text
    assert VALID_KEY not in text


@pytest.mark.asyncio
async def test_each_app_builds_its_own_metric_registry() -> None:
    """Two app constructions in one process must not collide on a metric name."""
    for _ in range(2):
        async with gateway() as client:
            response = await client.get("/metrics")
        assert response.status_code == 200
        assert "gateway_requests_total" in response.text


def test_settings_parse_csv_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """BLINDING_API_KEYS accepts a comma separated list with blank padding."""
    monkeypatch.setenv("BLINDING_API_KEYS", " alpha, bravo ,, charlie ")
    monkeypatch.setenv("BLINDING_LLM_URL", UPSTREAM_URL)
    settings = settings_from_env()
    assert settings.api_keys == frozenset({"alpha", "bravo", "charlie"})
    assert settings.llm_url == UPSTREAM_URL


def test_settings_parse_json_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """BLINDING_API_KEYS accepts a JSON array of strings."""
    monkeypatch.setenv("BLINDING_API_KEYS", '["alpha", "bravo"]')
    monkeypatch.setenv("BLINDING_LLM_URL", UPSTREAM_URL)
    settings = settings_from_env()
    assert settings.api_keys == frozenset({"alpha", "bravo"})


def test_settings_model_validate_accepts_cli_strings() -> None:
    """The CLI validates a dict of flags and environment strings, not typed values."""
    settings = GatewaySettings.model_validate(
        {
            "llm_url": UPSTREAM_URL,
            "api_keys": "alpha,bravo",
            "rate_limit_rps": "5.5",
            "host": "127.0.0.1",
            "port": "9090",
        }
    )
    assert settings.api_keys == frozenset({"alpha", "bravo"})
    assert settings.rate_limit_rps == 5.5
    assert settings.port == 9090


def test_settings_are_frozen() -> None:
    """The parsed settings cannot be mutated after construction."""
    settings = gateway_settings()
    with pytest.raises(ValidationError):
        settings.port = 9090


def test_create_app_refuses_an_empty_key_set() -> None:
    """Settings allow no keys, and the application refuses to start on them."""
    settings = gateway_settings(api_keys=frozenset())
    with pytest.raises(ValueError, match="API key"):
        create_app(settings)
