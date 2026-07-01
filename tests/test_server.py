"""Tests for the robustness fixes: JSON-body 400 (#48), admin body-size cap
(#49), and rotating gateway.log (#50)."""

from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

import admin
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

    stdlib = logging.getLogger("mcp-gateway")
    assert len(stdlib.handlers) == 1
    handler = stdlib.handlers[0]
    assert isinstance(handler, RotatingFileHandler)
    assert handler.maxBytes > 0 and handler.backupCount > 0

    log.info("hello_event", n=7)
    for h in stdlib.handlers:
        h.flush()

    line = log_path.read_text(encoding="utf-8").strip().splitlines()[-1]
    rec = json.loads(line)  # must still be valid JSON
    assert rec["event"] == "hello_event"
    assert rec["n"] == 7
    assert rec["level"] == "info"
