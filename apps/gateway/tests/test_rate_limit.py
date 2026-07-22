"""Token-bucket rate limiting: deterministic core tests + the 429 edge (todo 11).

Flake avoidance (documented per adversarial class): the CORE tests inject a
manually-advanced ``FakeClock`` — zero real sleeps, exact refill arithmetic.
The APP-level tests use a 2 RPS limit, where admitting one extra request would
require >= 0.5 s of real time between three back-to-back TestClient calls
(they complete in milliseconds), so the deny/refill outcomes cannot flip on a
slow runner; the one refill test sleeps 1.5x the refill interval, a lower
bound guaranteed by the monotonic clock. Each test builds its own app, so no
bucket state leaks between tests.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

import httpx
import pytest
from fastapi.testclient import TestClient
from inferenceops_gateway.auth import Principal
from inferenceops_gateway.config import GatewaySettings
from inferenceops_gateway.main import create_app
from inferenceops_gateway.rate_limit import TokenBucketRateLimiter, limit_key
from inferenceops_gateway.telemetry import Telemetry

PAYLOAD: Mapping[str, object] = {"features": [0.1] * 20}


@dataclass(slots=True)
class FakeClock:
    """A manually-advanced monotonic clock for deterministic bucket tests."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


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


def _limited_app(
    rate: float, keys: frozenset[str], mock_transport: httpx.MockTransport, telemetry: Telemetry
) -> TestClient:
    """A gateway app with the given per-key RPS limit (started via ``with``)."""
    settings = GatewaySettings(api_keys=keys, rate_limit_rps=rate)
    return TestClient(create_app(settings, transport=mock_transport, telemetry=telemetry))


# --- core: deterministic token-bucket arithmetic (injected clock) ---


def test_allows_burst_up_to_capacity_then_denies_with_retry_after() -> None:
    limiter = TokenBucketRateLimiter(rate=2.0, clock=FakeClock())
    first = limiter.consume("k")
    second = limiter.consume("k")
    third = limiter.consume("k")
    assert first.allowed
    assert second.allowed
    assert not third.allowed
    assert third.retry_after_s == pytest.approx(0.5)  # deficit 1 token / 2 per second
    assert third.remaining == pytest.approx(0.0)
    assert first.remaining == pytest.approx(1.0)


def test_refills_over_time() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(rate=2.0, clock=clock)
    assert limiter.consume("k").allowed
    assert limiter.consume("k").allowed
    assert not limiter.consume("k").allowed
    clock.advance(0.5)  # exactly one token at 2/s
    assert limiter.consume("k").allowed
    assert not limiter.consume("k").allowed  # the single refilled token is spent


def test_refill_is_capped_at_capacity() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(rate=2.0, clock=clock)
    assert limiter.consume("k").allowed  # 2 -> 1 tokens
    clock.advance(3600.0)  # an hour idle: refills to the cap (2), not 1 + 7200
    assert limiter.consume("k").allowed
    assert limiter.consume("k").allowed
    assert not limiter.consume("k").allowed


def test_buckets_are_isolated_per_key() -> None:
    limiter = TokenBucketRateLimiter(rate=1.0, clock=FakeClock())
    assert limiter.consume("a").allowed
    assert not limiter.consume("a").allowed
    assert limiter.consume("b").allowed  # b's bucket is untouched by a's drain


def test_capacity_floor_admits_one_request_at_sub_one_rps() -> None:
    clock = FakeClock()
    limiter = TokenBucketRateLimiter(rate=0.5, clock=clock)
    assert limiter.consume("k").allowed  # capacity floored to 1.0
    denied = limiter.consume("k")
    assert not denied.allowed
    assert denied.retry_after_s == pytest.approx(2.0)  # 1 token / 0.5 per second
    clock.advance(2.0)
    assert limiter.consume("k").allowed


def test_explicit_capacity_bounds_burst() -> None:
    limiter = TokenBucketRateLimiter(rate=10.0, capacity=3.0, clock=FakeClock())
    outcomes = [limiter.consume("k").allowed for _ in range(4)]
    assert outcomes == [True, True, True, False]


def test_non_positive_rate_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        TokenBucketRateLimiter(rate=0.0)
    with pytest.raises(ValueError, match="positive"):
        TokenBucketRateLimiter(rate=-1.0)


def test_limit_key_prefers_principal_then_ip_then_unknown() -> None:
    authenticated: dict[str, object] = {
        "type": "http",
        "state": {"principal": Principal(api_key_id="alpha", tenant="default")},
    }
    assert limit_key(authenticated) == "key:alpha"
    anonymous: dict[str, object] = {"type": "http", "client": ("10.0.0.7", 51234)}
    assert limit_key(anonymous) == "ip:10.0.0.7"
    no_client: dict[str, object] = {"type": "http"}
    assert limit_key(no_client) == "ip:unknown"


# --- edge: the middleware returns 429 + Retry-After through the real app ---


def test_burst_over_limit_returns_429_with_retry_after(
    valid_key: str, mock_transport: httpx.MockTransport, telemetry: Telemetry
) -> None:
    headers = {"X-API-Key": valid_key}
    with _limited_app(2.0, frozenset({valid_key}), mock_transport, telemetry) as client:
        responses = [_post_json(client, "/v1/predict", PAYLOAD, headers) for _ in range(3)]
    assert [r.status_code for r in responses] == [200, 200, 429]
    limited = responses[2]
    assert limited.headers["retry-after"] == "1"  # ceil(0.5 s), minimum 1
    body: dict[str, object] = limited.json()
    assert body["detail"] == "rate limit exceeded"


def test_bucket_refills_after_waiting(
    valid_key: str, mock_transport: httpx.MockTransport, telemetry: Telemetry
) -> None:
    headers = {"X-API-Key": valid_key}
    with _limited_app(2.0, frozenset({valid_key}), mock_transport, telemetry) as client:
        assert _post_json(client, "/v1/predict", PAYLOAD, headers).status_code == 200
        assert _post_json(client, "/v1/predict", PAYLOAD, headers).status_code == 200
        assert _post_json(client, "/v1/predict", PAYLOAD, headers).status_code == 429
        time.sleep(0.75)  # >= 0.5 s -> at least one token refilled at 2/s
        assert _post_json(client, "/v1/predict", PAYLOAD, headers).status_code == 200


def test_rate_limit_isolates_api_keys(
    valid_key: str,
    second_key: str,
    mock_transport: httpx.MockTransport,
    telemetry: Telemetry,
) -> None:
    keys = frozenset({valid_key, second_key})
    headers_a = {"X-API-Key": valid_key}
    headers_b = {"X-API-Key": second_key}
    with _limited_app(1.0, keys, mock_transport, telemetry) as client:
        first_a = _post_json(client, "/v1/predict", PAYLOAD, headers_a)
        second_a = _post_json(client, "/v1/predict", PAYLOAD, headers_a)
        first_b = _post_json(client, "/v1/predict", PAYLOAD, headers_b)
    assert first_a.status_code == 200
    assert second_a.status_code == 429
    assert first_b.status_code == 200  # the second key has its own, still-full bucket


def test_healthz_is_exempt_from_rate_limiting(
    valid_key: str, mock_transport: httpx.MockTransport, telemetry: Telemetry
) -> None:
    with _limited_app(1.0, frozenset({valid_key}), mock_transport, telemetry) as client:
        statuses = [_get(client, "/healthz").status_code for _ in range(5)]
    assert statuses == [200] * 5
