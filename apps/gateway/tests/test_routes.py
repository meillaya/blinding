"""Route table: transparent proxying, upstream-failure surfacing, reserved LLM
routes, and the overall route shape (todo 11).

Every upstream interaction goes through ``httpx.MockTransport``; the proxy
tests assert the ACTUAL mocked body/status came back (and what was forwarded),
never a tautology. Upstream failures (502 passthrough, connection error,
timeout) must surface as clean gateway errors, never a crash.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from inferenceops_gateway.config import GatewaySettings
from inferenceops_gateway.main import create_app
from inferenceops_gateway.telemetry import Telemetry

PAYLOAD: Mapping[str, object] = {"features": [0.1] * 20}


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


def _get(
    client: TestClient, url: str, headers: Mapping[str, str] | None = None
) -> httpx.Response:
    """Typed seam: GET via TestClient, returning a fully-known httpx.Response."""
    return client.get(url, headers=headers)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def _app_with(
    transport: httpx.MockTransport, telemetry: Telemetry, valid_key: str
) -> TestClient:
    """A started gateway app over a caller-supplied mocked upstream."""
    settings = GatewaySettings(api_keys=frozenset({valid_key}), rate_limit_rps=100.0)
    return TestClient(create_app(settings, transport=transport, telemetry=telemetry))


def test_predict_proxies_the_upstream_body_verbatim(
    client: TestClient,
    valid_key: str,
    predict_body: dict[str, object],
    upstream_requests: list[httpx.Request],
) -> None:
    response = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
    assert response.status_code == 200
    proxied: dict[str, object] = response.json()
    assert proxied == predict_body  # the mocked upstream's exact body, unchanged
    assert len(upstream_requests) == 1
    forwarded = upstream_requests[0]
    assert forwarded.method == "POST"
    assert forwarded.url.path == "/v1/predict"
    forwarded_body: dict[str, object] = json.loads(forwarded.content)
    assert forwarded_body == dict(PAYLOAD)  # request body forwarded verbatim
    assert "x-api-key" not in forwarded.headers  # gateway credentials never leak


def test_batch_is_proxied(
    client: TestClient, valid_key: str, predict_body: dict[str, object]
) -> None:
    payload: Mapping[str, object] = {"requests": [PAYLOAD, PAYLOAD]}
    response = _post_json(client, "/v1/batch", payload, {"X-API-Key": valid_key})
    assert response.status_code == 200
    proxied: dict[str, object] = response.json()
    assert proxied == predict_body


def test_models_is_proxied_as_get(client: TestClient, valid_key: str) -> None:
    response = _get(client, "/v1/models", {"X-API-Key": valid_key})
    assert response.status_code == 200
    proxied: list[object] = response.json()
    assert proxied == [{"model_id": "fraud-detector", "version": "1.0.0"}]


def test_query_string_is_forwarded(
    client: TestClient, valid_key: str, upstream_requests: list[httpx.Request]
) -> None:
    response = _get(client, "/v1/models?limit=5", {"X-API-Key": valid_key})
    assert response.status_code == 200
    assert upstream_requests[0].url.query == b"limit=5"


def test_upstream_error_status_passes_through(telemetry: Telemetry, valid_key: str) -> None:
    """A 502 from the model server is mirrored, body intact (transparent proxy)."""
    error_body = {"detail": "model exploded"}
    transport = httpx.MockTransport(lambda request: httpx.Response(502, json=error_body))
    with _app_with(transport, telemetry, valid_key) as client:
        response = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
    assert response.status_code == 502
    proxied: dict[str, object] = response.json()
    assert proxied == error_body


def test_upstream_connection_error_surfaces_as_502(
    telemetry: Telemetry, valid_key: str
) -> None:
    def refusing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _app_with(httpx.MockTransport(refusing), telemetry, valid_key) as client:
        response = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
    assert response.status_code == 502
    body: dict[str, object] = response.json()
    assert body["detail"] == "model server unavailable"


def test_upstream_timeout_surfaces_as_504(telemetry: Telemetry, valid_key: str) -> None:
    def too_slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("upstream too slow", request=request)

    with _app_with(httpx.MockTransport(too_slow), telemetry, valid_key) as client:
        response = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
    assert response.status_code == 504
    body: dict[str, object] = response.json()
    assert body["detail"] == "model server timeout"


def test_llm_routes_are_reserved_with_501(client: TestClient, valid_key: str) -> None:
    headers = {"X-API-Key": valid_key}
    completions = _post_json(client, "/v1/completions", {"prompt": "hi"}, headers)
    chat = _post_json(client, "/v1/chat/completions", {"messages": []}, headers)
    assert completions.status_code == 501
    assert chat.status_code == 501
    completions_body: dict[str, object] = completions.json()
    assert "todo 43" in str(completions_body["detail"])


def test_route_table_has_exactly_the_expected_surface(
    mock_transport: httpx.MockTransport, telemetry: Telemetry, settings: GatewaySettings
) -> None:
    """The public surface, read from the generated OpenAPI (the stable view of
    the route table that FastAPI 0.139's lazy router includes flatten for us)."""
    app = create_app(settings, transport=mock_transport, telemetry=telemetry)
    assert isinstance(app, FastAPI)
    schema = app.openapi()
    paths: dict[str, dict[str, object]] = schema["paths"]
    assert set(paths) == {
        "/healthz",
        "/v1/predict",
        "/v1/batch",
        "/v1/models",
        "/v1/completions",  # reserved, todo 43
        "/v1/chat/completions",  # reserved, todo 43
    }
    methods_by_path = {path: set(item) for path, item in paths.items()}
    assert methods_by_path["/v1/predict"] == {"post"}
    assert methods_by_path["/v1/batch"] == {"post"}
    assert methods_by_path["/v1/models"] == {"get"}
    assert methods_by_path["/v1/completions"] == {"post"}
    assert methods_by_path["/v1/chat/completions"] == {"post"}
    assert methods_by_path["/healthz"] == {"get"}
