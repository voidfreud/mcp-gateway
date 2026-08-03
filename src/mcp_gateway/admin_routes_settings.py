"""Settings and override Admin routes, isolated from the composition root."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_gateway import config_loader as cl
from mcp_gateway.config_loader import GatewayConfig


class AdminContext(Protocol):
    """The live Admin context surface needed by settings routes."""

    def load(self) -> GatewayConfig: ...

    log: Any
    lock: Any

    def commit(
        self,
        cfg: GatewayConfig,
        *backends: str,
        validate_virtual_refs: bool = True,
    ) -> None: ...

    def locked(self, handler: Callable[..., Any]) -> Callable[..., Any]: ...

    async def live_prompt_names(self, name: str) -> set[str] | None: ...


@dataclass(frozen=True)
class SettingsRouteDeps:
    """Facade-owned mutation helpers kept dynamic for compatibility and tests."""

    error: Callable[..., JSONResponse]
    needs_json: Callable[[Callable[..., Any]], Callable[..., Any]]
    apply_tool_override: Callable[..., Any]
    apply_resource_override: Callable[..., Any]
    apply_prompt_override: Callable[..., Any]
    set_instructions: Callable[..., Any]
    migrate_override: Callable[..., Any]
    import_settings: Callable[..., Any]


def settings_routes(  # noqa: PLR0915
    ctx: AdminContext, deps_factory: Callable[[], SettingsRouteDeps]
) -> list[Route]:
    """Tool/resource/prompt overrides, instructions, and settings import."""

    def deps() -> SettingsRouteDeps:
        return deps_factory()

    async def put_override(request: Request):
        payload = await request.json()
        cfg = ctx.load()
        try:
            uniquified = deps().apply_tool_override(cfg, payload["backend"], payload)
        except (cl.ConfigError, KeyError) as exc:
            return deps().error(str(exc))
        ctx.commit(cfg, payload["backend"])
        ctx.log.info(
            "tool_override_saved",
            backend=payload["backend"],
            tool=payload.get("tool_original"),
        )
        out: dict = {"ok": True, "reloaded": "in-process"}
        if uniquified is not None:
            # #22: the opt-in uniquify stored a suffixed name — hand the final
            # name back so the UI can reflect what actually shipped.
            out.update({"name": uniquified, "uniquified": True})
        return JSONResponse(out)

    async def reset_tool(request: Request):
        """Clear all overrides for one tool (revert to the backend default)."""
        payload = await request.json()
        cfg = ctx.load()
        try:
            b = next((x for x in cfg.backends if x.name == payload["backend"]), None)
            if b is None:
                return deps().error("unknown backend")
            b.tools = [t for t in b.tools if t.original != payload["tool_original"]]
        except (cl.ConfigError, KeyError) as exc:
            return deps().error(str(exc))
        ctx.commit(cfg, payload["backend"])
        ctx.log.info(
            "tool_override_reset",
            backend=payload["backend"],
            tool=payload.get("tool_original"),
        )
        return JSONResponse({"ok": True})

    async def put_resource_override(request: Request):
        """#15: upsert one resource/template override (keyed by uri)."""
        payload = await request.json()
        cfg = ctx.load()
        try:
            deps().apply_resource_override(cfg, payload["backend"], payload)
        except (cl.ConfigError, KeyError) as exc:
            return deps().error(str(exc))
        ctx.commit(cfg, payload["backend"])
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def reset_resource(request: Request):
        """#15: clear all overrides for one resource (revert to default)."""
        payload = await request.json()
        cfg = ctx.load()
        try:
            b = next((x for x in cfg.backends if x.name == payload["backend"]), None)
            if b is None:
                return deps().error("unknown backend")
            b.resources = [r for r in b.resources if r.uri != payload["uri"]]
        except (cl.ConfigError, KeyError) as exc:
            return deps().error(str(exc))
        ctx.commit(cfg, payload["backend"])
        return JSONResponse({"ok": True})

    async def put_prompt_override(request: Request):
        """#15: upsert one prompt's override (rename, text, args, enabled)."""
        payload = await request.json()
        try:
            backend = payload["backend"]
            override = payload.get("override", {})
            live_names = (
                await ctx.live_prompt_names(backend) if "name" in override else None
            )
            async with ctx.lock:
                cfg = ctx.load()
                deps().apply_prompt_override(
                    cfg, backend, payload, live_prompt_names=live_names
                )
                ctx.commit(cfg, backend)
        except (cl.ConfigError, KeyError) as exc:
            return deps().error(str(exc))
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def reset_prompt(request: Request):
        """#15: clear all overrides for one prompt (revert to default)."""
        payload = await request.json()
        cfg = ctx.load()
        try:
            b = next((x for x in cfg.backends if x.name == payload["backend"]), None)
            if b is None:
                return deps().error("unknown backend")
            b.prompts = [
                p for p in b.prompts if p.original != payload["prompt_original"]
            ]
        except (cl.ConfigError, KeyError) as exc:
            return deps().error(str(exc))
        ctx.commit(cfg, payload["backend"])
        return JSONResponse({"ok": True})

    async def put_instructions(request: Request):
        """Set a per-backend server-instructions override (``backend`` = name).
        Hot-reloads that backend's endpoint — it only changes the blurb Claude
        reads at initialize, no connection rebuild."""
        payload = await request.json()
        cfg = ctx.load()
        backend = payload.get("backend")
        try:
            deps().set_instructions(cfg, backend, payload.get("value"))
        except (cl.ConfigError, KeyError) as exc:
            return deps().error(str(exc))
        ctx.commit(cfg, backend)  # set_instructions validated the name
        ctx.log.info("backend_instructions_changed", backend=backend)
        return JSONResponse({"ok": True})

    async def migrate_override_route(request: Request):
        """#153: carry a dangling override's tuned text onto the tool's new
        original, then drop the old entry. Hot-reloads that backend."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        try:
            result = deps().migrate_override(
                cfg, name, payload.get("from"), payload.get("to")
            )
        except (cl.ConfigError, KeyError) as exc:
            return deps().error(str(exc))
        ctx.commit(cfg, name)
        return JSONResponse({"ok": True, "reloaded": "in-process", **result})

    async def discard_override_route(request: Request):
        """#153: drop a dangling override entry (its tuned text no longer
        applies). Same removal as /reset — the intent is different (clearing a
        stale entry, not reverting a live tool)."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return deps().error("unknown backend")
        original = payload.get("original")
        before = len(b.tools)
        b.tools = [t for t in b.tools if t.original != original]
        if len(b.tools) == before:
            return deps().error(f"no stored override for {original!r}")
        ctx.commit(cfg, name)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def post_import(request: Request):
        """Atomic settings import (#136): validate the whole bundle against a
        fresh cfg; persist and hot-reload only if EVERY item passes."""
        payload = await request.json()
        bundle = payload.get("settings") or payload
        if not isinstance(bundle, dict):
            return deps().error("settings must be a JSON object")
        mode = payload.get("mode", "merge")
        cfg = ctx.load()
        affected, errors = deps().import_settings(cfg, bundle, mode)
        if errors:
            return JSONResponse(
                {"ok": False, "errors": errors, "applied": False}, status_code=400
            )
        # disabled backends: stored, effective on enable (commit skips unmounted)
        ctx.commit(cfg, *affected)
        return JSONResponse({"ok": True, "backends": affected, "mode": mode})

    return [
        Route(
            "/admin/api/override",
            deps().needs_json(ctx.locked(put_override)),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/reset",
            deps().needs_json(ctx.locked(reset_tool)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/resource-override",
            deps().needs_json(ctx.locked(put_resource_override)),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/resource-reset",
            deps().needs_json(ctx.locked(reset_resource)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/prompt-override",
            deps().needs_json(put_prompt_override),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/prompt-reset",
            deps().needs_json(ctx.locked(reset_prompt)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/instructions",
            deps().needs_json(ctx.locked(put_instructions)),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/import",
            deps().needs_json(ctx.locked(post_import)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/migrate-override",
            deps().needs_json(ctx.locked(migrate_override_route)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/discard-override",
            deps().needs_json(ctx.locked(discard_override_route)),
            methods=["POST"],
        ),
    ]
