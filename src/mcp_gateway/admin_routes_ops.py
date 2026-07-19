"""Operational Admin routes, isolated from the Admin composition root."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from fastmcp import Client
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_gateway.config_loader import Backend, GatewayConfig


class AdminContext(Protocol):
    """The live Admin context surface needed by operational routes."""

    backend_runtime: Any
    hooks: dict[str, Any]
    log: Any

    def load(self) -> GatewayConfig: ...

    def restart_response(self, extra: dict) -> JSONResponse: ...


@dataclass(frozen=True)
class OpsRouteDeps:
    """Facade-owned collaborators kept dynamic for compatibility and tests."""

    error: Callable[..., JSONResponse]
    needs_json: Callable[[Callable[..., Any]], Callable[..., Any]]
    refresh: Callable[..., Any]
    status_timeout: float


def ops_routes(  # noqa: PLR0915
    ctx: AdminContext, deps_factory: Callable[[], OpsRouteDeps]
) -> list[Route]:
    """Operational endpoints: mini-inspector, manual restart, re-introspect,
    liveness status (#23), and the dashboard-load refresh sweep (#43)."""

    def deps() -> OpsRouteDeps:
        return deps_factory()

    async def restart_gateway(_request: Request):
        """Manual on-demand restart of the daemon (#56). Same launchd-gated
        semantics as a topology change: restarts when managed, honest no-op in
        dev/foreground."""
        ctx.log.info("gateway_restart_requested", source="admin")
        return ctx.restart_response({})

    async def run_tool(request: Request):  # noqa: PLR0911 — one early return per input-validation failure
        """Mini-Inspector (#3): execute one tool through the LIVE proxy — the
        same path Claude uses, so renames/transforms apply and reverse-map —
        and return structured + unstructured content + error state. Read-only
        w.r.t. config, so no lock; call_tool_mcp doesn't raise on a
        tool-level error (isError comes back in the payload)."""
        payload = await request.json()
        backend = payload.get("backend")
        proxy = ctx.backend_runtime.get_proxy(backend)
        if proxy is None:
            return JSONResponse(
                {"ok": False, "error": "backend not mounted"}, status_code=400
            )
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool:
            return JSONResponse(
                {"ok": False, "error": "missing or invalid tool (must be a string)"},
                status_code=400,
            )
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            return JSONResponse(
                {"ok": False, "error": "args must be an object"}, status_code=400
            )
        started = time.perf_counter()
        try:
            async with Client(proxy) as c:
                res = await c.call_tool_mcp(tool, args, timeout=60)
        except Exception as exc:  # noqa: BLE001 — surface, don't 500
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=502,
            )
        return JSONResponse(
            {
                "ok": True,
                "is_error": bool(res.isError),
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "content": [
                    blk.model_dump(mode="json", exclude_none=True)
                    for blk in (res.content or [])
                ],
                "structured": res.structuredContent,
            }
        )

    async def reintrospect(request: Request):
        """Manual Re-inspect: force a re-capture (bypasses the #43 throttle)
        and hot-reload so pins/enabled reconcile with the fresh tool list.
        Unlike the auto triggers, a failure here is surfaced (502), not just
        logged — the user explicitly asked and deserves the answer."""
        name = request.path_params["name"]
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return deps().error("unknown backend")
        res = await deps().refresh(ctx, b, force=True)
        if res["status"] == "error":
            return deps().error(f"introspection failed: {res['error']}", status=502)
        ctx.log.info(
            "backend_reintrospected", backend=name, changed=res.get("changed", False)
        )
        return JSONResponse({"ok": True, **res})

    async def get_status(_request: Request):
        """#23: per-backend liveness — one concurrent probe per backend through
        its LIVE mounted proxy (the same path Claude's list_tools takes), each
        bounded by STATUS_TIMEOUT so a hung backend marks itself, not the UI."""

        async def one(b: Backend) -> tuple[str, dict]:
            if not b.enabled:
                return b.name, {"state": "disabled"}
            proxy = ctx.backend_runtime.get_proxy(b.name)
            if proxy is None:
                return b.name, {"state": "unmounted"}
            started = time.perf_counter()
            try:
                async with asyncio.timeout(deps().status_timeout):
                    async with Client(proxy) as c:
                        tools = await c.list_tools()
                return b.name, {
                    "state": "ok",
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                    "tools": len(tools),
                }
            except Exception as exc:  # noqa: BLE001 — the error IS the status
                err = f"{type(exc).__name__}: {exc}"
                # #161: a WARM backend that probes `error` may have a dead session
                # fastmcp won't heal — recycle it best-effort (cooldown-debounced
                # in the hook). Stateless backends spin a fresh session per probe,
                # so there's nothing to recycle.
                if not b.stateless:
                    recycle = ctx.hooks.get("recycle")
                    if recycle is not None:
                        recycle(b.name)
                return b.name, {"state": "error", "error": err}

        cfg = ctx.load()
        results = await asyncio.gather(*(one(b) for b in cfg.backends))
        return JSONResponse({"backends": dict(results)})

    async def refresh_all(_request: Request):
        """#43 dashboard-load trigger: throttled re-introspect of every
        enabled+mounted backend, concurrently and per-backend isolated — a
        down/slow backend reports itself and never stalls the others."""

        async def one(b: Backend) -> tuple[str, dict]:
            if not b.enabled or b.name not in ctx.backend_runtime.proxies:
                return b.name, {"status": "skipped"}
            return b.name, await deps().refresh(ctx, b)

        cfg = ctx.load()
        results = await asyncio.gather(*(one(b) for b in cfg.backends))
        return JSONResponse({"ok": True, "backends": dict(results)})

    return [
        Route("/admin/api/run", deps().needs_json(run_tool), methods=["POST"]),
        Route("/admin/api/restart", restart_gateway, methods=["POST"]),
        Route("/admin/api/introspect/{name}", reintrospect, methods=["POST"]),
        Route("/admin/api/status", get_status, methods=["GET"]),
        Route("/admin/api/refresh", refresh_all, methods=["POST"]),
    ]
