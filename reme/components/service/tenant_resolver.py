"""Service-boundary tenant identity resolution.

Tenant identity is asserted by the service boundary from request *headers* and never
accepted from the request body. Two strategies ship:

- ``trusted_header``: read the tenant id straight from a header (default
  ``X-Reme-Tenant``). Use when ReMe sits behind a trusted caller/gateway that has
  already authenticated the end user — the recommended default for a consuming
  application backend (e.g. agentscope-java setting the header per call).
- ``static_token_map``: map a bearer credential to a tenant id from a config table.
  A demo-grade resolver; production with many, churning tenants wants a dynamic
  (JWT/DB) resolver, which slots in behind the same ``resolve`` interface.
"""

from collections.abc import Mapping
from typing import Optional

from ...schema import MultiTenantAuthConfig


def _header(headers: Mapping, name: str) -> Optional[str]:
    """Case-insensitive header lookup returning a stripped value or None."""
    if headers is None:
        return None
    value = headers.get(name)
    if value is None and hasattr(headers, "items"):
        lname = name.lower()
        for key, val in headers.items():
            if str(key).lower() == lname:
                value = val
                break
    value = value.strip() if isinstance(value, str) else value
    return value or None


class TenantResolver:
    """Resolve a request's headers to a tenant id (or None when unauthenticated)."""

    def resolve(self, headers: Mapping) -> Optional[str]:  # pragma: no cover - interface
        raise NotImplementedError


class TrustedHeaderResolver(TenantResolver):
    """Trust a tenant-id header set by an upstream authenticated caller/gateway."""

    def __init__(self, header: str = "X-Reme-Tenant") -> None:
        self.header = header

    def resolve(self, headers: Mapping) -> Optional[str]:
        return _header(headers, self.header)


class StaticTokenMapResolver(TenantResolver):
    """Map a bearer token (or raw credential) from a header to a tenant id."""

    def __init__(self, token_header: str = "Authorization", tokens: Optional[dict] = None) -> None:
        self.token_header = token_header
        self.tokens = dict(tokens or {})

    def resolve(self, headers: Mapping) -> Optional[str]:
        raw = _header(headers, self.token_header)
        if raw is None:
            return None
        token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw
        return self.tokens.get(token)


def build_tenant_resolver(auth: MultiTenantAuthConfig) -> TenantResolver:
    """Construct the configured resolver."""
    backend = (auth.backend or "trusted_header").strip()
    if backend == "trusted_header":
        return TrustedHeaderResolver(auth.header)
    if backend == "static_token_map":
        return StaticTokenMapResolver(auth.token_header, auth.tokens)
    raise ValueError(f"Unknown tenant auth backend: {backend!r}")
