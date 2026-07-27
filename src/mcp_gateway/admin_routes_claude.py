"""Claude Code registration routes, isolated from the Admin composition root."""

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
    """The read-only Admin context surface needed by Claude routes."""

    def load(self) -> GatewayConfig: ...

    log: Any


@dataclass(frozen=True)
class ClaudeRouteDeps:
    """Facade-owned collaborators kept dynamic for compatibility and tests."""

    cli_path: Callable[[], str | None]
    command: Callable[..., list[str]]
    parse_registrations: Callable[[str, list[str]], dict[str, bool]]
    error: Callable[..., JSONResponse]
    needs_json: Callable[[Callable[..., Any]], Callable[..., Any]]
    cache: MutableMapping[str, Any]
    cache_ttl: float
    monotonic: Callable[[], float]
    subprocess_run: Callable[..., Any]
    cli_timeout: float


def claude_routes(  # noqa: PLR0915 - nested route handlers stay cohesive
    ctx: AdminContext, deps_factory: Callable[[], ClaudeRouteDeps]
) -> list[Route]:
    """Build Claude Code registration routes.

    ``deps_factory`` resolves facade globals for every request. The historic
    ``mcp_gateway.admin`` module remains monkeypatchable by tests and external
    integrations, while this module has no import cycle back to it.
    """

    async def _cli_raw(argv: list[str]) -> tuple[int, str, str]:
        deps = deps_factory()
        return await admin_cli.run_cli(deps.subprocess_run, argv, deps.cli_timeout)

    def _invalidate_registrations_cache() -> None:
        """Drop the cached ``claude mcp list`` output after a successful
        mutation, so the next non-fresh registrations GET re-runs the CLI."""
        deps = deps_factory()
        deps.cache["output"] = None
        deps.cache["ts"] = 0.0

    async def _run_cli(argv: list[str], redact: str | None = None) -> JSONResponse:
        rc, stdout, stderr = await _cli_raw(argv)
        if rc == 0:
            _invalidate_registrations_cache()

        def _hide(value: str) -> str:
            return value.replace(redact, "***") if redact else value

        return JSONResponse(
            {
                "ok": rc == 0,
                "exit": rc,
                "stdout": _hide(stdout),
                "stderr": _hide(stderr),
                "command": _hide(" ".join(argv)),
                "note": "Claude Code may need a reload/restart to pick up the change",
            }
        )

    def _missing_cli() -> JSONResponse | None:
        deps = deps_factory()
        if deps.cli_path() is None:
            return deps.error(
                "claude CLI not found on the daemon's PATH — install Claude "
                "Code (or expose `claude` to the daemon's environment), then "
                "retry"
            )
        return None

    async def register_backend(request: Request):
        """``claude mcp add`` for one backend as ``gateway-<name>``."""
        name = request.path_params["name"]
        payload = await request.json()
        scope = payload.get("scope") or "local"
        cfg = ctx.load()
        if not any(backend.name == name for backend in cfg.backends):
            return deps_factory().error("unknown backend")
        missing = _missing_cli()
        if missing is not None:
            return missing
        url = f"http://{cfg.host}:{cfg.port}/{name}/mcp"
        deps = deps_factory()
        try:
            token = (
                cl.expand_env_required(cfg.bearer_token, "bearer_token")
                if cfg.bearer_token
                else None
            )
            argv = deps.command("add", name, url=url, scope=scope, bearer_token=token)
        except cl.ConfigError as exc:
            return deps.error(str(exc))
        ctx.log.info(
            "claude_registration_changed", backend=name, action="add", scope=scope
        )
        return await _run_cli(argv, redact=token)

    async def deregister_backend(request: Request):
        """``claude mcp remove`` for ``gateway-<name>``."""
        name = request.path_params["name"]
        payload = await request.json()
        scope = payload.get("scope") or "local"
        missing = _missing_cli()
        if missing is not None:
            return missing
        deps = deps_factory()
        try:
            argv = deps.command("remove", name, scope=scope)
        except cl.ConfigError as exc:
            return deps.error(str(exc))
        ctx.log.info(
            "claude_registration_changed", backend=name, action="remove", scope=scope
        )
        return await _run_cli(argv)

    async def cc_registrations(_request: Request):
        """Return cached registration state from ``claude mcp list``."""
        deps = deps_factory()
        if deps.cli_path() is None:
            return JSONResponse({"available": False})
        fresh = _request.query_params.get("fresh") in ("1", "true")
        now = deps.monotonic()
        output = deps.cache.get("output")
        if fresh or output is None or now - deps.cache["ts"] > deps.cache_ttl:
            rc, stdout, stderr = await admin_cli.run_cli(
                deps.subprocess_run, ["claude", "mcp", "list"], deps.cli_timeout
            )
            output = (stdout or "") + (stderr or "") if rc >= 0 else ""
            deps.cache["output"] = output
            deps.cache["ts"] = now
        names = [backend.name for backend in ctx.load().backends]
        return JSONResponse(
            {"available": True, "registered": deps.parse_registrations(output, names)}
        )

    async def reregister_all(request: Request):
        """Re-point every enabled backend at this gateway in Claude Code."""
        payload = await request.json()
        scope = payload.get("scope") or "local"
        cfg = ctx.load()
        missing = _missing_cli()
        if missing is not None:
            return missing
        deps = deps_factory()
        try:
            token = (
                cl.expand_env_required(cfg.bearer_token, "bearer_token")
                if cfg.bearer_token
                else None
            )
        except cl.ConfigError as exc:
            return deps.error(str(exc))

        def _hide(value: str) -> str:
            return value.replace(token, "***") if token else value

        results: list[dict] = []
        for backend in cfg.backends:
            if not backend.enabled:
                continue
            url = f"http://{cfg.host}:{cfg.port}/{backend.name}/mcp"
            try:
                remove_argv = deps.command("remove", backend.name, scope=scope)
                add_argv = deps.command(
                    "add",
                    backend.name,
                    url=url,
                    scope=scope,
                    bearer_token=token,
                )
            except cl.ConfigError as exc:
                results.append(
                    {"backend": backend.name, "ok": False, "error": str(exc)}
                )
                continue
            remove_rc, _remove_out, remove_err = await _cli_raw(remove_argv)
            rc, _output, error = await _cli_raw(add_argv)
            entry = {
                "backend": backend.name,
                "ok": rc == 0,
                "exit": rc,
                "stderr": _hide(error),
            }
            if remove_rc != 0 and rc != 0:
                # Remove errors are intentionally tolerated (idempotent
                # remove-then-add), but when the add ALSO fails the remove
                # diagnostic is the likely root cause — don't lose it.
                entry["remove_error"] = _hide(remove_err)
            results.append(entry)
        ok_count = sum(1 for result in results if result["ok"])
        if ok_count:
            _invalidate_registrations_cache()
        return JSONResponse(
            {
                "ok": all(result["ok"] for result in results),
                "count": len(results),
                "ok_count": ok_count,
                "backends": results,
                "note": "Claude Code may need a reload/restart to pick up the change",
            }
        )

    return [
        Route(
            "/admin/api/cc-reregister-all",
            deps_factory().needs_json(reregister_all),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/register",
            deps_factory().needs_json(register_backend),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/deregister",
            deps_factory().needs_json(deregister_backend),
            methods=["POST"],
        ),
        Route("/admin/api/cc-registrations", cc_registrations, methods=["GET"]),
    ]
