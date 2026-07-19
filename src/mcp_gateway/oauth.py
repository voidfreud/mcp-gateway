"""OAuth resource-server integration for independent gateway endpoints.

The gateway never issues tokens.  It validates JWT access tokens from an
external authorization server and publishes the RFC 9728 protected-resource
metadata that MCP clients need to start their OAuth flow.  A distinct provider
is created for every backend and for Virtual Tools so audiences remain scoped
to the endpoint the client registered.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from fastmcp.server.auth import JWTVerifier, RemoteAuthProvider
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.types import Receive, Scope, Send

from mcp_gateway.config_loader import OAuthConfig


def _origin(url: Any) -> str:
    """Return an origin-like URL without the normalized trailing slash."""
    return str(url).rstrip("/")


class _ScopeGuardMiddleware:
    """Return MCP's required 403 challenge for an under-scoped token.

    FastMCP wires ``RequireAuthMiddleware`` directly around the MCP route. Its
    JWT verifier also supports scope validation, but that path turns a missing
    scope into a 401. We leave verification to the JWT provider and perform
    authorization here so MCP clients receive the required 403 plus ``scope``.
    """

    def __init__(
        self,
        app,
        *,
        required_scopes: list[str],
        resource_metadata_url: str,
    ):
        self.app = app
        self._required_scopes = required_scopes
        self._resource_metadata_url = resource_metadata_url

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope.get("type") != "http" or not path.rstrip("/").endswith("/mcp"):
            await self.app(scope, receive, send)
            return
        credentials = scope.get("auth")
        if credentials is not None and isinstance(scope.get("user"), AuthenticatedUser):
            missing = [
                item
                for item in self._required_scopes
                if item not in getattr(credentials, "scopes", ())
            ]
            if missing:
                required = " ".join(self._required_scopes)
                description = f"Required scope: {', '.join(missing)}"
                challenge = (
                    'Bearer error="insufficient_scope", '
                    f'error_description="{description}", scope="{required}", '
                    f'resource_metadata="{self._resource_metadata_url}"'
                )
                body = json.dumps(
                    {
                        "error": "insufficient_scope",
                        "error_description": description,
                        "scope": required,
                    },
                    separators=(",", ":"),
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"www-authenticate", challenge.encode()),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


class GatewayRemoteAuthProvider(RemoteAuthProvider):
    """FastMCP remote provider with MCP-compliant scope authorization."""

    def __init__(self, *, endpoint: str, required_scopes: list[str], **kwargs):
        super().__init__(**kwargs)
        self._gateway_required_scopes = list(required_scopes)
        self._endpoint = endpoint.strip("/")
        # Disable FastMCP's built-in scope check; the guard above preserves the
        # distinction between authentication (401) and authorization (403).
        self.required_scopes = []

    def get_middleware(self) -> list:
        return [
            *super().get_middleware(),
            Middleware(
                _ScopeGuardMiddleware,
                required_scopes=self._gateway_required_scopes,
                resource_metadata_url=(
                    f"{_origin(self.base_url)}/.well-known/"
                    f"oauth-protected-resource/{self._endpoint}/mcp"
                ),
            ),
        ]


@dataclass(frozen=True)
class OAuthRuntime:
    """Factory and route owner for the configured OAuth profile."""

    config: OAuthConfig

    @property
    def public_origin(self) -> str:
        return _origin(self.config.public_base_url)

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """Browser origins that may reach a remotely advertised gateway."""
        return (self.public_origin,)

    def resource_url(self, endpoint: str) -> str:
        """Canonical protected-resource identifier for one endpoint."""
        segment = endpoint.strip("/")
        return f"{self.public_origin}/{segment}/mcp"

    def provider(self, endpoint: str) -> GatewayRemoteAuthProvider:
        """Build a JWT resource-server provider for *endpoint*.

        Required scopes are enforced by FastMCP's endpoint middleware.  They
        intentionally do not go on ``JWTVerifier``: FastMCP treats a missing
        scope during JWT validation as an invalid token (401), whereas MCP
        requires a valid but under-scoped token to receive 403.
        """
        verifier = JWTVerifier(
            jwks_uri=str(self.config.jwks_uri),
            issuer=str(self.config.issuer),
            audience=self.resource_url(endpoint),
            algorithm=self.config.algorithm,
        )
        provider = GatewayRemoteAuthProvider(
            endpoint=endpoint,
            required_scopes=list(self.config.required_scopes),
            token_verifier=verifier,
            authorization_servers=list(self.config.authorization_servers),
            base_url=self.public_origin,
            resource_base_url=f"{self.public_origin}/{endpoint.strip('/')}",
            scopes_supported=list(self.config.required_scopes),
        )
        return provider

    def attach_metadata(
        self, app: Starlette, endpoint: str, provider: RemoteAuthProvider
    ) -> None:
        """Attach root-level RFC 9728 routes and tag them for unmount cleanup."""
        for route in provider.get_well_known_routes("/mcp"):
            route._mcp_gateway_oauth_endpoint = endpoint
            app.router.routes.append(route)

    @staticmethod
    def detach_metadata(app: Starlette, endpoint: str) -> None:
        """Remove metadata routes belonging to one independently mounted endpoint."""
        app.router.routes[:] = [
            route
            for route in app.router.routes
            if getattr(route, "_mcp_gateway_oauth_endpoint", None) != endpoint
        ]
