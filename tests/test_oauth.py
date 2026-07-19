"""OAuth resource-server contracts for independent MCP endpoints."""

from __future__ import annotations

import time
from pathlib import Path

import jwt
import pytest
import structlog
from fastmcp.server.auth import JWTVerifier as RealJWTVerifier
from starlette.testclient import TestClient

from mcp_gateway import oauth, server
from mcp_gateway.config_loader import ConfigError, GatewayConfig, dump_toml, save


def _oauth_raw(**overrides):
    value = {
        "public_base_url": "http://127.0.0.1:9100",
        "authorization_servers": ["http://127.0.0.1:9999"],
        "issuer": "http://127.0.0.1:9999",
        "jwks_uri": "http://127.0.0.1:9999/jwks",
    }
    value.update(overrides)
    return value


def _cfg(**overrides) -> GatewayConfig:
    raw = {"oauth": _oauth_raw(**overrides), "backends": []}
    return GatewayConfig.model_validate(raw)


def test_oauth_config_round_trips_and_preserves_issuer_text():
    cfg = _cfg(issuer="https://login.example.com/tenant")
    text = dump_toml(cfg)
    assert 'issuer = "https://login.example.com/tenant"' in text
    reparsed = GatewayConfig.model_validate(__import__("tomllib").loads(text))
    assert reparsed.oauth == cfg.oauth


def test_oauth_rejects_static_bearer_and_public_http():
    with pytest.raises(ConfigError, match="mutually exclusive"):
        GatewayConfig.model_validate(
            {"bearer_token": "${TOKEN}", "oauth": _oauth_raw(), "backends": []}
        )
    with pytest.raises(ConfigError, match="must use https"):
        _cfg(
            public_base_url="http://gateway.example.com",
            authorization_servers=["https://login.example.com"],
            issuer="https://login.example.com",
            jwks_uri="https://login.example.com/jwks",
        )


def test_non_loopback_oauth_requires_separate_admin_token():
    with pytest.raises(ConfigError, match="Admin API"):
        GatewayConfig.model_validate(
            {
                "host": "0.0.0.0",
                "oauth": _oauth_raw(
                    public_base_url="https://gateway.example.com",
                    authorization_servers=["https://login.example.com"],
                    issuer="https://login.example.com",
                    jwks_uri="https://login.example.com/jwks",
                ),
                "backends": [],
            }
        )


def test_oauth_admin_token_must_be_an_env_reference():
    with pytest.raises(ConfigError, match=r"single \$\{ENV_VAR\}"):
        _cfg(admin_bearer_token="raw-secret")


def test_provider_binds_endpoint_audience_and_root_metadata():
    provider = oauth.OAuthRuntime(_cfg().oauth).provider("backend-a")
    assert provider.token_verifier.audience == ("http://127.0.0.1:9100/backend-a/mcp")
    assert provider.required_scopes == []  # custom guard owns 401 vs 403
    assert provider._gateway_required_scopes == ["mcp:access"]
    assert [route.path for route in provider.get_well_known_routes("/mcp")] == [
        "/.well-known/oauth-protected-resource/backend-a/mcp"
    ]


def test_oauth_virtual_endpoint_enforces_401_and_403(monkeypatch, tmp_path: Path):
    # Use HS256 only in this hermetic test. Production config accepts only
    # asymmetric algorithms and always supplies a remote JWKS URI.
    monkeypatch.setattr(
        oauth,
        "JWTVerifier",
        lambda **kwargs: RealJWTVerifier(
            public_key="test-secret-32-bytes-long-key!!!!",
            algorithm="HS256",
            issuer=kwargs["issuer"],
            audience=kwargs["audience"],
        ),
    )
    cfg = _cfg()
    config_path = tmp_path / "config.toml"
    save(cfg, config_path)
    app = server._build_app(
        cfg,
        structlog.get_logger("oauth-test"),
        {},
        {},
        {},
        config_path=str(config_path),
    )
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "oauth-test", "version": "1"},
        },
    }
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(app) as client:
        missing = client.post("/virtual/mcp", headers=headers, json=request)
        assert missing.status_code == 401
        assert "resource_metadata=" in missing.headers["www-authenticate"]

        claims = {
            "iss": "http://127.0.0.1:9999",
            "aud": "http://127.0.0.1:9100/virtual/mcp",
            "sub": "oauth-test",
            "exp": int(time.time()) + 600,
            "scope": "other",
        }
        token = jwt.encode(
            claims,
            "test-secret-32-bytes-long-key!!!!",
            algorithm="HS256",
        )
        under_scoped = client.post(
            "/virtual/mcp",
            headers={**headers, "Authorization": f"Bearer {token}"},
            json=request,
        )
        assert under_scoped.status_code == 403
        challenge = under_scoped.headers["www-authenticate"]
        assert 'error="insufficient_scope"' in challenge
        assert 'scope="mcp:access"' in challenge
        assert "resource_metadata=" in challenge

        claims["scope"] = "mcp:access"
        token = jwt.encode(
            claims,
            "test-secret-32-bytes-long-key!!!!",
            algorithm="HS256",
        )
        authorized = client.post(
            "/virtual/mcp",
            headers={**headers, "Authorization": f"Bearer {token}"},
            json=request,
        )
        assert authorized.status_code == 200
        assert '"protocolVersion":"2025-11-25"' in authorized.text


def test_remote_oauth_keeps_admin_api_on_separate_token(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("ADMIN_GATEWAY_TOKEN", "admin-secret")
    cfg = GatewayConfig.model_validate(
        {
            "host": "0.0.0.0",
            "oauth": _oauth_raw(
                public_base_url="https://gateway.example.com",
                authorization_servers=["https://login.example.com"],
                issuer="https://login.example.com",
                jwks_uri="https://login.example.com/jwks",
                admin_bearer_token="${ADMIN_GATEWAY_TOKEN}",
            ),
            "backends": [],
        }
    )
    config_path = tmp_path / "config.toml"
    save(cfg, config_path)
    app = server._build_app(
        cfg,
        structlog.get_logger("oauth-admin-test"),
        {},
        {},
        {},
        config_path=str(config_path),
    )
    with TestClient(app) as client:
        assert client.get("/admin/api/state").status_code == 401
        assert (
            client.get(
                "/admin/api/state",
                headers={"Authorization": "Bearer admin-secret"},
            ).status_code
            == 200
        )
        settings = client.get(
            "/admin/api/settings",
            headers={"Authorization": "Bearer admin-secret"},
        )
        assert settings.json()["auth_mode"] == "oauth_jwt"
        cannot_mix = client.put(
            "/admin/api/settings",
            headers={"Authorization": "Bearer admin-secret"},
            json={"bearer_token": "${OTHER_TOKEN}"},
        )
        assert cannot_mix.status_code == 400
        prm = client.get("/.well-known/oauth-protected-resource/virtual/mcp")
        assert prm.status_code == 200
        assert prm.json()["resource"] == "https://gateway.example.com/virtual/mcp"
