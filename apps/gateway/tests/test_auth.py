"""API-key auth: the pure ``authenticate`` core + the ASGI middleware edge (todo 11).

Middleware tests assert REAL status codes and bodies through the full app
(TestClient + mocked upstream), and prove a rejected request never reaches the
model server (the recorded upstream request list stays empty).
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx
import pytest
from fastapi.testclient import TestClient
from inferenceops_gateway.auth import InvalidApiKeyError, Principal, authenticate

PAYLOAD: Mapping[str, object] = {"features": [0.1] * 20}


def _post_json(
    client: TestClient,
    url: str,
    payload: Mapping[str, object],
    headers: Mapping[str, str] | None = None,
) -> httpx.Response:
    """Typed seam: POST JSON via TestClient, returning a fully-known httpx.Response.

    The starlette/httpx compat shim exposes partially-Unknown request methods
    under basedpyright strict, so this one call carries a scoped suppression
    (same pattern as the model-server tests); every assertion runs on known types.
    """
    return client.post(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        url, json=payload, headers=headers
    )


def _get(client: TestClient, url: str) -> httpx.Response:
    """Typed seam: GET via TestClient, returning a fully-known httpx.Response."""
    return client.get(url)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]


def test_authenticate_valid_key_returns_principal() -> None:
    principal = authenticate("alpha", frozenset({"alpha", "bravo"}))
    assert principal == Principal(api_key_id="alpha", tenant="default")


def test_authenticate_missing_key_raises() -> None:
    with pytest.raises(InvalidApiKeyError, match="missing"):
        authenticate(None, frozenset({"alpha"}))


def test_authenticate_unknown_key_raises() -> None:
    with pytest.raises(InvalidApiKeyError, match="invalid"):
        authenticate("mallory", frozenset({"alpha"}))


def test_authenticate_fails_closed_on_empty_key_set() -> None:
    with pytest.raises(InvalidApiKeyError):
        authenticate("alpha", frozenset())


def test_missing_api_key_returns_401_and_never_reaches_upstream(
    client: TestClient, upstream_requests: list[httpx.Request]
) -> None:
    response = _post_json(client, "/v1/predict", PAYLOAD)
    assert response.status_code == 401
    body: dict[str, object] = response.json()
    assert body["detail"] == "missing X-API-Key header"
    assert upstream_requests == []


def test_invalid_api_key_returns_401_and_never_reaches_upstream(
    client: TestClient, upstream_requests: list[httpx.Request]
) -> None:
    response = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": "wrong-key"})
    assert response.status_code == 401
    body: dict[str, object] = response.json()
    assert body["detail"] == "invalid API key"
    assert upstream_requests == []


def test_valid_api_key_is_admitted(client: TestClient, valid_key: str) -> None:
    response = _post_json(client, "/v1/predict", PAYLOAD, {"X-API-Key": valid_key})
    assert response.status_code == 200


def test_healthz_needs_no_key(client: TestClient) -> None:
    response = _get(client, "/healthz")
    assert response.status_code == 200
    body: dict[str, object] = response.json()
    assert body["status"] == "ok"
