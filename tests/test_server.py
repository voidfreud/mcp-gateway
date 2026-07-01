"""Tests for the robustness fixes: JSON-body 400 (#48), admin body-size cap
(#49), rotating gateway.log (#50), plus the admin-UX cluster: gateway version
surfacing (#57) and honest dev/foreground restart reporting (#53/#56)."""

from __future__ import annotations

import json
import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

import structlog

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import admin
import config_loader as cl
import server


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


def test_body_limit_ignores_non_admin_paths():
    client = TestClient(_limited_app(1024))
    r = client.post("/other", content=b"x" * 4096)
    assert r.status_code == 200
    assert r.json()["len"] == 4096


# ---------------------------------------------------------------------------
# #50 — gateway.log is a rotating handler, JSON format unchanged
# ---------------------------------------------------------------------------


def test_configure_logging_rotates_and_keeps_json(tmp_path):
    log_path = tmp_path / "gateway.log"
    log = server._configure_logging(str(log_path))

    # The single rotating handler lives on the ROOT logger (issue #50), so every
    # logger — ours + uvicorn/fastmcp — flows into the one rotating file instead
    # of launchd's err.log. mcp-gateway has no own handler; it propagates.
    root = logging.getLogger()
    assert len(root.handlers) == 1
    handler = root.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes > 0 and handler.backupCount > 0
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


# ---------------------------------------------------------------------------
# #57 — gateway version surfaced (single source) in /health and the admin state
# ---------------------------------------------------------------------------


def test_gateway_version_matches_pyproject():
    text = (admin.HERE / "pyproject.toml").read_text(encoding="utf-8")
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


# ---------------------------------------------------------------------------
# #53/#56 — restart is only claimed when we're actually launchd-managed
# ---------------------------------------------------------------------------


def test_under_launchd_false_in_test_process():
    # pytest is never the launchd-managed daemon, so this is False — which is what
    # makes add/remove/restart report "dev-no-restart" instead of a stuck spinner.
    assert admin.under_launchd() is False


def _admin_app(tmp_path: Path) -> Starlette:
    cfg = cl.GatewayConfig.model_validate(
        {"backends": [{"name": "b", "transport": "stdio", "command": "/bin/x"}]}
    )
    path = tmp_path / "config.toml"
    cl.save(cfg, str(path))
    app = Starlette()
    # structlog logger: the app logs with kwargs (e.g. log.info("x", backend=...)),
    # which a stdlib logger rejects.
    admin.register(app, str(path), structlog.get_logger("test"), {}, {})
    return app


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


def test_display_name_route_sets_and_clears(tmp_path):
    client = TestClient(_admin_app(tmp_path))
    r1 = client.post("/admin/api/backend/b/display-name", json={"value": "Nice Label"})
    assert r1.status_code == 200
    assert cl.load(_cfg_path(tmp_path)).backends[0].display_name == "Nice Label"
    # blank clears back to None (falls back to the canonical name)
    r2 = client.post("/admin/api/backend/b/display-name", json={"value": "   "})
    assert r2.status_code == 200
    assert cl.load(_cfg_path(tmp_path)).backends[0].display_name is None
