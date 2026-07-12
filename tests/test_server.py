"""Tests for the robustness fixes: JSON-body 400 (#48), admin body-size cap
(#49), rotating gateway.log (#50), plus the admin-UX cluster: gateway version
surfacing (#57) and honest dev/foreground restart reporting (#53/#56)."""

from __future__ import annotations

import json
import logging
import os
import plistlib
import re
import subprocess
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

import anyio
import pytest
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


def test_configure_logging_closes_prior_handler(tmp_path):
    """#87: a second setup must close the previous handler (no FD leak) and not
    accumulate handlers on the root logger."""
    server._configure_logging(str(tmp_path / "a.log"))
    prior = logging.getLogger().handlers[0]
    server._configure_logging(str(tmp_path / "b.log"))
    root = logging.getLogger()
    assert len(root.handlers) == 1  # replaced, not accumulated
    assert root.handlers[0] is not prior
    assert prior.stream is None or prior.stream.closed  # prior FD released


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


# ---------------------------------------------------------------------------
# #45 — one-click Claude Code registration via the `claude` CLI
# ---------------------------------------------------------------------------


def _fake_claude_cli(monkeypatch, calls, rc=0, stdout="", stderr=""):
    """Pretend `claude` is on PATH and capture the exact argv it's run with."""
    monkeypatch.setattr(admin.shutil, "which", lambda _cmd: "/usr/bin/claude")

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(admin.subprocess, "run", fake_run)


def test_register_backend_runs_claude_mcp_add(tmp_path, monkeypatch):
    calls = []
    _fake_claude_cli(monkeypatch, calls, stdout="added")
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/register", json={"scope": "user"}
    )
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True and j["exit"] == 0 and j["stdout"] == "added"
    # exact argv: gateway-<name> convention + the real endpoint URL
    assert calls == [
        [
            "claude",
            "mcp",
            "add",
            "--transport",
            "http",
            "--scope",
            "user",
            "gateway-b",
            "http://127.0.0.1:9100/b/mcp",
        ]
    ]
    assert j["command"] == " ".join(calls[0])
    assert "reload" in j["note"] or "restart" in j["note"]


def test_register_backend_defaults_to_local_scope(tmp_path, monkeypatch):
    calls = []
    _fake_claude_cli(monkeypatch, calls)
    r = TestClient(_admin_app(tmp_path)).post("/admin/api/backend/b/register", json={})
    assert r.status_code == 200
    argv = calls[0]
    assert argv[argv.index("--scope") + 1] == "local"


def test_register_cli_failure_is_ok_false_http_200(tmp_path, monkeypatch):
    # the HTTP call succeeded; the CLI failed — surface it, don't 4xx/5xx
    calls = []
    _fake_claude_cli(monkeypatch, calls, rc=1, stderr="No such command")
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/register", json={"scope": "local"}
    )
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is False and j["exit"] == 1
    assert j["stderr"] == "No such command"


def test_register_missing_claude_cli_is_400(tmp_path, monkeypatch):
    monkeypatch.setattr(admin.shutil, "which", lambda _cmd: None)
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/register", json={"scope": "local"}
    )
    assert r.status_code == 400
    assert "claude CLI not found" in r.json()["error"]


def test_register_bad_scope_is_400(tmp_path, monkeypatch):
    calls = []
    _fake_claude_cli(monkeypatch, calls)
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/register", json={"scope": "global"}
    )
    assert r.status_code == 400
    assert calls == []  # rejected before any CLI run


def test_register_unknown_backend_is_400(tmp_path, monkeypatch):
    # register requires the backend to exist so the registered URL is real
    calls = []
    _fake_claude_cli(monkeypatch, calls)
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/ghost/register", json={"scope": "local"}
    )
    assert r.status_code == 400
    assert calls == []


def test_deregister_runs_claude_mcp_remove(tmp_path, monkeypatch):
    calls = []
    _fake_claude_cli(monkeypatch, calls)
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/deregister", json={"scope": "project"}
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls == [["claude", "mcp", "remove", "--scope", "project", "gateway-b"]]


def test_deregister_of_removed_backend_still_runs(tmp_path, monkeypatch):
    # the remove/rename cleanup path: the backend is already gone from config,
    # but its stale Claude Code registration must still be removable
    calls = []
    _fake_claude_cli(monkeypatch, calls)
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/gone/deregister", json={"scope": "local"}
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls == [["claude", "mcp", "remove", "--scope", "local", "gateway-gone"]]


def test_deregister_bad_scope_is_400(tmp_path, monkeypatch):
    calls = []
    _fake_claude_cli(monkeypatch, calls)
    r = TestClient(_admin_app(tmp_path)).post(
        "/admin/api/backend/b/deregister", json={"scope": "everywhere"}
    )
    assert r.status_code == 400
    assert calls == []


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


def test_suppress_list_changed_clears_capability():
    from fastmcp.server import create_proxy

    b = cl.Backend(name="b", transport="stdio", command="/bin/x")
    proxy = create_proxy(cl.to_proxy_config_one(b), name="mcp-gateway-b")
    server._suppress_list_changed(proxy)
    caps = proxy._mcp_server.create_initialization_options().capabilities
    assert caps.tools.listChanged is False
    assert caps.resources.listChanged is False
    assert caps.prompts.listChanged is False


# ---------------------------------------------------------------------------
# #78 — disabled backends are never mounted (endpoint 404s); unmount cleans up
# ---------------------------------------------------------------------------


def test_boot_skips_disabled_backends(tmp_path, monkeypatch):
    monkeypatch.setattr(admin, "DEFAULTS_DIR", tmp_path / "defaults")
    mounted = []

    async def fake_mount(
        app, stack, b, cfg, all_tools, meta, captured, reg, _hold, log, *extra
    ):
        mounted.append(b.name)
        reg[b.name] = object()
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
    server._unmount(app, "b", registry, holders)
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


# ---------------------------------------------------------------------------
# #149 — launchd decoupled from the repo path via the ~/.local/opt symlink
# ---------------------------------------------------------------------------

REPO_ROOT = Path(server.__file__).resolve().parent
SYMLINK_PREFIX = "/.local/opt/mcp-gateway"


def _plist_strings(node):
    """Every string value in a parsed plist, recursively."""
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for v in node.values():
            yield from _plist_strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _plist_strings(v)


def test_plist_paths_all_go_through_stable_symlink():
    # The repo plist must reference the repo ONLY via ~/.local/opt/mcp-gateway
    # (maintained by install.sh) — a hardcoded clone path is exactly the drift
    # that #149 made invisible. No string may mention any real clone location.
    plist = plistlib.loads((REPO_ROOT / "com.void.mcp-gateway.plist").read_bytes())
    strings = list(_plist_strings(plist))
    assert strings  # parsed something

    for s in strings:
        assert "/Developer/projects/" not in s, s
        assert "/Developer/mine/" not in s, s
        # Any string that names a repo file/dir must route through the symlink.
        if "server.py" in s or "config.toml" in s or "/.venv/" in s:
            assert SYMLINK_PREFIX + "/" in s, s

    # The load-bearing keys specifically:
    for arg in plist["ProgramArguments"]:
        assert SYMLINK_PREFIX + "/" in arg, arg
    assert plist["WorkingDirectory"].endswith(SYMLINK_PREFIX)
    assert SYMLINK_PREFIX + "/" in plist["EnvironmentVariables"]["MCP_GATEWAY_CONFIG"]


def test_install_sh_is_executable_and_parses():
    # install.sh maintains the symlink + plist; keep it syntactically valid for
    # macOS's stock /bin/bash 3.2 (bash -n) and executable.
    script = REPO_ROOT / "install.sh"
    assert os.access(script, os.X_OK)
    proc = subprocess.run(
        ["/bin/bash", "-n", str(script)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0, proc.stderr
