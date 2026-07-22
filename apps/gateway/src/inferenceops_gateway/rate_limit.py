"""Per-tenant rate limiting (stub; real token bucket lands later)."""

from __future__ import annotations


class RateLimiter:
    """Decide whether a request for a tenant is allowed."""

    def allow(self, tenant: str) -> bool:
        """Return whether a request for ``tenant`` is allowed."""
        raise NotImplementedError
