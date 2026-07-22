"""Prometheus metrics registry for the model server (stub)."""

from __future__ import annotations


class MetricsRegistry:
    """Owns the Prometheus metrics exposed by the model server."""

    def observe_request(self, method: str, status: str, model_version: str) -> None:
        """Record a single inference request."""
        raise NotImplementedError
