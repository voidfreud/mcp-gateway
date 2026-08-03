"""Backend lifecycle and topology Admin routes."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_gateway import config_loader as cl
from mcp_gateway.config_loader import Backend, GatewayConfig


class AdminContext(Protocol):
    """The live Admin context surface needed by backend routes."""

    backend_runtime: Any
    hooks: dict[str, Any]
    log: Any
    lock: Any

    def load(self) -> GatewayConfig: ...

    def commit(
        self,
        cfg: GatewayConfig,
        *backends: str,
        validate_virtual_refs: bool = True,
    ) -> None: ...

    def locked(self, handler: Callable[..., Any]) -> Callable[..., Any]: ...

    def restart_response(self, extra: dict) -> JSONResponse: ...


@dataclass(frozen=True)
class BackendRouteDeps:
    """Facade-owned helpers kept dynamic for compatibility and tests."""

    error: Callable[..., JSONResponse]
    needs_json: Callable[[Callable[..., Any]], Callable[..., Any]]
    clean: Callable[[Any], Any]
    name_pattern: re.Pattern[str]
    virtual_route: str
    stable_backend_id: Callable[[Backend], str]
    hot_reload: Callable[..., Any]
    capture_defaults: Callable[[Backend], Awaitable[dict]]
    save_defaults: Callable[[dict], Any]
    defaults_dir: Path
    refresh_timestamps: dict[str, float]
    monotonic: Callable[[], float]


def backend_routes(  # noqa: PLR0915
    ctx: AdminContext, deps_factory: Callable[[], BackendRouteDeps]
) -> list[Route]:
    """Per-backend flags and topology: pin, enable, display name, rename,
    add, and remove."""

    def deps() -> BackendRouteDeps:
        return deps_factory()

    async def pin_backend(request: Request):
        """Toggle per-backend always_load (pin all its tools upfront)."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return deps().error("unknown backend")
        b.always_load = bool(payload.get("value", False))
        ctx.commit(cfg, name)
        ctx.log.info("backend_pin_changed", backend=name, pinned=b.always_load)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def _apply_enabled(b: Backend, value: bool) -> None:
        """Bring one backend's live mount in line with its enabled flag (#78)."""
        if value:
            if b.name not in ctx.backend_runtime.proxies:
                add = ctx.hooks.get("add")
                if add is not None:
                    await add(b)
            else:
                deps().hot_reload(ctx.backend_runtime, ctx.load(), b.name, ctx.log)
        else:
            remove = ctx.hooks.get("remove")
            if remove is not None:
                remove(b.name)

    async def set_stateless(request: Request):
        """Toggle a backend's warm/stateless session strategy (#161)."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return deps().error("unknown backend")
        b.stateless = bool(payload.get("value", False))
        ctx.commit(cfg)
        recycle = ctx.hooks.get("recycle")
        if recycle is not None:
            recycle(name)
        ctx.log.info(
            "backend_session_mode_changed", backend=name, stateless=b.stateless
        )
        return JSONResponse(
            {"ok": True, "reloaded": "recycled", "stateless": b.stateless}
        )

    async def enable_backend(request: Request):
        """Enable/disable one backend and mount or unmount it live (#78)."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return deps().error("unknown backend")
        value = bool(payload.get("value", True))
        b.enabled = value
        ctx.commit(cfg)
        await _apply_enabled(b, value)
        ctx.log.info("backend_enabled_changed", backend=name, enabled=value)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def enable_all(request: Request):
        """Enable or disable every backend, mounting or unmounting each."""
        payload = await request.json()
        value = bool(payload.get("value", True))
        cfg = ctx.load()
        for b in cfg.backends:
            b.enabled = value
        ctx.commit(cfg)
        for b in cfg.backends:
            await _apply_enabled(b, value)
        ctx.log.info(
            "backends_enabled_changed", enabled=value, backend_count=len(cfg.backends)
        )
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def set_display_name(request: Request):
        """Set a backend's display-only name (#42)."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return deps().error("unknown backend")
        try:
            b.display_name = deps().clean(payload.get("value"))
        except cl.ConfigError as exc:
            return deps().error(str(exc))
        ctx.commit(cfg)
        ctx.log.info(
            "backend_display_name_changed", backend=name, display_name=b.display_name
        )
        return JSONResponse({"ok": True})

    async def rename_backend(  # noqa: PLR0911, PLR0912, PLR0915
        request: Request,
    ):
        """Hard-rename a backend while preserving config and live topology."""
        name = request.path_params["name"]
        payload = await request.json()
        value = payload.get("value")
        new_name = value.strip() if isinstance(value, str) else ""
        if not deps().name_pattern.match(new_name):
            return deps().error(
                f"invalid backend name {new_name!r}: use only letters, digits, "
                "'_' or '-' (max 64 chars)"
            )
        if new_name in cl.RESERVED_BACKEND_NAMES:
            return deps().error(f"backend name {new_name!r} is reserved")
        cfg = ctx.load()
        old_backend = next((x for x in cfg.backends if x.name == name), None)
        if old_backend is None:
            return deps().error("unknown backend")
        if any(x.name == new_name for x in cfg.backends):
            return deps().error(
                f"backend name {new_name!r} already exists — pick a different one"
            )
        candidate = cfg.model_copy(deep=True)
        new_backend = next(x for x in candidate.backends if x.name == name)
        new_backend.name = new_name
        try:
            candidate = GatewayConfig.model_validate(candidate.model_dump())
        except Exception as exc:  # noqa: BLE001 - complete candidate validation
            return deps().error(str(exc))
        ctx.commit(candidate, validate_virtual_refs=False)
        old_defaults = deps().defaults_dir / f"{name}.json"
        new_defaults = deps().defaults_dir / f"{new_name}.json"
        old_defaults_data = (
            json.loads(old_defaults.read_text(encoding="utf-8"))
            if old_defaults.is_file()
            else None
        )
        if old_defaults_data is not None:
            data = dict(old_defaults_data)
            data["backend"] = new_name
            deps().save_defaults(data)
            old_defaults.unlink(missing_ok=True)
        base = f"http://{candidate.host}:{candidate.port}"
        response = {
            "backend": new_name,
            "old_endpoint": f"{base}/{name}/mcp",
            "new_endpoint": f"{base}/{new_name}/mcp",
            "old_registration": f"gateway-{name}",
            "new_registration": f"gateway-{new_name}",
        }
        remove = ctx.hooks.get("remove")
        add = ctx.hooks.get("add")
        if remove is not None and add is not None:
            mount_error = None
            try:
                mounted = await add(new_backend)
            except Exception as exc:  # noqa: BLE001 - transaction rolls back below
                mounted = False
                mount_error = f": {type(exc).__name__}: {exc}"
            if mounted:
                remove(name)
                ctx.log.info("backend_renamed", old_backend=name, backend=new_name)
                return JSONResponse({"ok": True, "reloaded": "hot-rename", **response})
            remove(new_name)
            ctx.commit(cfg, validate_virtual_refs=False)
            new_defaults.unlink(missing_ok=True)
            if old_defaults_data is not None:
                deps().save_defaults(old_defaults_data)
            recovery_error = None
            if name not in ctx.backend_runtime.proxies:
                try:
                    if not await add(old_backend):
                        recovery_error = "; old backend re-mount also failed"
                except Exception as exc:  # noqa: BLE001 - report safe rollback state
                    recovery_error = (
                        f"; old backend re-mount failed: {type(exc).__name__}: {exc}"
                    )
            return JSONResponse(
                {
                    "ok": False,
                    "reloaded": "mount-failed-rolled-back",
                    "error": "new backend mount failed; rename rolled back"
                    + (mount_error or "")
                    + (recovery_error or ""),
                    **response,
                },
                status_code=500,
            )
        return ctx.restart_response(response)

    async def add_backend(  # noqa: PLR0911
        request: Request,
    ):
        """Import, introspect, persist, and hot-add a new backend."""
        payload = await request.json()
        if payload.get("name") in cl.RESERVED_BACKEND_NAMES:
            return deps().error(f"backend name {payload.get('name')!r} is reserved")
        if any(b.name == payload.get("name") for b in ctx.load().backends):
            return JSONResponse(
                {"ok": False, "error": "backend name already exists"}, status_code=400
            )
        try:
            b = Backend(
                name=payload["name"],
                transport=payload["transport"],
                url=deps().clean(payload.get("url")),
                command=deps().clean(payload.get("command")),
                args=payload.get("args") or [],
                auth_header=deps().clean(payload.get("auth_header")),
                auth_value=deps().clean(payload.get("auth_value")),
                headers=payload.get("headers") or {},
                auth=deps().clean(payload.get("auth")),
                headers_helper=deps().clean(payload.get("headers_helper")),
                stateless=bool(payload.get("stateless", False)),
                init_timeout=payload.get(
                    "init_timeout", cl.DEFAULT_BACKEND_INIT_TIMEOUT
                ),
                request_timeout=payload.get(
                    "request_timeout", cl.DEFAULT_BACKEND_REQUEST_TIMEOUT
                ),
            )
        except Exception as exc:  # noqa: BLE001 (pydantic/validation)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        b.id = deps().stable_backend_id(b)
        try:
            deps().save_defaults(await deps().capture_defaults(b))
            deps().refresh_timestamps[b.name] = deps().monotonic()
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": f"could not connect to backend: {exc}"},
                status_code=400,
            )
        async with ctx.lock:
            cfg = ctx.load()
            if any(x.name == b.name for x in cfg.backends):
                return JSONResponse(
                    {"ok": False, "error": "backend name already exists"},
                    status_code=400,
                )
            candidate = cfg.model_copy(
                update={"backends": [*cfg.backends, b]}, deep=True
            )
            try:
                candidate = GatewayConfig.model_validate(candidate.model_dump())
            except Exception as exc:  # noqa: BLE001 - complete candidate validation
                return deps().error(str(exc))
            ctx.commit(candidate)
        hot_add = ctx.hooks.get("add")
        if hot_add is not None:
            if await hot_add(b):
                ctx.log.info("backend_added", backend=b.name, reloaded="hot-add")
                return JSONResponse(
                    {"ok": True, "reloaded": "hot-add", "backend": b.name}
                )
            ctx.log.info("backend_added", backend=b.name, reloaded="mount-failed")
            return JSONResponse(
                {"ok": True, "reloaded": "mount-failed", "backend": b.name}
            )
        ctx.log.info("backend_added", backend=b.name, reloaded="restarting")
        return ctx.restart_response({"backend": b.name})

    async def remove_backend(request: Request):
        name = request.path_params["name"]
        cfg = ctx.load()
        backend = next((b for b in cfg.backends if b.name == name), None)
        if backend is None:
            return deps().error("unknown backend")
        backend_id = deps().stable_backend_id(backend)
        referenced_by = [
            tool.name
            for tool in cfg.virtual_tools
            if any(member.backend_id == backend_id for member in tool.members)
        ]
        if referenced_by:
            return deps().error(
                f"backend is referenced by Virtual Tool(s): {', '.join(referenced_by)}"
            )
        before = len(cfg.backends)
        cfg.backends = [b for b in cfg.backends if b.name != name]
        if len(cfg.backends) == before:
            return deps().error("unknown backend")
        ctx.commit(cfg)
        (deps().defaults_dir / f"{name}.json").unlink(missing_ok=True)
        ctx.log.info("backend_removed", backend=name)
        return ctx.restart_response({})

    return [
        Route(
            "/admin/api/backend/{name}/pin",
            deps().needs_json(ctx.locked(pin_backend)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/enabled",
            deps().needs_json(ctx.locked(enable_backend)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/stateless",
            deps().needs_json(ctx.locked(set_stateless)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/enabled",
            deps().needs_json(ctx.locked(enable_all)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/display-name",
            deps().needs_json(ctx.locked(set_display_name)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/rename",
            deps().needs_json(ctx.locked(rename_backend)),
            methods=["POST"],
        ),
        Route("/admin/api/backend", deps().needs_json(add_backend), methods=["POST"]),
        Route(
            "/admin/api/backend/{name}",
            ctx.locked(remove_backend),
            methods=["DELETE"],
        ),
    ]
