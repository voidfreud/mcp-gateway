"""Tests for the robustness fixes: JSON-body 400 (#48), admin body-size cap
(#49), rotating gateway.log (#50), plus the admin-UX cluster: gateway version
surfacing (#57) and honest dev/foreground restart reporting (#53/#56)."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import anyio
import pytest
import structlog
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp_gateway import admin, runtime, server
from mcp_gateway import config_loader as cl

# ---------------------------------------------------------------------------
# #48 — malformed/missing JSON body → 400 (not 500 + traceback)
# ---------------------------------------------------------------------------


def _echo_app():
    async def echo(request):
        payload = await request.json()
        return JSONResponse({"ok": True, "got": payload})

    return Starlette(
        routes=[Route("/admin/api/echo", admin._needs_json(echo), methods=["POST"])]
    )


def test_needs_json_valid_body_passes_through():
    client = TestClient(_echo_app())
    r = client.post("/admin/api/echo", json={"a": 1})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "got": {"a": 1}}


def test_needs_json_malformed_body_is_400():
    client = TestClient(_echo_app())
    r = client.post("/admin/api/echo", content=b"not json")
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_needs_json_empty_body_is_400():
    client = TestClient(_echo_app())
    r = client.post("/admin/api/echo", content=b"")
    assert r.status_code == 400


@pytest.mark.parametrize("body", [b"[1, 2]", b'"str"', b"42", b"true", b"null"])
def test_needs_json_non_object_body_is_400(body):
    # Syntactically valid JSON that isn't an object must not reach the handler —
    # payload["backend"] / payload.get() on a list/str/int would 500.
    client = TestClient(_echo_app())
    r = client.post("/admin/api/echo", content=body)
    assert r.status_code == 400
    assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# #49 — admin request-body size cap → 413
# ---------------------------------------------------------------------------


def _limited_app(max_bytes: int):
    async def sink(request):
        body = await request.body()
        return JSONResponse({"ok": True, "len": len(body)})

    return Starlette(
        routes=[
            Route("/admin/api/sink", sink, methods=["POST"]),
            Route("/other", sink, methods=["POST"]),
        ],
        middleware=[Middleware(server.BodyLimitMiddleware, max_bytes=max_bytes)],
    )


def test_body_limit_rejects_oversized_admin_body():
    client = TestClient(_limited_app(1024))
    r = client.post("/admin/api/sink", content=b"x" * 2048)
    assert r.status_code == 413


def test_body_limit_allows_small_admin_body():
    client = TestClient(_limited_app(1024))
    r = client.post("/admin/api/sink", content=b"x" * 512)
    assert r.status_code == 200
    assert r.json()["len"] == 512


def test_admin_body_limit_allows_maximum_utf8_metadata_json_value():
    metadata = "\0" * cl.MAX_METADATA_LIMIT_BYTES
    body = json.dumps(
        {"tool_description": metadata},
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")

    assert len(metadata.encode("utf-8")) == cl.MAX_METADATA_LIMIT_BYTES
    assert len(body) <= server.ADMIN_BODY_LIMIT

    client = TestClient(_limited_app(server.ADMIN_BODY_LIMIT))
    r = client.post(
        "/admin/api/sink",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["len"] == len(body)


def test_admin_body_limit_rejects_envelope_overflow():
    body = json.dumps({"padding": "x" * server.ADMIN_BODY_LIMIT}).encode("utf-8")
    assert len(body) > server.ADMIN_BODY_LIMIT

    client = TestClient(_limited_app(server.ADMIN_BODY_LIMIT))
    r = client.post(
        "/admin/api/sink",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 413


def test_body_limit_ignores_non_admin_paths():
    client = TestClient(_limited_app(1024))
    r = client.post("/other", content=b"x" * 4096)
    assert r.status_code == 200
    assert r.json()["len"] == 4096


# ---------------------------------------------------------------------------
# #26 — optional bearer token on backend endpoints (defense-in-depth)
# ---------------------------------------------------------------------------


def _bearer_app(token):
    async def ping(request):
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/b/mcp", ping, methods=["GET"]),
            Route("/health", ping, methods=["GET"]),
            Route("/ready", ping, methods=["GET"]),
            Route("/health-check/mcp", ping, methods=["GET"]),
            Route("/ready-api/mcp", ping, methods=["GET"]),
            Route("/admin/api/state", ping, methods=["GET"]),
            Route("/admin/api/run", ping, methods=["POST"]),
            Route("/admin", ping, methods=["GET", "POST"]),
        ],
        middleware=[Middleware(server.BearerAuthMiddleware, token=token)],
    )


def test_bearer_no_token_is_passthrough():
    # token unset/empty -> pure passthrough, no header required anywhere
    for token in (None, ""):
        r = TestClient(_bearer_app(token)).get("/b/mcp")
        assert r.status_code == 200


def test_bearer_missing_header_is_401_with_challenge():
    r = TestClient(_bearer_app("sekret")).get("/b/mcp")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"
    assert r.json() == {"ok": False, "error": "missing or invalid bearer token"}


def test_bearer_wrong_token_is_401():
    client = TestClient(_bearer_app("sekret"))
    for bad in ("Bearer wrong", "Bearer sekret2", "sekret", "Basic sekret"):
        r = client.get("/b/mcp", headers={"Authorization": bad})
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"] == "Bearer"


def test_bearer_correct_token_passes():
    r = TestClient(_bearer_app("sekret")).get(
        "/b/mcp", headers={"Authorization": "Bearer sekret"}
    )
    assert r.status_code == 200


def test_bearer_exempts_health_ready_and_admin_page_only():
    # liveness probes stay open, and so does the bare GET /admin page (the UI
    # shell must load to prompt for the token) — but the admin API is
    # challenged: an unauthenticated local process could otherwise rewrite
    # config or execute backend tools via /admin/api/run (2026-07-12 audit).
    client = TestClient(_bearer_app("sekret"))
    for path in ("/health", "/ready", "/admin"):
        assert client.get(path).status_code == 200, path
    assert client.get("/admin/api/state").status_code == 401
    assert client.post("/admin/api/run").status_code == 401
    for path in ("/health-check/mcp", "/ready-api/mcp"):
        assert client.get(path).status_code == 401, path
    assert (
        client.get(
            "/admin/api/state", headers={"Authorization": "Bearer sekret"}
        ).status_code
        == 200
    )


def test_bearer_admin_page_open_is_get_only():
    # only the GET page shell is exempt — other methods on /admin are challenged
    client = TestClient(_bearer_app("sekret"))
    assert client.post("/admin").status_code == 401


def test_build_app_missing_bearer_env_fails_loudly(tmp_path, monkeypatch):
    # a ${ENV} bearer_token whose var is unset must raise ConfigError at BUILD
    # time (startup), never surface per request.
    monkeypatch.delenv("NO_SUCH_GW_TOKEN", raising=False)
    monkeypatch.setenv("MCP_GATEWAY_SECRETS", str(tmp_path / "absent.env"))
    cfg = cl.GatewayConfig.model_validate(
        {
            "bearer_token": "${NO_SUCH_GW_TOKEN}",
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    with pytest.raises(cl.ConfigError):
        server._build_app(
            cfg,
            structlog.get_logger("test"),
            {},
            {},
            {},
            config_path=str(tmp_path / "config.toml"),
        )


def test_build_app_empty_expanded_bearer_env_fails_loudly(tmp_path, monkeypatch):
    # a ${ENV} bearer_token whose var IS set but empty must also raise at build
    # time — an empty token would otherwise disable auth silently (the
    # middleware treats a falsy token as passthrough).
    monkeypatch.setenv("EMPTY_GW_TOKEN", "")
    cfg = cl.GatewayConfig.model_validate(
        {
            "bearer_token": "${EMPTY_GW_TOKEN}",
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    with pytest.raises(cl.ConfigError, match="expands to an empty string"):
        server._build_app(
            cfg,
            structlog.get_logger("test"),
            {},
            {},
            {},
            config_path=str(tmp_path / "config.toml"),
        )


def test_build_app_empty_expanded_oauth_admin_token_fails_loudly(tmp_path, monkeypatch):
    # the OAuth profile's admin_bearer_token guards /admin/api — same rule.
    monkeypatch.setenv("EMPTY_GW_TOKEN", "")
    cfg = cl.GatewayConfig.model_validate(
        {
            "oauth": {
                "public_base_url": "http://127.0.0.1:9100",
                "authorization_servers": ["http://127.0.0.1:9999"],
                "issuer": "http://127.0.0.1:9999",
                "jwks_uri": "http://127.0.0.1:9999/jwks",
                "admin_bearer_token": "${EMPTY_GW_TOKEN}",
            },
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    with pytest.raises(cl.ConfigError, match="expands to an empty string"):
        server._build_app(
            cfg,
            structlog.get_logger("test"),
            {},
            {},
            {},
            config_path=str(tmp_path / "config.toml"),
        )


def test_build_app_wires_bearer_auth(tmp_path, monkeypatch):
    # end-to-end wiring: token resolved from the env once, backend paths AND
    # the admin API gated; health/ready + the GET /admin shell exempt.
    monkeypatch.setenv("GW_TOKEN_26", "sekret")
    cfg = cl.GatewayConfig.model_validate(
        {
            "bearer_token": "${GW_TOKEN_26}",
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200  # exempt
        assert client.get("/ready").status_code == 503  # exempt (degraded, not 401)
        assert client.get("/admin").status_code == 200  # UI shell exempt
        assert client.get("/admin/api/state").status_code == 401  # API challenged
        assert (
            client.get(
                "/admin/api/state", headers={"Authorization": "Bearer sekret"}
            ).status_code
            == 200
        )
        r = client.get("/b/mcp")
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"] == "Bearer"
        # correct token passes the gate (404: the /bin/x backend never mounts)
        ok = client.get("/b/mcp", headers={"Authorization": "Bearer sekret"})
        assert ok.status_code != 401


# ---------------------------------------------------------------------------
# #50 — gateway.log is a rotating handler, JSON format unchanged
# ---------------------------------------------------------------------------


def test_configure_logging_rotates_and_keeps_json(tmp_path):
    log_path = tmp_path / "gateway.log"
    log = server._configure_logging(str(log_path))

    # The single queue handler lives on ROOT; its listener owns the rotating
    # file handler, so every logger — ours + uvicorn/fastmcp — flows into the one
    # rotating file without blocking the event loop.
    root = logging.getLogger()
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert type(handler).__name__ == "_AsyncQueueHandler"
    runtime = server.logging_setup.status()
    assert runtime["max_bytes"] > 0 and runtime["backup_count"] > 0
    assert runtime["listener_alive"] is True
    app_logger = logging.getLogger("mcp-gateway")
    assert app_logger.handlers == []
    assert app_logger.propagate is True

    log.info("hello_event", n=7)
    handler.flush()

    line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)  # must still be valid JSON
    assert rec["event"] == "hello_event"
    assert rec["n"] == 7
    assert rec["level"] == "info"


def test_library_warnings_route_into_gateway_log(tmp_path):
    """A stray library warning (uvicorn/fastmcp/etc.) must land in the rotating
    gateway.log, not launchd's err.log — that's what bounds err.log (issue #50)."""
    log_path = tmp_path / "gateway.log"
    server._configure_logging(str(log_path))

    logging.getLogger("uvicorn.error").warning("simulated library warning")
    logging.getLogger().handlers[0].flush()

    assert "simulated library warning" in log_path.read_text(encoding="utf-8")


def test_configure_logging_closes_prior_handler(tmp_path):
    """#87: a second setup must close the previous handler (no FD leak) and not
    accumulate handlers on the root logger."""
    server._configure_logging(str(tmp_path / "a.log"))
    prior = logging.getLogger().handlers[0]
    server._configure_logging(str(tmp_path / "b.log"))
    root = logging.getLogger()
    assert len(root.handlers) == 1  # replaced, not accumulated
    assert root.handlers[0] is not prior
    assert prior._listener is None or not prior._listener.alive  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# #57 — gateway version surfaced (single source) in /health and the admin state
# ---------------------------------------------------------------------------


def test_gateway_version_matches_pyproject():
    text = (admin.HERE.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    want = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text).group(1)
    assert admin.gateway_version() == want


def test_health_reports_version():
    client = TestClient(
        Starlette(routes=[Route("/health", server._health, methods=["GET"])])
    )
    r = client.get("/health")
    assert r.status_code == 200
    # still starts with "ok" so existing liveness checks pass; version is visible
    assert r.text.startswith("ok")
    assert admin.gateway_version() in r.text


def test_health_reports_resolved_code_path():
    # #149: /health names the daemon's REAL resolved code path, so a ghost
    # process running from a deleted/moved clone is visible to a one-line curl
    # (the path won't match where the repo lives now).
    client = TestClient(
        Starlette(routes=[Route("/health", server._health, methods=["GET"])])
    )
    r = client.get("/health")
    assert r.text.startswith("ok")
    assert f" @ {Path(server.__file__).resolve().parent}" in r.text


# ---------------------------------------------------------------------------
# #53/#56 — restart is only claimed when we're actually launchd-managed
# ---------------------------------------------------------------------------


def test_under_launchd_false_in_test_process():
    # pytest is never the launchd-managed daemon, so this is False — which is what
    # makes add/remove/restart report "dev-no-restart" instead of a stuck spinner.
    assert admin.under_launchd() is False


def _admin_app(tmp_path: Path, proxies=None) -> Starlette:
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = Starlette()
    # structlog logger: the app logs with kwargs (e.g. log.info("x", backend=...)),
    # which a stdlib logger rejects.
    admin.register(
        app,
        str(path),
        structlog.get_logger("test"),
        proxies if proxies is not None else {},
        {},
    )
    return app


def test_run_tool_rejects_non_string_tool(tmp_path):
    # #81: a non-string tool must 400 at the guard, not reach the proxy / 502.
    # A placeholder registry entry makes the backend "mounted" so the tool guard
    # (which runs after the mount check) is what fires.
    app = _run_app(tmp_path, {"b": object()})
    r = TestClient(app).post(
        "/admin/api/run", json={"backend": "b", "tool": 123, "args": {}}
    )
    assert r.status_code == 400
    assert "tool" in r.json()["error"]


def test_run_tool_unmounted_backend_is_400(tmp_path):
    r = TestClient(_run_app(tmp_path, {})).post(
        "/admin/api/run", json={"backend": "b", "tool": "t", "args": {}}
    )
    assert r.status_code == 400


def test_restart_route_dev_reports_no_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: False)
    r = TestClient(_admin_app(tmp_path)).post("/admin/api/restart")
    assert r.status_code == 200
    assert r.json()["reloaded"] == "dev-no-restart"


def test_restart_route_managed_reports_restarting(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: True)
    calls = []
    monkeypatch.setattr(admin, "restart_daemon", lambda log: calls.append(1))
    r = TestClient(_admin_app(tmp_path)).post("/admin/api/restart")
    assert r.status_code == 200
    assert r.json()["reloaded"] == "restarting"
    # the BackgroundTask (real kickstart, stubbed here) ran after the response
    assert calls == [1]


# ---------------------------------------------------------------------------
# #38/#40/#42 — backend enable/disable, master toggle, display-name routes
# ---------------------------------------------------------------------------


def _cfg_path(tmp_path) -> str:
    return str(tmp_path / "config.toml")


def test_enable_backend_route_persists_disabled(tmp_path):
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/enabled", json={"value": False}
    )
    assert r.status_code == 200
    assert cl.load(_cfg_path(tmp_path)).backends[0].enabled is False


def test_enable_backend_unknown_is_400(tmp_path):
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/nope/enabled", json={"value": True}
    )
    assert r.status_code == 400


def test_enable_all_route_sets_every_backend(tmp_path):
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/enabled", json={"value": False}
    )
    assert r.status_code == 200
    assert all(not b.enabled for b in cl.load(_cfg_path(tmp_path)).backends)


def test_remove_backend_prunes_defaults_file(tmp_path, monkeypatch):
    # #54: removing a backend must also delete its captured defaults JSON
    d = tmp_path / "defaults"
    d.mkdir()
    (d / "b.json").write_text("{}")
    monkeypatch.setattr(admin, "DEFAULTS_DIR", d)
    r = TestClient(_admin_app(tmp_path)).request("DELETE", "/admin/api/backend/b")
    assert r.status_code == 200
    assert not (d / "b.json").exists()


def test_remove_backend_unknown_is_400_and_keeps_defaults(tmp_path, monkeypatch):
    d = tmp_path / "defaults"
    d.mkdir()
    (d / "b.json").write_text("{}")
    monkeypatch.setattr(admin, "DEFAULTS_DIR", d)
    r = TestClient(_admin_app(tmp_path)).request("DELETE", "/admin/api/backend/nope")
    assert r.status_code == 400
    assert (d / "b.json").exists()


# ---------------------------------------------------------------------------
# #7 — import hot-adds the backend into the running daemon (no restart)
# ---------------------------------------------------------------------------


def _live_app(tmp_path):
    """The REAL parent app via _build_app — TestClient's context manager runs the
    lifespan, so the mounter task is live and hooks["add"] is installed."""
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    return server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )


def test_import_hot_adds_backend_without_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "DEFAULTS_DIR", tmp_path / "defaults")

    async def fake_capture(b):
        return {"backend": b.name, "instructions": None, "tools": []}

    monkeypatch.setattr(admin, "capture_defaults", fake_capture)
    with TestClient(_live_app(tmp_path)) as client:
        # stateless=True builds the proxy lazily, so mounting needs no live
        # backend; the (dead) URL only matters at call time.
        r = client.post(
            "/admin/api/backend",
            json={
                "name": "new",
                "transport": "http",
                "url": "http://127.0.0.1:9/mcp",
                "stateless": True,
            },
        )
        assert r.status_code == 200
        assert r.json()["reloaded"] == "hot-add"
        # The endpoint is mounted in the RUNNING app — same process, no restart.
        assert client.get("/new/mcp").status_code != 404
        # And the admin state serves it immediately (what the UI re-renders from).
        st = client.get("/admin/api/state").json()
        assert "new" in [b["name"] for b in st["backends"]]


def test_import_hot_add_config_persisted(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "DEFAULTS_DIR", tmp_path / "defaults")

    async def fake_capture(b):
        return {"backend": b.name, "instructions": None, "tools": []}

    monkeypatch.setattr(admin, "capture_defaults", fake_capture)
    with TestClient(_live_app(tmp_path)) as client:
        client.post(
            "/admin/api/backend",
            json={
                "name": "new",
                "transport": "http",
                "url": "http://127.0.0.1:9/mcp",
                "stateless": True,
            },
        )
    # survives the daemon lifecycle: it's in config.toml, not only in memory
    names = [b.name for b in cl.load(str(tmp_path / "config.toml")).backends]
    assert names == ["b", "new"]


# ---------------------------------------------------------------------------
# #61 — backends mount concurrently; a hung backend can't serialize the others
# ---------------------------------------------------------------------------


def test_slow_backend_does_not_block_boot_or_others(tmp_path, monkeypatch):
    """ "slow" is FIRST in config and hangs in connect. Sequential boot would
    never yield (TestClient enter would hang); concurrent boot serves /health
    immediately and mounts "fast" while slow is still stuck."""
    monkeypatch.setattr(server, "SHUTDOWN_GRACE", 0.1)  # don't wait on the hung one
    started, mounted = [], []

    async def fake_mount(
        app, stack, b, cfg, all_tools, meta, captured, _reg, _hold, log, *extra
    ):
        started.append(b.name)
        if b.name == "slow":
            await anyio.sleep(3600)  # hung connect; cancelled by SHUTDOWN_GRACE
        mounted.append(b.name)
        return True

    monkeypatch.setattr(server, "_mount_backend", fake_mount)
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": "slow", "transport": "stdio", "command": "/bin/x"},
                {"name": "fast", "transport": "stdio", "command": "/bin/y"},
            ]
        }
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200  # ready despite the hang
        for _ in range(100):  # fast mounts concurrently, behind no queue
            if "fast" in mounted:
                break
            time.sleep(0.02)
        assert set(started) == {"slow", "fast"}  # both began; nothing serialized
        assert mounted == ["fast"]  # slow still stuck -> only its endpoint waits
    # context exit returned -> shutdown didn't hang on the stuck runner


# ---------------------------------------------------------------------------
# #3 — admin "Run tool": execute through the live proxy, show the result
# ---------------------------------------------------------------------------


def _run_app(tmp_path, registry):
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = Starlette()
    admin.register(app, str(path), structlog.get_logger("test"), registry, {})
    return app


def _echo_server():
    from fastmcp import FastMCP

    m = FastMCP("b")

    @m.tool
    def echo(text: str) -> str:
        """Echo the input back."""
        return "echo: " + text

    @m.tool
    def boom() -> str:
        raise ValueError("kaboom")

    return m


def test_run_tool_executes_through_proxy(tmp_path):
    client = TestClient(_run_app(tmp_path, {"b": _echo_server()}))
    r = client.post(
        "/admin/api/run", json={"backend": "b", "tool": "echo", "args": {"text": "hi"}}
    )
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["is_error"] is False
    texts = [c["text"] for c in j["content"] if c["type"] == "text"]
    assert texts == ["echo: hi"]
    assert j["ms"] >= 0


def test_run_tool_surfaces_tool_error_not_500(tmp_path):
    client = TestClient(_run_app(tmp_path, {"b": _echo_server()}))
    r = client.post("/admin/api/run", json={"backend": "b", "tool": "boom", "args": {}})
    assert r.status_code == 200  # transport ok; the TOOL failed
    j = r.json()
    assert j["ok"] is True and j["is_error"] is True


def test_run_tool_unknown_backend_is_400(tmp_path):
    client = TestClient(_run_app(tmp_path, {}))
    r = client.post("/admin/api/run", json={"backend": "nope", "tool": "x", "args": {}})
    assert r.status_code == 400


def test_add_backend_does_not_lose_concurrent_edit(tmp_path, monkeypatch):
    """#52: add_backend awaits a network probe between config load and save. A
    config edit that lands during that await must NOT be overwritten — the
    handler re-loads the config under config_lock before committing."""
    monkeypatch.setattr(admin, "DEFAULTS_DIR", tmp_path / "defaults")
    cfg_path = _cfg_path(tmp_path)

    async def fake_capture(b):
        # simulate a concurrent admin edit landing while the probe is in flight
        cfg = cl.load(cfg_path)
        cfg.backends[0].display_name = "edited-during-probe"
        cl.save(cfg, cfg_path)
        return {"backend": b.name, "instructions": None, "tools": []}

    monkeypatch.setattr(admin, "capture_defaults", fake_capture)
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend",
        json={"name": "new", "transport": "stdio", "command": "/bin/y"},
    )
    assert r.status_code == 200
    after = cl.load(cfg_path)
    assert [b.name for b in after.backends] == ["b", "new"]  # backend added
    assert after.backends[0].display_name == "edited-during-probe"  # edit kept


def test_add_backend_duplicate_after_probe_is_400(tmp_path, monkeypatch):
    """The dup-check re-runs under the lock: a same-named backend appearing
    during the probe await is rejected instead of appended twice."""
    monkeypatch.setattr(admin, "DEFAULTS_DIR", tmp_path / "defaults")
    cfg_path = _cfg_path(tmp_path)

    async def fake_capture(b):
        cfg = cl.load(cfg_path)
        cfg.backends.append(cl.Backend(name="new", transport="stdio", command="/bin/z"))
        cl.save(cfg, cfg_path)
        return {"backend": b.name, "instructions": None, "tools": []}

    monkeypatch.setattr(admin, "capture_defaults", fake_capture)
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend",
        json={"name": "new", "transport": "stdio", "command": "/bin/y"},
    )
    assert r.status_code == 400
    assert [b.name for b in cl.load(cfg_path).backends] == ["b", "new"]


def test_override_route_uniquify_returns_final_name(tmp_path):
    # #22 — the PUT override route: strict reject by default; with the opt-in
    # flag it stores the suffixed name and reports it back to the UI.
    admin.DEFAULTS_DIR.mkdir(parents=True, exist_ok=True)  # conftest-isolated
    (admin.DEFAULTS_DIR / "b.json").write_text(
        json.dumps(
            {
                "backend": "b",
                "tools": [
                    {
                        "original": "t1",
                        "title": None,
                        "description": "d1",
                        "params": [],
                    },
                    {
                        "original": "t2",
                        "title": None,
                        "description": "d2",
                        "params": [],
                    },
                ],
            }
        )
    )
    client = TestClient(_admin_app(tmp_path))
    # default (no flag) stays exactly today's strict reject
    strict = client.put(
        "/admin/api/override",
        json={"backend": "b", "tool_original": "t1", "override": {"name": "t2"}},
    )
    assert strict.status_code == 400
    assert "already used" in strict.json()["error"]
    # opt-in flag -> 200 with the final stored name surfaced
    r = client.put(
        "/admin/api/override",
        json={
            "backend": "b",
            "tool_original": "t1",
            "on_collision": "uniquify",
            "override": {"name": "t2"},
        },
    )
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "reloaded": "in-process",
        "name": "t2_2",
        "uniquified": True,
    }
    assert cl.load(_cfg_path(tmp_path)).backends[0].tools[0].name == "t2_2"
    # a save that needed no uniquify keeps today's response shape
    r2 = client.put(
        "/admin/api/override",
        json={"backend": "b", "tool_original": "t1", "override": {"name": "fresh"}},
    )
    assert r2.json() == {"ok": True, "reloaded": "in-process"}


def test_display_name_route_sets_and_clears(tmp_path):
    client = TestClient(_admin_app(tmp_path))
    r1 = client.post("/admin/api/backend/b/display-name", json={"value": "Nice Label"})
    assert r1.status_code == 200
    assert cl.load(_cfg_path(tmp_path)).backends[0].display_name == "Nice Label"
    # blank clears back to None (falls back to the canonical name)
    r2 = client.post("/admin/api/backend/b/display-name", json={"value": "   "})
    assert r2.status_code == 200
    assert cl.load(_cfg_path(tmp_path)).backends[0].display_name is None


# ---------------------------------------------------------------------------
# #44 — hard-rename a backend (real identity change, unlike display_name #42)
# ---------------------------------------------------------------------------


def _cfg_app(tmp_path, cfg_dict) -> Starlette:
    """Admin app over an arbitrary config dict (rename needs tools/multiple
    backends, which _admin_app's fixed single-backend config can't express)."""
    cfg = cl.GatewayConfig.model_validate(cfg_dict)
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = Starlette()
    admin.register(app, str(path), structlog.get_logger("test"), {}, {})
    return app


def _write_defaults(name, tools=("t",)):
    """Captured-defaults stub (same shape as test_admin's _write_defaults);
    conftest already points admin.DEFAULTS_DIR at a throwaway dir."""
    d = admin.DEFAULTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(
        json.dumps(
            {
                "backend": name,
                "instructions": None,
                "tools": [
                    {"original": t, "title": None, "description": "d", "params": []}
                    for t in tools
                ],
            }
        )
    )


def test_rename_backend_renames_config_and_migrates_defaults(tmp_path):
    app = _cfg_app(
        tmp_path,
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "display_name": "Nice Label",
                    "tools": [{"original": "t", "name": "renamed_tool"}],
                }
            ]
        },
    )
    _write_defaults("b")
    r = TestClient(app).post("/admin/api/backend/b/rename", json={"value": "nb"})
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    # the UI needs both identities to tell the user what to reconfigure
    assert j["old_endpoint"] == "http://127.0.0.1:9100/b/mcp"
    assert j["new_endpoint"] == "http://127.0.0.1:9100/nb/mcp"
    assert j["old_registration"] == "gateway-b"
    assert j["new_registration"] == "gateway-nb"
    after = cl.load(str(tmp_path / "config.toml"))
    assert [x.name for x in after.backends] == ["nb"]  # config key renamed
    b = after.backends[0]
    assert b.display_name == "Nice Label"  # cosmetic label rides along
    assert [t.original for t in b.tools] == ["t"]  # overrides survive intact
    assert b.tools[0].name == "renamed_tool"
    # captured-defaults baseline migrated old -> new (backend key updated too)
    assert not (admin.DEFAULTS_DIR / "b.json").exists()
    migrated = json.loads((admin.DEFAULTS_DIR / "nb.json").read_text())
    assert migrated["backend"] == "nb"
    assert [t["original"] for t in migrated["tools"]] == ["t"]


def test_rename_backend_without_defaults_file_still_renames(tmp_path):
    # never-introspected backend: no defaults file to migrate — tolerated
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/rename", json={"value": "nb"}
    )
    assert r.status_code == 200
    assert [x.name for x in cl.load(_cfg_path(tmp_path)).backends] == ["nb"]


@pytest.mark.parametrize("bad", ["has space", "dot.name", "", "a" * 65, 7])
def test_rename_backend_invalid_name_is_400(tmp_path, bad):
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/rename", json={"value": bad}
    )
    assert r.status_code == 400
    assert [x.name for x in cl.load(_cfg_path(tmp_path)).backends] == ["b"]


def test_rename_backend_duplicate_name_is_400(tmp_path):
    app = _cfg_app(
        tmp_path,
        {
            "backends": [
                {"name": "b", "transport": "stdio", "command": "/bin/x"},
                {"name": "c", "transport": "stdio", "command": "/bin/x"},
            ]
        },
    )
    r = TestClient(app).post("/admin/api/backend/b/rename", json={"value": "c"})
    assert r.status_code == 400
    assert "already exists" in r.json()["error"]
    names = [x.name for x in cl.load(str(tmp_path / "config.toml")).backends]
    assert names == ["b", "c"]  # nothing changed


def test_rename_backend_unknown_is_400(tmp_path):
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/nope/rename", json={"value": "nb"}
    )
    assert r.status_code == 400


def test_backend_name_virtual_is_explicitly_rejected_on_add_and_rename(tmp_path):
    client = TestClient(_admin_app(tmp_path))
    renamed = client.post("/admin/api/backend/b/rename", json={"value": "virtual"})
    assert renamed.status_code == 400
    assert "reserved" in renamed.json()["error"]
    added = client.post(
        "/admin/api/backend",
        json={"name": "virtual", "transport": "stdio", "command": "/bin/x"},
    )
    assert added.status_code == 400
    assert "reserved" in added.json()["error"]
    assert [item.name for item in cl.load(_cfg_path(tmp_path)).backends] == ["b"]


@pytest.mark.parametrize("reserved", ["admin", "health", "ready"])
def test_backend_name_builtin_route_is_rejected_on_add_and_rename(tmp_path, reserved):
    # Same hazard as 'virtual': these names shadow built-in routes (/admin UI,
    # /health + /ready liveness — the latter two also bearer-auth exempt).
    client = TestClient(_admin_app(tmp_path))
    renamed = client.post("/admin/api/backend/b/rename", json={"value": reserved})
    assert renamed.status_code == 400
    assert "reserved" in renamed.json()["error"]
    added = client.post(
        "/admin/api/backend",
        json={"name": reserved, "transport": "stdio", "command": "/bin/x"},
    )
    assert added.status_code == 400
    assert "reserved" in added.json()["error"]
    assert [item.name for item in cl.load(_cfg_path(tmp_path)).backends] == ["b"]


def test_hot_rename_mount_failure_restores_config_defaults_and_old_mount(tmp_path):
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, path)
    _write_defaults("b")
    old_proxy = object()
    registry = {"b": old_proxy}
    calls = []

    async def add(backend):
        calls.append(("add", backend.name))
        return False

    def remove(name):
        calls.append(("remove", name))

    app = Starlette()
    admin.register(
        app,
        str(path),
        structlog.get_logger("test"),
        registry,
        {},
        {"add": add, "remove": remove},
    )
    response = TestClient(app).post("/admin/api/backend/b/rename", json={"value": "nb"})
    assert response.status_code == 500
    assert response.json()["reloaded"] == "mount-failed-rolled-back"
    assert [item.name for item in cl.load(path).backends] == ["b"]
    assert registry["b"] is old_proxy
    assert calls == [("add", "nb"), ("remove", "nb")]
    assert (admin.DEFAULTS_DIR / "b.json").is_file()
    assert not (admin.DEFAULTS_DIR / "nb.json").exists()


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/admin/api/cc-registrations"),
        ("GET", "/admin/api/codex-registrations"),
        ("POST", "/admin/api/cc-reregister-all"),
        ("POST", "/admin/api/backend/b/register"),
        ("POST", "/admin/api/backend/b/deregister"),
        ("POST", "/admin/api/backend/b/codex/register"),
        ("POST", "/admin/api/backend/b/codex/deregister"),
        ("POST", "/admin/api/virtual/codex/register"),
        ("POST", "/admin/api/virtual/codex/deregister"),
    ],
)
def test_client_registration_routes_are_not_exposed(tmp_path, method, path):
    response = TestClient(_admin_app(tmp_path)).request(method, path, json={})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# #153 — stale-override migrate/discard routes
# ---------------------------------------------------------------------------


def _dangling_defaults():
    """Captured baseline: the RENAMED tool ("new-tool") with a "keep" param."""
    admin.DEFAULTS_DIR.mkdir(parents=True, exist_ok=True)
    (admin.DEFAULTS_DIR / "b.json").write_text(
        json.dumps(
            {
                "backend": "b",
                "instructions": None,
                "tools": [
                    {
                        "original": "new-tool",
                        "title": None,
                        "description": "nd",
                        "params": [{"original": "keep", "description": "kd"}],
                    }
                ],
            }
        )
    )


def test_migrate_override_route_happy_path(tmp_path):
    _dangling_defaults()
    app = _cfg_app(
        tmp_path,
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [
                        {
                            "original": "old-tool",
                            "name": "shiny",
                            "description": "tuned",
                            "params": [
                                {"original": "keep", "description": "better"},
                                {"original": "gone", "description": "lost"},
                            ],
                        }
                    ],
                }
            ]
        },
    )
    r = TestClient(app).post(
        "/admin/api/backend/b/migrate-override",
        json={"from": "old-tool", "to": "new-tool"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["carried_params"] == ["keep"]
    assert j["dropped_params"] == ["gone"]  # "gone" isn't a param of "new-tool"
    after = cl.load(str(tmp_path / "config.toml")).backends[0]
    tools = {t.original: t for t in after.tools}
    assert "old-tool" not in tools  # dangling entry removed
    assert tools["new-tool"].name == "shiny"
    assert tools["new-tool"].description == "tuned"
    assert [p.original for p in tools["new-tool"].params] == ["keep"]


def test_migrate_override_route_unknown_target_is_400(tmp_path):
    _write_defaults("b", ("new-tool",))
    app = _cfg_app(
        tmp_path,
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "old-tool", "name": "shiny"}],
                }
            ]
        },
    )
    r = TestClient(app).post(
        "/admin/api/backend/b/migrate-override",
        json={"from": "old-tool", "to": "ghost"},
    )
    assert r.status_code == 400
    # nothing changed — the dangling entry survives the rejected migrate
    after = cl.load(str(tmp_path / "config.toml")).backends[0]
    assert [t.original for t in after.tools] == ["old-tool"]


def test_discard_override_route_drops_dangling(tmp_path):
    _write_defaults("b", ("new-tool",))
    app = _cfg_app(
        tmp_path,
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "tools": [{"original": "old-tool", "name": "shiny"}],
                }
            ]
        },
    )
    r = TestClient(app).post(
        "/admin/api/backend/b/discard-override", json={"original": "old-tool"}
    )
    assert r.status_code == 200
    assert cl.load(str(tmp_path / "config.toml")).backends[0].tools == []


def test_discard_override_route_unknown_is_400(tmp_path):
    _write_defaults("b", ("new-tool",))
    app = _cfg_app(
        tmp_path,
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]},
    )
    r = TestClient(app).post(
        "/admin/api/backend/b/discard-override", json={"original": "nope"}
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# #96 — bad config recovers from a backup instead of crash-looping
# ---------------------------------------------------------------------------


def _good_cfg():
    return cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )


def test_load_config_valid_passthrough(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    cl.save(_good_cfg(), str(p))
    monkeypatch.setattr(server, "CONFIG_PATH", str(p))
    cfg = server._load_config_or_recover(structlog.get_logger("test"))
    assert cfg.backends[0].name == "b"


def test_recover_from_backup_on_bad_config(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    cl.save(_good_cfg(), str(backups / "config-20260101-000000.toml"))
    bad = tmp_path / "config.toml"
    bad.write_text("this is = not valid toml [[[")
    monkeypatch.setattr(server, "CONFIG_PATH", str(bad))
    monkeypatch.setattr(admin, "BACKUP_DIR", backups)
    cfg = server._load_config_or_recover(structlog.get_logger("test"))
    assert cfg.backends[0].name == "b"  # recovered from the backup


def test_recover_raises_when_no_valid_backup(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "config-20260101-000000.toml").write_text("also = broken [[[")
    bad = tmp_path / "config.toml"
    bad.write_text("not = valid [[[")
    monkeypatch.setattr(server, "CONFIG_PATH", str(bad))
    monkeypatch.setattr(admin, "BACKUP_DIR", backups)
    with pytest.raises(Exception):  # noqa: B017 — main() catches this -> clean exit
        server._load_config_or_recover(structlog.get_logger("test"))


# ---------------------------------------------------------------------------
# #85 — a hung backend probe times out instead of blocking boot/import
# ---------------------------------------------------------------------------


def test_capture_defaults_times_out(monkeypatch):
    monkeypatch.setattr(admin, "CAPTURE_TIMEOUT", 0.05)

    class HangingClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            await anyio.sleep(10)  # never resolves within the timeout

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(admin, "Client", HangingClient)
    b = cl.Backend(name="b", transport="stdio", command="/bin/x")
    with pytest.raises(TimeoutError):
        anyio.run(admin.capture_defaults, b)


# ---------------------------------------------------------------------------
# #79 — perf: skip empty reconcile + cache gateway_version
# ---------------------------------------------------------------------------


def test_reconcile_skips_when_no_overrides():
    # Empty index -> no-op (returns cleanly, never inspects the tool list).
    server._reconcile({}, [], structlog.get_logger("test"))


def test_reconcile_warns_on_override_missing_from_captured_defaults():
    # #105: reconcile against captured defaults (no live round-trip). A config
    # `original` not in the captured tool list is flagged.
    warned = []

    class _L:
        def warning(self, _event, **kw):
            warned.append(kw.get("tool"))

        def info(self, *a, **kw):
            pass

    server._reconcile({"typo": "b", "real": "b"}, ["real"], _L())
    assert warned == ["typo"]  # only the unknown key warns


def test_gateway_version_is_cached():
    admin.gateway_version.cache_clear()
    v1 = admin.gateway_version()
    v2 = admin.gateway_version()
    assert v1 == v2
    assert admin.gateway_version.cache_info().hits >= 1


# ---------------------------------------------------------------------------
# #94 — /ready reflects backend mount status (distinct from /health liveness)
# ---------------------------------------------------------------------------


def test_ready_reports_degraded_when_backend_unmounted(tmp_path):
    # The stdio /bin/x backend can't complete an MCP handshake, so it never
    # mounts -> /ready is degraded (503) and names it missing, while /health
    # (liveness) still answers ok.
    with TestClient(_live_app(tmp_path)) as client:
        assert client.get("/health").status_code == 200
        r = client.get("/ready")
        assert r.status_code == 503
        body = r.json()
        assert body["ready"] is False
        assert "b" in body["enabled"] and "b" in body["missing"]
        assert body["mounted"] == []


# ---------------------------------------------------------------------------
# #90 — don't advertise listChanged (we never push the notification)
# ---------------------------------------------------------------------------


def test_backend_initialize_metadata_matches_gateway_surface():
    from fastmcp.server import create_proxy

    b = cl.Backend(name="b", transport="stdio", command="/bin/x")
    proxy = create_proxy(
        cl.to_proxy_config_one(b),
        name="mcp-gateway-b",
        version=admin.gateway_version(),
    )
    server._suppress_list_changed(proxy)
    server._set_gateway_capabilities(proxy)
    options = proxy._mcp_server.create_initialization_options()

    assert options.server_name == "mcp-gateway-b"
    assert options.server_version == admin.gateway_version()
    assert options.capabilities.model_dump(exclude_none=True) == {
        "logging": {},
        "prompts": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
        "tools": {"listChanged": False},
    }


def test_virtual_initialize_metadata_advertises_tools_only():
    virtual = server.virtual_tools.build_virtual_server(
        cl.GatewayConfig(), cl.GatewayConfig(), {}, structlog.get_logger("test")
    )
    server._suppress_list_changed(virtual)
    server._set_gateway_capabilities(virtual, tools_only=True)
    options = virtual._mcp_server.create_initialization_options()

    assert options.server_name == "mcp-gateway-virtual"
    assert options.server_version == admin.gateway_version()
    assert options.capabilities.model_dump(exclude_none=True) == {
        "tools": {"listChanged": False}
    }


def test_virtual_endpoint_applies_static_catalog_capabilities(tmp_path, monkeypatch):
    """The permanent Virtual Tools mount must not keep FastMCP's true default."""
    seen = []
    monkeypatch.setattr(
        server, "_suppress_list_changed", lambda fastmcp: seen.append(fastmcp.name)
    )
    cfg = cl.GatewayConfig()
    path = tmp_path / "config.toml"
    cl.save(cfg, path)
    app = server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        for _ in range(100):
            if seen:
                break
            time.sleep(0.01)
    assert seen == ["mcp-gateway-virtual"]


# ---------------------------------------------------------------------------
# #78 — disabled backends are never mounted (endpoint 404s); unmount cleans up
# ---------------------------------------------------------------------------


def test_boot_skips_disabled_backends(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "DEFAULTS_DIR", tmp_path / "defaults")
    mounted = []

    async def fake_mount(
        app, stack, b, cfg, all_tools, meta, captured, rt, log, *extra
    ):
        mounted.append(b.name)
        rt.mount(b.name, object(), [])
        return True

    monkeypatch.setattr(server, "_mount_backend", fake_mount)
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": "on", "transport": "stdio", "command": "/bin/x"},
                {
                    "name": "off",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "enabled": False,
                },
            ]
        }
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )
    with TestClient(app) as client:
        for _ in range(100):
            if "on" in mounted:
                break
            time.sleep(0.02)
        assert "on" in mounted
        assert "off" not in mounted  # #78: disabled backend never mounted
        r = client.get("/ready").json()
        assert r["enabled"] == ["on"]  # "off" isn't even expected to be mounted


def test_unmount_drops_route_and_registry():
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route

    inner = Starlette()
    app = Starlette(routes=[Route("/health", lambda r: None), Mount("/b", app=inner)])
    registry, holders = {"b": object()}, {"b": [object()]}
    server._unmount(app, "b", runtime.BackendRuntime.from_legacy(registry, holders))
    assert "b" not in registry and "b" not in holders
    paths = [getattr(r, "path", None) for r in app.router.routes]
    assert "/b" not in paths  # backend mount removed
    assert "/health" in paths  # other routes untouched


# ---------------------------------------------------------------------------
# #35 — a hidden param's injected default reaches the backend
# ---------------------------------------------------------------------------


def test_hidden_required_param_default_injected():
    """The whole #35 chain through a REAL FastMCP proxy: the hidden param
    vanishes from the broadcast schema, and the backend still receives the
    injected value on every call."""
    from fastmcp import Client, FastMCP
    from fastmcp.server import create_proxy

    m = FastMCP("b")

    @m.tool
    def greet(text: str, mode: str) -> str:
        """Greet."""
        return mode + ":" + text

    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",  # unused — the proxy wraps m in-process
                    "tools": [
                        {
                            "original": "greet",
                            "params": [
                                {"original": "mode", "hide": True, "default": "loud"}
                            ],
                        }
                    ],
                }
            ]
        }
    )
    proxy = create_proxy(m, name="mcp-gateway-b")
    transforms, _ = cl.build_transforms(cfg, cfg.backends[0])
    proxy.add_transform(transforms)

    async def go():
        async with Client(proxy) as c:
            (tool,) = [t for t in await c.list_tools() if t.name == "greet"]
            props = (tool.inputSchema or {}).get("properties", {})
            assert "mode" not in props  # hidden from Claude's broadcast
            assert "mode" not in (tool.inputSchema or {}).get("required", [])
            return await c.call_tool_mcp("greet", {"text": "hi"})

    res = anyio.run(go)
    assert res.isError is False
    texts = [blk.text for blk in res.content if blk.type == "text"]
    assert texts == ["loud:hi"]  # the injected default reached the backend


# ---------------------------------------------------------------------------
# #23 — /admin/api/status: per-backend liveness, isolated + bounded
# ---------------------------------------------------------------------------


def _status_app(tmp_path, registry, backends=None):
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": backends
            or [
                {"name": "b", "transport": "stdio", "command": "/bin/x"},
                {"name": "c", "transport": "stdio", "command": "/bin/y"},
            ]
        }
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = Starlette()
    admin.register(app, str(path), structlog.get_logger("test"), registry, {})
    return app


def test_status_ok_unmounted_disabled_error(tmp_path):
    backends = [
        {"name": "b", "transport": "stdio", "command": "/bin/x"},  # live
        {"name": "c", "transport": "stdio", "command": "/bin/y"},  # not mounted
        {"name": "d", "transport": "stdio", "command": "/bin/z", "enabled": False},
        {"name": "e", "transport": "stdio", "command": "/bin/w"},  # broken proxy
    ]
    registry = {"b": _echo_server(), "e": object()}  # e: Client() will choke
    client = TestClient(_status_app(tmp_path, registry, backends))
    r = client.get("/admin/api/status")
    assert r.status_code == 200
    s = r.json()["backends"]
    assert s["b"]["state"] == "ok" and s["b"]["tools"] == 2 and s["b"]["ms"] >= 0
    assert s["c"]["state"] == "unmounted"
    assert s["d"]["state"] == "disabled"
    assert s["e"]["state"] == "error" and s["e"]["error"]


# ---------------------------------------------------------------------------
# #43 — /admin/api/refresh + re-introspect delta + list_changed handler
# ---------------------------------------------------------------------------


def _fake_capture(tools):
    async def capture(b):
        return {
            "backend": b.name,
            "captured_at": 0,
            "instructions": None,
            "server_info": None,
            "capabilities": None,
            "tools": [
                {"original": t, "title": None, "description": "d", "params": []}
                for t in tools
            ],
        }

    return capture


def test_refresh_route_refreshes_mounted_skips_rest(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "capture_defaults", _fake_capture(["echo", "boom"]))
    registry = {"b": _echo_server()}
    client = TestClient(_status_app(tmp_path, registry))
    r = client.post("/admin/api/refresh")
    assert r.status_code == 200
    res = r.json()["backends"]
    assert res["b"]["status"] == "refreshed"
    assert sorted(res["b"]["added"]) == ["boom", "echo"]  # no prior baseline
    assert res["c"]["status"] == "skipped"  # not mounted
    # the baseline file landed
    assert {t["original"] for t in admin.load_defaults("b")["tools"]} == {
        "echo",
        "boom",
    }


def test_reintrospect_route_reports_delta_and_errors(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "capture_defaults", _fake_capture(["echo"]))
    registry = {"b": _echo_server()}
    client = TestClient(_status_app(tmp_path, registry))
    r = client.post("/admin/api/introspect/b")
    assert r.status_code == 200 and r.json()["added"] == ["echo"]

    assert client.post("/admin/api/introspect/nope").status_code == 400

    async def broken(b):
        raise RuntimeError("backend down")

    monkeypatch.setattr(admin, "capture_defaults", broken)
    r = client.post("/admin/api/introspect/b")  # force=True bypasses throttle
    assert r.status_code == 502
    assert "backend down" in r.json()["error"]


def test_list_changed_handler_enqueues_via_real_dispatch():
    """Tripwire on FastMCP's MessageHandler dispatch: a wire-shaped
    ToolListChangedNotification must reach on_tool_list_changed and enqueue the
    backend; a full queue is swallowed (a refresh is already pending)."""
    import mcp.types

    b = cl.Backend(name="b", transport="stdio", command="/bin/x")
    got = []
    h = server._ListChangedHandler(b, got.append, structlog.get_logger("test"))
    note = mcp.types.ServerNotification(
        mcp.types.ToolListChangedNotification(method="notifications/tools/list_changed")
    )
    anyio.run(h, note)
    assert got == [b]

    def full(_):
        raise anyio.WouldBlock

    h2 = server._ListChangedHandler(b, full, structlog.get_logger("test"))
    anyio.run(h2, note)  # must not raise


def test_post_mount_refresh_trigger_fires(tmp_path, monkeypatch):
    """#43 trigger 1: a successful mount schedules a background baseline
    refresh for that backend (throttled inside refresh_defaults)."""
    refreshed = []

    async def fake_refresh(b, *a, **k):
        refreshed.append(b.name)
        return {"status": "refreshed", "changed": False}

    monkeypatch.setattr(admin, "refresh_and_reload", fake_refresh)

    async def fake_mount(app, stack, b, *a, **k):
        return True

    monkeypatch.setattr(server, "_mount_backend", fake_mount)
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )
    with TestClient(app):
        for _ in range(100):
            if refreshed:
                break
            time.sleep(0.02)
    assert refreshed == ["b"]


def test_post_mount_refresh_passes_baseline_max_age(tmp_path, monkeypatch):
    """#157: the mount-time trigger carries cfg.baseline_max_age into the age
    gate — and ONLY that trigger (the list_changed worker and interval sweep
    stay ungated, max_age=0)."""
    seen = []

    async def fake_refresh(b, *a, **k):
        seen.append((b.name, k.get("max_age")))
        return {"status": "fresh"}

    monkeypatch.setattr(admin, "refresh_and_reload", fake_refresh)

    async def fake_mount(app, stack, b, *a, **k):
        return True

    monkeypatch.setattr(server, "_mount_backend", fake_mount)
    cfg = cl.GatewayConfig.model_validate(
        {
            "baseline_max_age": 1234,
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )
    with TestClient(app):
        for _ in range(100):
            if seen:
                break
            time.sleep(0.02)
    assert seen == [("b", 1234)]


def test_autorefresh_event_paths_are_ungated(tmp_path, monkeypatch):
    """#157: _AutoRefresh.refresh defaults to max_age=0 — the tools/list_changed
    worker and the interval sweep never skip on baseline age."""
    seen = []

    async def fake_refresh(b, *a, **k):
        seen.append(k.get("max_age"))
        return {"status": "refreshed", "changed": False}

    monkeypatch.setattr(admin, "refresh_and_reload", fake_refresh)
    cfg = cl.GatewayConfig.model_validate(
        {
            "baseline_max_age": 1234,
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    log = structlog.get_logger("test")
    b = cfg.backends[0]

    async def go():
        ar = server._AutoRefresh(
            0, cfg.baseline_max_age, str(path), runtime.BackendRuntime(), log
        )
        await ar.refresh(b)  # the worker/interval path
        await ar.post_mount(b)  # the gated path, for contrast

    anyio.run(go)
    assert seen == [0.0, 1234]


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_install_sh_is_executable_and_parses():
    # Keep the checkout compatibility wrapper valid for macOS's stock
    # /bin/bash 3.2 and executable.
    script = REPO_ROOT / "install.sh"
    assert os.access(script, os.X_OK)
    proc = subprocess.run(
        ["/bin/bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# admin.html integrity — the UI is a single hand-merged file; guard it
# ---------------------------------------------------------------------------


def test_admin_html_has_no_conflict_markers():
    # A merge conflict in admin.html once shipped committed (the CONFLICT line
    # scrolled out of a tail'ed merge log) — ruff doesn't lint HTML and no test
    # parsed the page, so the whole admin UI silently broke. Never again.
    html_path = REPO_ROOT / "src" / "mcp_gateway" / "admin.html"
    text = html_path.read_text(encoding="utf-8")
    for marker in ("<" * 7, "=" * 7 + "\n", ">" * 7):
        assert marker not in text, f"merge-conflict marker {marker[:7]!r} in admin.html"


def test_admin_html_inline_script_parses():
    # `node --check` the inline <script> so a syntax error (stray backtick,
    # bad template literal, conflict remnant) fails the gate, not the browser.
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    html_path = REPO_ROOT / "src" / "mcp_gateway" / "admin.html"
    text = html_path.read_text(encoding="utf-8")
    start = text.index("<script>") + len("<script>")
    end = text.index("</script>")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(text[start:end])
        js_path = fh.name
    try:
        proc = subprocess.run(
            [node, "--check", js_path], capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, proc.stderr
    finally:
        os.unlink(js_path)


def test_admin_html_has_update_check_contract():
    text = (REPO_ROOT / "src" / "mcp_gateway" / "admin.html").read_text(
        encoding="utf-8"
    )
    assert "const updateAvailable = update.available === true;" in text
    assert "const updateBadge = updateAvailable" in text
    assert "Update ${esc(latestVersion)} available" in text
    assert "<code>mcp-gateway update</code>" in text
    assert "It sends the installed version in the User-Agent" in text
    assert "offline failures are tolerated" in text
    assert "updates are never applied automatically" in text
    assert "STATE.update_check === true ? 'checked' : ''" in text
    assert "${STATE.update_check === true ? 'On' : 'Off'}" in text
    assert "update_check: $('#set_update_check').checked === true" in text


def test_admin_html_has_first_class_virtual_tools_surface():
    text = (REPO_ROOT / "src" / "mcp_gateway" / "admin.html").read_text(
        encoding="utf-8"
    )
    assert "Virtual Tools" in text
    assert "Create Virtual Tool" in text
    assert (
        'const VIRTUAL = "__virtual__"' in text
        or "const VIRTUAL = '__virtual__'" in text
    )
    assert "/virtual/mcp" in text
    for lifecycle in (
        "Save draft",
        "Save &amp; activate",
        "Validate &amp; resolve",
        "Test draft",
        "Disable",
        "Delete",
    ):
        assert lifecycle in text


def test_admin_html_virtual_tools_uses_adr_api_contract():
    text = (REPO_ROOT / "src" / "mcp_gateway" / "admin.html").read_text(
        encoding="utf-8"
    )
    for endpoint in (
        "/admin/api/virtual-tools",
        "/admin/api/virtual-catalog",
        "/validate",
        "/test",
        "/activate",
        "/disable",
    ):
        assert endpoint in text
    for stable_identity in ("backend_id", "tool_original"):
        assert stable_identity in text
    assert "Arguments (JSON object)" in text
    assert "prompt('Test arguments as JSON:'" not in text
    for security_field in (
        "egress_acknowledged",
        "API key reference",
        "external provider",
    ):
        assert security_field in text


def test_admin_html_metadata_limit_controls_use_utf8_and_api_contract():
    text = (REPO_ROOT / "src" / "mcp_gateway" / "admin.html").read_text(
        encoding="utf-8"
    )

    # Global, backend, tool, and Virtual Tool controls all serialize the exact
    # config/API field names. Scoped controls explicitly expose inheritance;
    # only the gateway-level tool limit exposes unlimited.
    for field in (
        "server_instructions_max_bytes",
        "tool_description_max_bytes",
        "description_max_bytes",
    ):
        assert f'name="{field}"' in text
    assert "/admin/api/backend/${encodeURIComponent(name)}/limits" in text
    assert "inherit gateway" in text
    assert "inherit backend" in text
    assert "tool_description_max_bytes_unlimited" in text

    # Browser-side validation must count encoded bytes, keep the inclusive
    # boundary valid, and announce both inline and API validation failures.
    assert "new TextEncoder()" in text
    assert "bytes > cap" in text
    assert "bytes <= cap" in text
    assert "Number.isInteger(value)" in text
    assert "value <= 1048576" in text
    assert 'role="status"' in text
    assert 'aria-live="polite"' in text
    assert 'role="alert"' in text

    # Effective limits come from state rather than the old hard-coded 2048 B
    # textarea cap, and every added static limit control has label semantics.
    assert 'data-cap="2048"' not in text
    for control in (
        "vt_description",
        "vt_description_limit",
        "set_instructions_limit",
        "set_tool_limit",
    ):
        assert f'for="{control}"' in text
    assert "effective_server_instructions_max_bytes" in text
    assert "effective_tool_description_max_bytes" in text
    assert "effective_description_max_bytes" in text


def _extract_admin_js_function(text: str, name: str) -> str:
    """Verbatim source of `function <name>(...) { ... }` from admin.html."""
    start = text.index(f"function {name}(")
    brace = text.index("{", start)
    depth = 0
    for i in range(brace, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unterminated function {name} in admin.html")


# Executed by node: drives the REAL parseByteLimit/scopedByteLimit extracted
# verbatim from admin.html. Fails on the pre-#286 parser, where Number() rounds
# 0.99999999999999999 -> 1 and 1048576.0000000001 -> 1048576 onto valid limits
# instead of rejecting the fractional strings.
_BYTE_LIMIT_PARSER_ASSERTIONS = r"""
const failures = [];
function makeInput(value) {
  return { value: String(value), classList: { toggle() {} }, setAttribute() {} };
}
function rejects(raw) {
  try {
    parseByteLimit(makeInput(raw), 'Test limit');
    failures.push('expected rejection: ' + JSON.stringify(raw));
  } catch (e) {
    if (!/must be an integer from 1 to 1,048,576 bytes/.test(String(e.message))) {
      failures.push('unexpected error for ' + JSON.stringify(raw) + ': ' + e.message);
    }
  }
}
function accepts(raw, expected) {
  const got = parseByteLimit(makeInput(raw), 'Test limit');
  if (got !== expected) {
    failures.push(
      'expected ' + expected + ', got ' + got + ' for ' + JSON.stringify(raw)
    );
  }
}
// Fractional strings Number() would round onto valid limits: reject.
rejects('0.99999999999999999');
rejects('1048576.0000000001');
rejects('1.5');
rejects('1048576.0');
// NaN/Infinity, empty/whitespace, signs, and out-of-range values stay rejected.
rejects('');
rejects('   ');
rejects('NaN');
rejects('Infinity');
rejects('-1');
rejects('0');
rejects('1048577');
// Inclusive 1..1048576 acceptance; surrounding whitespace is trimmed first.
accepts('1', 1);
accepts('1048576', 1048576);
accepts(' 1 ', 1);
accepts('2048', 2048);
// Scoped null semantics: inherit checked -> null without parsing; unchecked
// parses through parseByteLimit and still rejects invalid input.
const inheritOn = { checked: true };
const inheritOff = { checked: false };
if (scopedByteLimit(makeInput('not a number'), inheritOn, 'Test limit') !== null) {
  failures.push('inherit should yield null without parsing');
}
if (scopedByteLimit(makeInput('2048'), inheritOff, 'Test limit') !== 2048) {
  failures.push('scoped override should parse the limit');
}
try {
  scopedByteLimit(makeInput('1.5'), inheritOff, 'Test limit');
  failures.push('scoped override should reject invalid input');
} catch (e) { /* expected */ }
if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
"""


def test_admin_html_byte_limit_parser_rejects_fractional_rounding():
    # #286: browser limit parsing must require a plain-decimal integer on the
    # trimmed raw string before any Number() conversion, so fractional strings
    # that round onto valid limits (0.99999999999999999 -> 1,
    # 1048576.0000000001 -> 1048576) are rejected. The token assertions in
    # test_admin_html_metadata_limit_controls_use_utf8_and_api_contract remain
    # as a static guard; this one executes the shipped functions.
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    text = (REPO_ROOT / "src" / "mcp_gateway" / "admin.html").read_text(
        encoding="utf-8"
    )
    harness = (
        _extract_admin_js_function(text, "parseByteLimit")
        + "\n"
        + _extract_admin_js_function(text, "scopedByteLimit")
        + "\n"
        + _BYTE_LIMIT_PARSER_ASSERTIONS
    )
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as fh:
        fh.write(harness)
        js_path = fh.name
    try:
        proc = subprocess.run(
            [node, js_path], capture_output=True, text=True, check=False
        )
        assert proc.returncode == 0, proc.stderr or proc.stdout
    finally:
        os.unlink(js_path)


def test_interval_refresh_loop_sweeps_and_stops(tmp_path, monkeypatch):
    """#43 trigger 4: with an interval set, mounted+enabled backends are swept
    on the clock; close() ends the loop promptly (no shutdown hang)."""
    refreshed = []

    async def fake_refresh(b, *a, **k):
        refreshed.append(b.name)
        return {"status": "refreshed", "changed": False}

    monkeypatch.setattr(admin, "refresh_and_reload", fake_refresh)
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {"name": "up", "transport": "stdio", "command": "/bin/x"},
                {"name": "down", "transport": "stdio", "command": "/bin/y"},
            ]
        }
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    log = structlog.get_logger("test")

    async def go():
        ar = server._AutoRefresh(
            1,
            0,
            str(path),
            runtime.BackendRuntime.from_legacy({"up": object()}, {}),
            log,
        )
        ar.interval = 0.05  # fast clock for the test
        async with anyio.create_task_group() as tg:
            tg.start_soon(ar.interval_loop)
            with anyio.fail_after(2):
                while not refreshed:
                    await anyio.sleep(0.01)
            ar.close()  # must end the loop; the task group then drains

    anyio.run(go)
    assert refreshed and set(refreshed) == {"up"}  # only mounted+enabled swept


# ---------------------------------------------------------------------------
# Origin guard — MCP spec DNS-rebinding protection (Streamable HTTP security)
# ---------------------------------------------------------------------------


def _origin_app():
    async def ping(request):
        return JSONResponse({"ok": True})

    return Starlette(
        routes=[
            Route("/b/mcp", ping, methods=["GET"]),
            Route("/admin/api/state", ping, methods=["GET"]),
        ],
        middleware=[
            Middleware(server.OriginGuardMiddleware, host="127.0.0.1", port=9100)
        ],
    )


def test_origin_absent_passes():
    # non-browser clients (Claude Code, curl) send no Origin
    client = TestClient(_origin_app())
    assert client.get("/b/mcp").status_code == 200


def test_origin_own_gateway_passes():
    # the admin UI's same-origin fetches, on any loopback spelling
    client = TestClient(_origin_app())
    for origin in (
        "http://127.0.0.1:9100",
        "http://localhost:9100",
        "http://[::1]:9100",
    ):
        r = client.get("/admin/api/state", headers={"Origin": origin})
        assert r.status_code == 200, origin


def test_origin_foreign_is_403():
    # the DNS-rebinding shape: a browser page's own origin against loopback —
    # spec: MUST reject an invalid Origin with 403
    client = TestClient(_origin_app())
    for origin in (
        "http://evil.example",
        "https://127.0.0.1:9100",  # scheme matters
        "http://127.0.0.1:9999",  # port matters
        "null",  # sandboxed documents
    ):
        r = client.get("/b/mcp", headers={"Origin": origin})
        assert r.status_code == 403, origin
        assert r.json()["error"] == "invalid origin"


def test_build_app_wires_origin_guard(tmp_path):
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200  # no Origin -> open
        r = client.get("/health", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# #286 — admin middleware order: bearer auth BEFORE the body cap
# ---------------------------------------------------------------------------


def _stacked_app(tmp_path, monkeypatch):
    """The REAL parent app from _build_app, driven raw (no lifespan/backends):
    only the middleware stack is under test. Token resolved from env, exactly
    like production."""
    monkeypatch.setenv("GW_TOKEN_286", "sekret")
    cfg = cl.GatewayConfig.model_validate(
        {
            "bearer_token": "${GW_TOKEN_286}",
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    return server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=str(path)
    )


def _drive_raw(app, *, headers, receive):
    """Run one raw ASGI request against the app; return the send messages."""
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/admin/api/state",
        "raw_path": b"/admin/api/state",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1:8000"), *headers],
        "client": ("127.0.0.1", 4321),
        "server": ("127.0.0.1", 8000),
    }
    sent = []

    async def send(message):
        sent.append(message)

    async def go():
        await app(scope, receive, send)

    anyio.run(go)
    return sent


def _sent_body(sent):
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    )


def test_unauth_admin_api_401_without_buffering_body(tmp_path, monkeypatch):
    """#286 regression: the bearer gate sits OUTSIDE the body cap, so a
    missing or invalid token on /admin/api/* answers 401 without the gateway
    ever calling receive() — a receive that raises proves no body was read or
    buffered, so an attacker can't make the daemon chew on their payload."""
    app = _stacked_app(tmp_path, monkeypatch)

    async def never_receive():
        raise AssertionError("receive() was called before authentication")

    for auth in (None, "Bearer wrong", "Basic sekret"):
        headers = [(b"authorization", auth.encode())] if auth is not None else []
        sent = _drive_raw(app, headers=headers, receive=never_receive)
        assert sent[0]["status"] == 401, auth
        assert dict(sent[0]["headers"])[b"content-type"] == b"application/json"
        assert json.loads(_sent_body(sent)) == {
            "ok": False,
            "error": "missing or invalid bearer token",
        }


def test_authenticated_oversized_admin_body_still_413(tmp_path, monkeypatch):
    # behavior-preserving: the body cap still bites for VALID tokens — the
    # declared Content-Length alone rejects, without buffering (receive must
    # not run here either).
    app = _stacked_app(tmp_path, monkeypatch)

    async def never_receive():
        raise AssertionError("receive() was called for an over-limit body")

    sent = _drive_raw(
        app,
        headers=[
            (b"authorization", b"Bearer sekret"),
            (b"content-length", str(server.ADMIN_BODY_LIMIT + 1).encode()),
        ],
        receive=never_receive,
    )
    assert sent[0]["status"] == 413
    assert json.loads(_sent_body(sent)) == {
        "ok": False,
        "error": "request body too large",
    }


def test_authenticated_admin_api_still_200(tmp_path, monkeypatch):
    # behavior-preserving: a valid token on a normal request still reaches the
    # handler through the reordered stack.
    app = _stacked_app(tmp_path, monkeypatch)

    async def empty_receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    sent = _drive_raw(
        app,
        headers=[(b"authorization", b"Bearer sekret")],
        receive=empty_receive,
    )
    assert sent[0]["status"] == 200


# ---------------------------------------------------------------------------
# #161 — supervised warm sessions: dead-session detection + automatic recycle
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        anyio.ClosedResourceError(),
        anyio.BrokenResourceError(),
        anyio.EndOfStream(),
        BrokenPipeError("broken pipe"),
        ConnectionResetError("connection reset by peer"),
        RuntimeError("Session terminated (HTTP 404)"),
        RuntimeError("peer closed connection"),
        Exception("Server disconnected without sending a response"),
    ],
)
def test_is_session_death_matches_transport_signatures(exc):
    assert server.is_session_death(exc) is True


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("kaboom"),  # a normal tool error must NOT look like a dead session
        RuntimeError("invalid argument: missing field 'query'"),
        Exception("tool returned an error"),
        KeyError("nope"),
    ],
)
def test_is_session_death_ignores_ordinary_errors(exc):
    assert server.is_session_death(exc) is False


class _FakeCtx:
    message = type("M", (), {"name": "sometool"})()


def _run_middleware(mw, raised):
    async def call_next(_ctx):
        raise raised

    return anyio.run(mw.on_call_tool, _FakeCtx(), call_next)


def test_middleware_recycles_on_session_death_and_reraises():
    fired = []
    mw = server.CallLogMiddleware(
        structlog.get_logger("test"), "b", lambda: fired.append(1)
    )
    with pytest.raises(anyio.ClosedResourceError):
        _run_middleware(mw, anyio.ClosedResourceError())
    assert fired == [1]  # recycle scheduled exactly once, call still failed


def test_middleware_does_not_recycle_on_ordinary_error():
    fired = []
    mw = server.CallLogMiddleware(
        structlog.get_logger("test"), "b", lambda: fired.append(1)
    )
    with pytest.raises(ValueError):
        _run_middleware(mw, ValueError("kaboom"))
    assert fired == []  # a normal tool error is NOT a dead session


def test_middleware_stateless_backend_never_recycles():
    # on_session_death=None (a stateless backend) -> the trigger is never called
    # even for a genuine session-death exception (there is no warm session to heal).
    mw = server.CallLogMiddleware(structlog.get_logger("test"), "b", None)
    with pytest.raises(anyio.ClosedResourceError):
        _run_middleware(mw, anyio.ClosedResourceError())


def _warm_cfg(tmp_path):
    cfg = cl.GatewayConfig.model_validate(
        {
            "backends": [
                {
                    "name": "b",
                    "transport": "stdio",
                    "command": "/bin/x",
                    "stateless": False,
                }
            ]
        }
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    return cfg, str(path)


def _recycle_app(tmp_path, monkeypatch, mounts):
    """A live app whose _mount_backend is faked to just record each mount and
    register the backend, so recycle (teardown + re-run) is observable without a
    real backend. refresh is stubbed out (no real capture on the fake command)."""

    async def fake_mount(
        app, stack, b, cfg, all_tools, meta, captured, rt, log, *extra
    ):
        mounts.append(b.name)
        rt.mount(b.name, object(), [])
        return True

    async def fake_refresh(*a, **k):
        return {"status": "throttled"}

    monkeypatch.setattr(server, "_mount_backend", fake_mount)
    monkeypatch.setattr(admin, "refresh_and_reload", fake_refresh)
    cfg, path = _warm_cfg(tmp_path)
    return server._build_app(
        cfg, structlog.get_logger("test"), {}, {}, {}, config_path=path
    ), path


def _wait_for(pred, tries=200, delay=0.02):
    for _ in range(tries):
        if pred():
            return True
        time.sleep(delay)
    return False


def test_stateless_route_persists_and_recycles(tmp_path, monkeypatch):
    mounts: list[str] = []
    app, path = _recycle_app(tmp_path, monkeypatch, mounts)
    with TestClient(app) as client:
        assert _wait_for(lambda: mounts.count("b") >= 1)  # initial mount
        r = client.post("/admin/api/backend/b/stateless", json={"value": True})
        assert r.status_code == 200
        assert r.json()["reloaded"] == "recycled"
        # persisted to config
        assert cl.load(path).backends[0].stateless is True
        # recycled -> the backend was re-mounted (teardown + fresh re-run)
        assert _wait_for(lambda: mounts.count("b") >= 2)


def test_recycle_cooldown_suppresses_second_recycle(tmp_path, monkeypatch):
    mounts: list[str] = []
    app, path = _recycle_app(tmp_path, monkeypatch, mounts)
    with TestClient(app) as client:
        assert _wait_for(lambda: mounts.count("b") >= 1)
        # pre-stamp the cooldown as if a recycle just happened -> the toggle's
        # recycle is skipped; the config change still persists.
        server._last_recycle["b"] = time.monotonic()
        r = client.post("/admin/api/backend/b/stateless", json={"value": True})
        assert r.status_code == 200
        assert cl.load(path).backends[0].stateless is True
        time.sleep(0.3)  # give any (suppressed) recycle a chance to run
        assert mounts.count("b") == 1  # cooldown suppressed the re-mount


def test_stateless_route_persists_without_lifespan_hook(tmp_path):
    # A bare admin app (no lifespan) has no recycle hook — the value must still
    # persist and the route must still succeed.
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/stateless", json={"value": True}
    )
    assert r.status_code == 200
    assert cl.load(_cfg_path(tmp_path)).backends[0].stateless is True


def test_stateless_route_unknown_backend_is_400(tmp_path):
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/nope/stateless", json={"value": True}
    )
    assert r.status_code == 400


def test_status_probe_recycles_warm_backend_on_error(tmp_path):
    # #161: a WARM backend that probes `error` triggers the recycle hook.
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    fired: list[str] = []
    app = Starlette()
    # a non-proxy object in the registry makes Client(proxy) fail -> error state
    admin.register(
        app,
        str(path),
        structlog.get_logger("test"),
        {"b": object()},
        {},
        {"recycle": fired.append},
    )
    r = TestClient(app).get("/admin/api/status")
    assert r.status_code == 200
    assert r.json()["backends"]["b"]["state"] == "error"
    assert fired == ["b"]


def test_import_default_is_warm_in_admin_html():
    # #161: newly imported backends default to warm (stateless: false) for EVERY
    # transport now that a dead warm session auto-recycles.
    html = (REPO_ROOT / "src" / "mcp_gateway" / "admin.html").read_text(
        encoding="utf-8"
    )
    assert "stateless: false" in html
    assert "stateless: !stdio" not in html  # the old transport-conditional is gone


# ---------------------------------------------------------------------------
# #155 — gateway settings card (bearer token ref + introspect interval)
# ---------------------------------------------------------------------------


def test_settings_get_returns_current(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: False)
    client = TestClient(_admin_app(tmp_path))
    j = client.get("/admin/api/settings").json()
    assert j == {
        "bearer_token": None,
        "introspect_interval": 0,
        "log_level": "INFO",
        "log_max_bytes": 5 * 1024 * 1024,
        "log_backup_count": 5,
        "server_instructions_max_bytes": 2048,
        "tool_description_max_bytes": None,
        "update_check": True,
    }


def test_settings_put_roundtrips(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: False)
    client = TestClient(_admin_app(tmp_path))
    r = client.put(
        "/admin/api/settings",
        json={"bearer_token": "${MCP_GATEWAY_TOKEN}", "introspect_interval": 45},
    )
    assert r.status_code == 200
    # persisted
    cfg = cl.load(_cfg_path(tmp_path))
    assert cfg.bearer_token == "${MCP_GATEWAY_TOKEN}"
    assert cfg.introspect_interval == 45
    # and readable back through GET
    j = client.get("/admin/api/settings").json()
    assert j["bearer_token"] == "${MCP_GATEWAY_TOKEN}"
    assert j["introspect_interval"] == 45


def test_settings_put_roundtrips_logging_controls(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: False)
    client = TestClient(_admin_app(tmp_path))
    r = client.put(
        "/admin/api/settings",
        json={"log_level": "DEBUG", "log_max_bytes": 131072, "log_backup_count": 2},
    )
    assert r.status_code == 200
    cfg = cl.load(_cfg_path(tmp_path))
    assert cfg.log_level == "DEBUG"
    assert cfg.log_max_bytes == 131072
    assert cfg.log_backup_count == 2


@pytest.mark.parametrize(
    "payload",
    [
        {"log_level": "verbose"},
        {"log_max_bytes": 1024},
        {"log_backup_count": 0},
    ],
)
def test_settings_put_rejects_invalid_logging_controls(tmp_path, payload):
    response = TestClient(_admin_app(tmp_path)).put("/admin/api/settings", json=payload)
    assert response.status_code == 400


def test_settings_put_rejects_raw_secret(tmp_path):
    r = TestClient(_admin_app(tmp_path)).put(
        "/admin/api/settings", json={"bearer_token": "sk-live-deadbeef"}
    )
    assert r.status_code == 400
    assert "environment variable" in r.json()["error"]


def test_settings_put_empty_token_clears(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: False)
    client = TestClient(_admin_app(tmp_path))
    client.put("/admin/api/settings", json={"bearer_token": "${TOK}"})
    r = client.put("/admin/api/settings", json={"bearer_token": ""})
    assert r.status_code == 200
    assert cl.load(_cfg_path(tmp_path)).bearer_token is None


@pytest.mark.parametrize("bad", [-1, 1.5, True, "5", None])
def test_settings_put_rejects_bad_interval(tmp_path, bad):
    r = TestClient(_admin_app(tmp_path)).put(
        "/admin/api/settings", json={"introspect_interval": bad}
    )
    assert r.status_code == 400


def test_settings_put_dev_reports_no_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: False)
    r = TestClient(_admin_app(tmp_path)).put(
        "/admin/api/settings", json={"introspect_interval": 5}
    )
    assert r.json()["reloaded"] == "dev-no-restart"


def test_settings_put_managed_reports_restarting(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: True)
    monkeypatch.setattr(admin, "restart_daemon", lambda log: None)
    r = TestClient(_admin_app(tmp_path)).put(
        "/admin/api/settings", json={"introspect_interval": 5}
    )
    assert r.json()["reloaded"] == "restarting"


def test_build_state_surfaces_gateway_settings(tmp_path):
    cfg = cl.GatewayConfig.model_validate(
        {
            "bearer_token": "${TOK}",
            "introspect_interval": 30,
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    st = admin.build_state(cfg)
    assert st["bearer_token"] == "${TOK}"  # the ${ENV} REF, never resolved
    assert st["introspect_interval"] == 30


# ---------------------------------------------------------------------------
# #A8 — update-check toggle: state shape, settings API, lifespan monitor wiring
# ---------------------------------------------------------------------------


_STATUS = {
    "current_version": "1.2.3",
    "latest_version": "1.2.4",
    "available": True,
    "checked_at": "2026-08-03T00:00:00+00:00",
    "error": None,
}


def test_build_state_surfaces_update_check_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.updates, "current_status", lambda: _STATUS)
    cfg = cl.GatewayConfig.model_validate(
        {
            "update_check": False,
            "backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}],
        }
    )
    st = admin.build_state(cfg)
    assert st["update_check"] is False
    # the EXACT shared status mapping — no paths, credentials, or internals
    assert st["update"] == _STATUS
    assert set(_STATUS) == {
        "current_version",
        "latest_version",
        "available",
        "checked_at",
        "error",
    }


def test_state_endpoint_includes_update_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.updates, "current_status", lambda: _STATUS)
    j = TestClient(_admin_app(tmp_path)).get("/admin/api/state").json()
    assert j["update_check"] is True  # default on
    assert j["update"] == _STATUS


def test_settings_get_returns_update_check(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: False)
    j = TestClient(_admin_app(tmp_path)).get("/admin/api/settings").json()
    assert j["update_check"] is True


def test_settings_put_update_check_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: False)
    client = TestClient(_admin_app(tmp_path))
    r = client.put("/admin/api/settings", json={"update_check": False})
    assert r.status_code == 200
    cfg = cl.load(_cfg_path(tmp_path))
    assert cfg.update_check is False
    # readable back through GET, and the TOML carries the explicit opt-out
    assert client.get("/admin/api/settings").json()["update_check"] is False
    assert "update_check = false" in Path(_cfg_path(tmp_path)).read_text()


@pytest.mark.parametrize("bad", [0, 1, "false", "yes", None, [], {}])
def test_settings_put_rejects_non_boolean_update_check(tmp_path, bad):
    r = TestClient(_admin_app(tmp_path)).put(
        "/admin/api/settings", json={"update_check": bad}
    )
    assert r.status_code == 400
    assert "update_check" in r.json()["error"]


def test_settings_put_update_check_dev_reports_no_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: False)
    r = TestClient(_admin_app(tmp_path)).put(
        "/admin/api/settings", json={"update_check": False}
    )
    assert r.json()["reloaded"] == "dev-no-restart"


def test_settings_put_update_check_managed_reports_restarting(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "under_launchd", lambda: True)
    calls = []
    monkeypatch.setattr(admin, "restart_daemon", lambda log: calls.append(1))
    r = TestClient(_admin_app(tmp_path)).put(
        "/admin/api/settings", json={"update_check": False}
    )
    assert r.json()["reloaded"] == "restarting"
    assert calls == [1]


async def _monitor_stub(log, calls):
    calls.append(1)
    await anyio.sleep(3600)


def test_update_check_disabled_starts_no_monitor(tmp_path, monkeypatch):
    # opt-out must mean ZERO monitor execution: no task, no network call
    calls = []
    monkeypatch.setattr(
        server.updates, "monitor", lambda log: _monitor_stub(log, calls)
    )
    cfg = cl.GatewayConfig.model_validate({"update_check": False, "backends": []})
    cl.save(cfg, str(tmp_path / "config.toml"))
    app = server._build_app(
        cfg,
        structlog.get_logger("test"),
        {},
        {},
        {},
        config_path=str(tmp_path / "config.toml"),
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    assert calls == []


def test_update_check_enabled_starts_exactly_one_monitor(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.updates, "monitor", lambda log: _monitor_stub(log, calls)
    )
    # the sleeping stub only ends via the shutdown-grace cancellation; shrink
    # the grace so the test exits fast (no backends need real unwind time)
    monkeypatch.setattr(server, "SHUTDOWN_GRACE", 0.2)
    cfg = cl.GatewayConfig.model_validate({})  # update_check defaults True
    cl.save(cfg, str(tmp_path / "config.toml"))
    app = server._build_app(
        cfg,
        structlog.get_logger("test"),
        {},
        {},
        {},
        config_path=str(tmp_path / "config.toml"),
    )
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
    # exactly one monitor task ran for this lifespan, and shutdown cancelled it
    assert calls == [1]


# --- #15 resource + prompt override endpoints -------------------------------


def _seed_rp_defaults():
    admin.save_defaults(
        {
            "backend": "b",
            "instructions": None,
            "tools": [],
            "resources": [
                {
                    "uri": "file://a.txt",
                    "name": "a-name",
                    "title": None,
                    "description": "a-desc",
                    "mime_type": "text/plain",
                }
            ],
            "resource_templates": [],
            "prompts": [
                {
                    "original": "p1",
                    "title": None,
                    "description": "p-desc",
                    "args": [
                        {"original": "q", "description": "q-desc", "required": False}
                    ],
                }
            ],
        }
    )


def test_resource_override_endpoint_roundtrip(tmp_path):
    _seed_rp_defaults()
    client = TestClient(_admin_app(tmp_path))
    r = client.put(
        "/admin/api/resource-override",
        json={
            "backend": "b",
            "uri": "file://a.txt",
            "override": {"name": "better", "description": "tuned"},
        },
    )
    assert r.status_code == 200 and r.json()["reloaded"] == "in-process"
    state = client.get("/admin/api/state").json()
    (res,) = state["backends"][0]["resources"]
    assert res["name"] == "better" and res["description"] == "tuned"
    r = client.post(
        "/admin/api/resource-reset", json={"backend": "b", "uri": "file://a.txt"}
    )
    assert r.status_code == 200
    state = client.get("/admin/api/state").json()
    (res,) = state["backends"][0]["resources"]
    assert res["name"] is None and res["description"] is None


def test_prompt_override_endpoint_roundtrip_and_validation(tmp_path):
    _seed_rp_defaults()
    client = TestClient(_admin_app(tmp_path))
    r = client.put(
        "/admin/api/prompt-override",
        json={
            "backend": "b",
            "prompt_original": "p1",
            "override": {
                "name": "better_p1",
                "args": [{"original": "q", "description": "tuned"}],
            },
        },
    )
    assert r.status_code == 200
    state = client.get("/admin/api/state").json()
    (p,) = state["backends"][0]["prompts"]
    assert p["name"] == "better_p1" and p["args"][0]["description"] == "tuned"
    # invalid name -> clean 400
    r = client.put(
        "/admin/api/prompt-override",
        json={
            "backend": "b",
            "prompt_original": "p1",
            "override": {"name": "has space"},
        },
    )
    assert r.status_code == 400 and "invalid prompt name" in r.json()["error"]
    r = client.post(
        "/admin/api/prompt-reset", json={"backend": "b", "prompt_original": "p1"}
    )
    assert r.status_code == 200
    state = client.get("/admin/api/state").json()
    assert state["backends"][0]["prompts"][0]["name"] is None


def test_prompt_override_rejects_collision_with_live_catalog(tmp_path):
    from fastmcp.prompts.base import Prompt

    class Provider:
        async def list_prompts(self):
            return [Prompt(name="p1"), Prompt(name="p2"), Prompt(name="new_live")]

    class Proxy:
        providers = [Provider()]

    _seed_rp_defaults()
    client = TestClient(_admin_app(tmp_path, {"b": Proxy()}))
    response = client.put(
        "/admin/api/prompt-override",
        json={
            "backend": "b",
            "prompt_original": "p1",
            "override": {"name": "new_live"},
        },
    )
    assert response.status_code == 400
    assert "already used by prompts" in response.json()["error"]


def test_rp_override_endpoints_reject_unknown_backend(tmp_path):
    client = TestClient(_admin_app(tmp_path))
    r = client.put(
        "/admin/api/resource-override",
        json={"backend": "ghost", "uri": "file://x", "override": {}},
    )
    assert r.status_code == 400
    r = client.put(
        "/admin/api/prompt-override",
        json={"backend": "ghost", "prompt_original": "p", "override": {}},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Audit hardening — instructions type guard and reset/import 400s
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [123, True, [1], {"x": 1}])
def test_put_instructions_rejects_non_string_value(tmp_path, bad):
    # a non-string value must 400 at the boundary, not AttributeError on
    # .encode("utf-8") downstream -> 500
    r = TestClient(_admin_app(tmp_path)).put(
        "/admin/api/instructions", json={"backend": "b", "value": bad}
    )
    assert r.status_code == 400
    assert "string or null" in r.json()["error"]


def test_put_instructions_null_still_clears(tmp_path):
    client = TestClient(_admin_app(tmp_path))
    r = client.put("/admin/api/instructions", json={"backend": "b", "value": "tuned"})
    assert r.status_code == 200
    assert cl.load(_cfg_path(tmp_path)).backends[0].instructions == "tuned"
    r = client.put("/admin/api/instructions", json={"backend": "b", "value": None})
    assert r.status_code == 200
    assert cl.load(_cfg_path(tmp_path)).backends[0].instructions is None


@pytest.mark.parametrize(
    "route",
    ["/admin/api/reset", "/admin/api/resource-reset", "/admin/api/prompt-reset"],
)
def test_reset_routes_missing_fields_are_400(tmp_path, route):
    # bare payload["backend"] / payload["tool_original"] lookups must be a
    # clean 400, not a KeyError -> 500 (the put_* siblings already catch it).
    r = TestClient(_admin_app(tmp_path)).post(route, json={})
    assert r.status_code == 400
    assert r.json()["ok"] is False


@pytest.mark.parametrize("settings", ["foo", [1], 42])
def test_post_import_rejects_non_object_settings(tmp_path, settings):
    # a truthy non-dict "settings" value must 400, not AttributeError on
    # bundle.get("kind") -> 500
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/import", json={"settings": settings}
    )
    assert r.status_code == 400
    assert r.json()["ok"] is False


def test_post_import_valid_bundle_still_applies(tmp_path):
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/import",
        json={"settings": {"backends": {"b": {"instructions": "tuned"}}}},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert cl.load(_cfg_path(tmp_path)).backends[0].instructions == "tuned"
