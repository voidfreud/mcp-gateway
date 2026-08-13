"""Virtual Tool admin routes, isolated from the Admin composition root."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_gateway import config_loader as cl
from mcp_gateway import virtual_tools as virtual_mod
from mcp_gateway.config_loader import GatewayConfig


class AdminContext(Protocol):
    """The live Admin context surface needed by Virtual Tool routes."""

    backend_runtime: Any
    hooks: dict[str, Any]
    log: Any

    def load(self) -> GatewayConfig: ...

    def commit(
        self, cfg: GatewayConfig, *, validate_virtual_refs: bool = True
    ) -> None: ...

    def locked(self, handler: Callable[..., Any]) -> Callable[..., Any]: ...


@dataclass(frozen=True)
class VirtualRouteDeps:
    """Facade-owned collaborators kept dynamic for compatibility and tests."""

    load_defaults: Callable[[str], dict | None]
    find_tool_override: Callable[..., Any]
    error: Callable[..., JSONResponse]
    needs_json: Callable[[Callable[..., Any]], Callable[..., Any]]


def virtual_routes(  # noqa: PLR0915
    ctx: AdminContext, deps_factory: Callable[[], VirtualRouteDeps]
) -> list[Route]:
    """First-class Virtual Tool catalog, draft lifecycle, testing and activation."""

    def deps() -> VirtualRouteDeps:
        return deps_factory()

    def _definition(tool: cl.VirtualTool) -> dict:
        return tool.model_dump(mode="json", exclude_none=True)

    def _find(cfg: GatewayConfig, name: str) -> cl.VirtualTool | None:
        return next((tool for tool in cfg.virtual_tools if tool.name == name), None)

    def _clean_virtual_payload(payload: dict) -> dict:
        """Discard client-authored consent receipts; only activation may mint one."""
        cleaned = dict(payload)
        cleaned.pop("consent_fingerprint", None)
        cleaned.pop("egress_consent_fingerprint", None)
        if isinstance(cleaned.get("router"), dict):
            cleaned["router"] = dict(cleaned["router"])
            cleaned["router"].pop("consent_fingerprint", None)
            cleaned["router"].pop("egress_consent_fingerprint", None)
        return cleaned

    def _consent_fingerprint(tool: cl.VirtualTool) -> str | None:
        """Fingerprint exactly the administrator-visible OpenRouter egress shape."""
        if tool.dispatch != "llm" or tool.router is None:
            return None
        return cl.llm_egress_consent_fingerprint(tool)

    async def _resolution(tool: cl.VirtualTool, cfg: GatewayConfig) -> dict:
        return await virtual_mod.resolve_tool(tool, cfg, ctx.backend_runtime.proxies)

    def _hot_replace(cfg: GatewayConfig) -> None:
        server = ctx.hooks.get("virtual_server")
        if server is None:
            raise cl.ConfigError("the shared /virtual/mcp endpoint is not mounted")
        virtual_mod.replace_tools(
            server,
            cfg,
            ctx.load,
            ctx.backend_runtime.proxies,
            ctx.log,
            ctx.hooks.setdefault("virtual_status", {}),
        )

    async def list_virtual_tools(_request: Request):
        cfg = ctx.load()
        resolutions = await asyncio.gather(
            *(_resolution(tool, cfg) for tool in cfg.virtual_tools)
        )
        statuses = ctx.hooks.setdefault("virtual_status", {})
        listed = []
        for tool, resolution in zip(cfg.virtual_tools, resolutions, strict=True):
            definition = _definition(tool)
            definition["members"] = [
                {**member, "resolution": resolved}
                for member, resolved in zip(
                    definition["members"], resolution["members"], strict=True
                )
            ]
            listed.append(
                {
                    **definition,
                    # #286: virtual tools have no captured default — the
                    # definition text IS the effective description. Stored
                    # limit (None = inherit), effective cap (tool > gateway;
                    # None = unbounded), and its UTF-8 byte size.
                    "description_max_bytes": tool.description_max_bytes,
                    "effective_description_max_bytes": (
                        cl.effective_virtual_description_limit(cfg, tool)
                    ),
                    "description_bytes": cl.utf8_byte_len(tool.description),
                    "resolution": resolution,
                    **statuses.get(tool.name, {}),
                }
            )
        return JSONResponse(
            {
                "mounted": "virtual_server" in ctx.hooks,
                "endpoint": "/virtual/mcp",
                "tools": listed,
            }
        )

    async def virtual_catalog(_request: Request):
        cfg = ctx.load()
        backends = []
        for backend in cfg.backends:
            defaults = deps().load_defaults(backend.name) or {}
            tools = []
            for source in defaults.get("tools", []):
                override = deps().find_tool_override(backend, source["original"])
                effective = (
                    override.name if override and override.name else source["original"]
                )
                params = []
                overrides = {
                    item.original: item
                    for item in (override.params if override else [])
                }
                for param in source.get("params", []):
                    changed = overrides.get(param["original"])
                    params.append(
                        {
                            "original": param["original"],
                            "effective_name": (
                                changed.name
                                if changed is not None and changed.name
                                else param["original"]
                            ),
                            "description": param.get("description"),
                            "required": param.get("required", False),
                            "hidden": changed.hide if changed else False,
                        }
                    )
                tools.append(
                    {
                        "original": source["original"],
                        "effective_name": effective,
                        "description": (
                            override.description
                            if override and override.description is not None
                            else source.get("description")
                        ),
                        "enabled": backend.enabled
                        and (override.enabled if override else True),
                        "params": params,
                    }
                )
            backends.append(
                {
                    "id": virtual_mod.stable_backend_id(backend),
                    "name": backend.name,
                    "effective_name": backend.display_name or backend.name,
                    "enabled": backend.enabled,
                    "tools": tools,
                }
            )
        return JSONResponse({"backends": backends})

    async def create_virtual(request: Request):
        payload = _clean_virtual_payload(await request.json())
        payload = {**payload, "enabled": False}
        try:
            tool = cl.VirtualTool.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - Pydantic/config validation
            return deps().error(str(exc))
        cfg = ctx.load()
        if _find(cfg, tool.name) is not None:
            return deps().error("virtual tool name already exists")
        virtual_mod.ensure_backend_ids(cfg)
        try:
            candidate = cfg.model_copy(
                update={"virtual_tools": [*cfg.virtual_tools, tool]}, deep=True
            )
            candidate = GatewayConfig.model_validate(candidate.model_dump())
            virtual_mod.build_virtual_tool(
                tool, candidate, ctx.backend_runtime.proxies, ctx.log
            )
        except Exception as exc:  # noqa: BLE001 - dry build
            return deps().error(str(exc))
        ctx.commit(candidate)
        return JSONResponse(
            {"ok": True, "tool": _definition(tool), "lifecycle": "draft"},
            status_code=201,
        )

    async def update_virtual(request: Request):  # noqa: PLR0911 - validation/rollback exits
        name = request.path_params["name"]
        payload = _clean_virtual_payload(await request.json())
        cfg = ctx.load()
        previous = _find(cfg, name)
        if previous is None:
            return deps().error("unknown virtual tool", 404)
        # PUT always creates/replaces an inactive draft. An active revision is
        # removed from the shared endpoint after persistence; activation is the
        # only operation that can put the edited definition back into service.
        payload = {**payload, "enabled": False}
        try:
            tool = cl.VirtualTool.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            return deps().error(str(exc))
        if tool.name != name and _find(cfg, tool.name) is not None:
            return deps().error("virtual tool name already exists")
        virtual_mod.ensure_backend_ids(cfg)
        tools = [tool if item.name == name else item for item in cfg.virtual_tools]
        try:
            candidate = GatewayConfig.model_validate(
                cfg.model_copy(update={"virtual_tools": tools}, deep=True).model_dump()
            )
            virtual_mod.build_virtual_tool(
                tool, candidate, ctx.backend_runtime.proxies, ctx.log
            )
        except Exception as exc:  # noqa: BLE001
            return deps().error(str(exc))
        ctx.commit(candidate)
        if previous.enabled:
            try:
                _hot_replace(candidate)
            except Exception as exc:  # noqa: BLE001
                ctx.commit(cfg, validate_virtual_refs=False)
                _hot_replace(cfg)
                return deps().error(
                    f"hot reload failed; previous definition restored: {exc}", 500
                )
        return JSONResponse(
            {"ok": True, "tool": _definition(tool), "lifecycle": "draft"}
        )

    async def validate_virtual(request: Request):
        cfg = ctx.load()
        tool = _find(cfg, request.path_params["name"])
        if tool is None:
            return deps().error("unknown virtual tool", 404)
        resolution = await _resolution(tool, cfg)
        return JSONResponse(resolution, status_code=200 if resolution["ok"] else 400)

    async def test_virtual(request: Request):
        cfg = ctx.load()
        tool = _find(cfg, request.path_params["name"])
        if tool is None:
            return deps().error("unknown virtual tool", 404)
        payload = await request.json()
        resolution = await _resolution(tool, cfg)
        if not resolution["ok"]:
            return JSONResponse(resolution, status_code=400)
        started = time.perf_counter()
        try:
            result = await virtual_mod.run_virtual(
                tool,
                payload.get("arguments") or {},
                cfg,
                ctx.backend_runtime.proxies,
                ctx.log,
            )
            receipt = result.model_dump(mode="json", by_alias=True)
            status = {
                "last_test": {
                    "ok": not result.is_error,
                    "status": "passed" if not result.is_error else "failed",
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                }
            }
            ctx.hooks.setdefault("virtual_status", {})[tool.name] = status
            return JSONResponse(
                {"ok": not result.is_error, "result": receipt, **status}
            )
        except Exception as exc:  # noqa: BLE001
            status = {"last_test": {"ok": False, "status": "failed", "error": str(exc)}}
            ctx.hooks.setdefault("virtual_status", {})[tool.name] = status
            return deps().error(str(exc))

    async def activate_virtual(request: Request):
        cfg = ctx.load()
        name = request.path_params["name"]
        tool = _find(cfg, name)
        if tool is None:
            return deps().error("unknown virtual tool", 404)
        fingerprint = _consent_fingerprint(tool)
        consent_store = ctx.hooks.setdefault("virtual_consent_fingerprints", {})
        if fingerprint is None:
            consent_store.pop(name, None)
        else:
            consent_store[name] = fingerprint
        ctx.hooks.setdefault("virtual_status", {}).setdefault(name, {})[
            "consent_fingerprint"
        ] = fingerprint
        resolution = await _resolution(tool, cfg)
        if not resolution["ok"]:
            return JSONResponse(resolution, status_code=400)
        candidate = cfg.model_copy(deep=True)
        active = _find(candidate, name)
        assert active is not None
        active.enabled = True
        if active.router is not None:
            active.router.egress_consent_fingerprint = fingerprint
        try:
            candidate = GatewayConfig.model_validate(candidate.model_dump())
            virtual_mod.build_virtual_tool(
                active, candidate, ctx.backend_runtime.proxies, ctx.log
            )
        except Exception as exc:  # noqa: BLE001
            return deps().error(str(exc))
        # Full live resolution immediately above is stronger than the captured
        # baseline guard used for synchronous legacy mutations.
        ctx.commit(candidate, validate_virtual_refs=False)
        try:
            _hot_replace(candidate)
        except Exception as exc:  # noqa: BLE001
            ctx.commit(cfg, validate_virtual_refs=False)
            _hot_replace(cfg)
            return deps().error(f"activation failed; draft restored: {exc}", 500)
        return JSONResponse({"ok": True, "enabled": True, "reloaded": "hot"})

    async def disable_virtual(request: Request):
        cfg = ctx.load()
        tool = _find(cfg, request.path_params["name"])
        if tool is None:
            return deps().error("unknown virtual tool", 404)
        candidate = cfg.model_copy(deep=True)
        disabled = _find(candidate, tool.name)
        assert disabled is not None
        disabled.enabled = False
        candidate = GatewayConfig.model_validate(candidate.model_dump())
        ctx.commit(candidate)
        try:
            _hot_replace(candidate)
        except Exception as exc:  # noqa: BLE001
            ctx.commit(cfg, validate_virtual_refs=False)
            _hot_replace(cfg)
            return deps().error(
                f"disable failed; active definition restored: {exc}", 500
            )
        return JSONResponse({"ok": True, "enabled": False, "reloaded": "hot"})

    async def delete_virtual(request: Request):
        cfg = ctx.load()
        name = request.path_params["name"]
        if _find(cfg, name) is None:
            return deps().error("unknown virtual tool", 404)
        candidate = cfg.model_copy(
            update={
                "virtual_tools": [
                    tool for tool in cfg.virtual_tools if tool.name != name
                ]
            },
            deep=True,
        )
        candidate = GatewayConfig.model_validate(candidate.model_dump())
        ctx.commit(candidate)
        try:
            _hot_replace(candidate)
        except Exception as exc:  # noqa: BLE001
            ctx.commit(cfg, validate_virtual_refs=False)
            _hot_replace(cfg)
            return deps().error(f"delete failed; definition restored: {exc}", 500)
        return JSONResponse({"ok": True})

    return [
        Route("/admin/api/virtual-tools", list_virtual_tools, methods=["GET"]),
        Route("/admin/api/virtual-catalog", virtual_catalog, methods=["GET"]),
        Route(
            "/admin/api/virtual-tools",
            deps().needs_json(ctx.locked(create_virtual)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}",
            deps().needs_json(ctx.locked(update_virtual)),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}",
            ctx.locked(delete_virtual),
            methods=["DELETE"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}/validate",
            validate_virtual,
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}/test",
            deps().needs_json(test_virtual),
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}/activate",
            ctx.locked(activate_virtual),
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}/disable",
            ctx.locked(disable_virtual),
            methods=["POST"],
        ),
    ]
