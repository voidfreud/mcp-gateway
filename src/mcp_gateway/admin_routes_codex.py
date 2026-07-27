"""Codex registration routes, isolated from the admin composition root."""

from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_gateway import admin_cli
from mcp_gateway import config_loader as cl
from mcp_gateway.config_loader import GatewayConfig


class AdminContext(Protocol):
    """The read-only Admin context surface needed by Codex registration."""

    def load(self) -> GatewayConfig: ...

    log: Any


@dataclass(frozen=True)
class CodexRouteDeps:
    """Facade-owned collaborators kept dynamic for compatibility and tests."""

    cli_path: Callable[[], str | None]
    command: Callable[..., list[str]]
    bearer_env_var: Callable[[str | None], str | None]
    parse_registrations: Callable[[str, list[str]], dict[str, bool]]
    error: Callable[..., JSONResponse]
    needs_json: Callable[[Callable[..., Any]], Callable[..., Any]]
    cache: MutableMapping[str, Any]
    cache_ttl: float
    monotonic: Callable[[], float]
    subprocess_run: Callable[..., Any]
    cli_timeout: float
    virtual_route: str = "virtual"


def codex_routes(  # noqa: PLR0915 - grouped registration endpoints share one policy
    ctx: AdminContext, deps_factory: Callable[[], CodexRouteDeps]
) -> list[Route]:
    """Build independent-Codex MCP registration routes for backends and Virtual Tools.

    ``deps_factory`` intentionally resolves facade globals for every request.
    The historic ``mcp_gateway.admin`` module remains monkeypatchable by tests
    and external integrations, while this module has no import cycle back to it.
    """

    async def _cli_raw(argv: list[str]) -> tuple[int, str, str]:
        deps = deps_factory()
        return await admin_cli.run_cli(deps.subprocess_run, argv, deps.cli_timeout)

    def _invalidate_registrations_cache() -> None:
        """Drop the cached ``codex mcp list`` output after a successful
        mutation, so the next non-fresh registrations GET re-runs the CLI."""
        deps = deps_factory()
        deps.cache["output"] = None
        deps.cache["ts"] = 0.0

    def _binary() -> str | JSONResponse:
        deps = deps_factory()
        binary = deps.cli_path()
        if binary is None:
            return deps.error(
                "Codex CLI not found — install Codex/ChatGPT desktop or expose "
                "the codex executable to the gateway daemon, then retry"
            )
        return binary

    async def _register(name: str, endpoint: str):
        cfg = ctx.load()
        binary = _binary()
        if isinstance(binary, JSONResponse):
            return binary
        url = f"http://{cfg.host}:{cfg.port}/{endpoint}/mcp"
        deps = deps_factory()
        try:
            bearer_env_var = deps.bearer_env_var(cfg.bearer_token)
            argv = deps.command("add", name, url=url, bearer_env_var=bearer_env_var)
        except cl.ConfigError as exc:
            return deps.error(str(exc))
        argv[0] = binary
        ctx.log.info("codex_registration_changed", backend=name, action="add")
        rc, stdout, stderr = await _cli_raw(argv)
        if rc == 0:
            _invalidate_registrations_cache()
        return JSONResponse(
            {
                "ok": rc == 0,
                "exit": rc,
                "stdout": stdout,
                "stderr": stderr,
                "command": " ".join(argv),
                "note": "Restart Codex or open a new task to load the server",
            }
        )

    async def register_backend(request: Request):
        name = request.path_params["name"]
        cfg = ctx.load()
        if not any(backend.name == name for backend in cfg.backends):
            return deps_factory().error("unknown backend")
        return await _register(name, name)

    async def register_virtual(request: Request):
        del request
        deps = deps_factory()
        return await _register(deps.virtual_route, deps.virtual_route)

    async def deregister_backend(request: Request):
        name = request.path_params["name"]
        return await _deregister(name)

    async def _deregister(name: str):
        binary = _binary()
        if isinstance(binary, JSONResponse):
            return binary
        deps = deps_factory()
        argv = deps.command("remove", name)
        argv[0] = binary
        ctx.log.info("codex_registration_changed", backend=name, action="remove")
        rc, stdout, stderr = await _cli_raw(argv)
        if rc == 0:
            _invalidate_registrations_cache()
        return JSONResponse(
            {
                "ok": rc == 0,
                "exit": rc,
                "stdout": stdout,
                "stderr": stderr,
                "command": " ".join(argv),
                "note": "Restart Codex or open a new task to unload the server",
            }
        )

    async def deregister_virtual(request: Request):
        del request
        deps = deps_factory()
        return await _deregister(deps.virtual_route)

    async def registrations(request: Request):
        binary = _binary()
        if isinstance(binary, JSONResponse):
            return JSONResponse({"available": False})
        deps = deps_factory()
        fresh = request.query_params.get("fresh") in ("1", "true")
        now = deps.monotonic()
        output = deps.cache.get("output")
        if fresh or output is None or now - deps.cache["ts"] > deps.cache_ttl:
            rc, stdout, stderr = await _cli_raw([binary, "mcp", "list", "--json"])
            if rc != 0:
                return JSONResponse(
                    {
                        "available": True,
                        "ok": False,
                        "error": stderr or stdout or f"codex mcp list exited {rc}",
                    }
                )
            output = stdout
            deps.cache["output"] = output
            deps.cache["ts"] = now
        try:
            registered = deps.parse_registrations(
                output,
                [backend.name for backend in ctx.load().backends]
                + [deps.virtual_route],
            )
        except cl.ConfigError as exc:
            return JSONResponse({"available": True, "ok": False, "error": str(exc)})
        return JSONResponse({"available": True, "ok": True, "registered": registered})

    return [
        Route(
            "/admin/api/backend/{name}/codex/register",
            deps_factory().needs_json(register_backend),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/codex/deregister",
            deps_factory().needs_json(deregister_backend),
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual/codex/register",
            deps_factory().needs_json(register_virtual),
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual/codex/deregister",
            deps_factory().needs_json(deregister_virtual),
            methods=["POST"],
        ),
        Route("/admin/api/codex-registrations", registrations, methods=["GET"]),
    ]
