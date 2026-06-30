"""mcp-gateway: a thin FastMCP proxy that rewrites every broadcast text.

One persistent loopback HTTP daemon, shared by all Claude Code sessions. Each
backend MCP server is proxied **on its own endpoint** (``/<backend>/mcp``) under
one Starlette app, alongside the admin UI (``/admin``) and a health check
(``/health``). Per backend the gateway rewrites the tool name, title,
description, and every parameter name/description, while forwarding the actual
tool calls untouched.

Why one endpoint per backend (issue #29): Claude Code truncates each MCP
server's ``instructions`` at ~2KB. A single aggregating proxy gave every backend
ONE shared 2KB budget; a separate endpoint per backend means each backend is its
own MCP server in Claude Code with its OWN 2KB budget, and its own session
strategy (a down backend only fails its own endpoint — issue #9). See README.md.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import anyio
import structlog
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware, MiddlewareContext

import admin
import config_loader

CONFIG_PATH = os.environ.get("MCP_GATEWAY_CONFIG", "config.toml")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_logging(log_file: str) -> structlog.BoundLogger:
    """JSON structlog to *log_file* (created if missing)."""
    # Quiet FastMCP's own INFO chatter (e.g. the benign "reusing existing
    # session" proxy line) so the daemon's launchd out.log stays readable.
    # Our structured events go through structlog below, not this logger.
    logging.getLogger("fastmcp").setLevel(logging.WARNING)

    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a", encoding="utf-8")
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.WriteLoggerFactory(file=fh),
    )
    return structlog.get_logger("mcp-gateway")


# ---------------------------------------------------------------------------
# Tool-call logging middleware
# ---------------------------------------------------------------------------


class CallLogMiddleware(Middleware):
    """Log every tool call and its outcome/latency to the structured log."""

    def __init__(self, log: structlog.BoundLogger, backend: str) -> None:
        self._log = log
        self._backend = backend

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        msg = getattr(context, "message", None)
        tool = getattr(msg, "name", "?")
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:  # noqa: BLE001 - log and re-raise
            self._log.error(
                "tool_call_error",
                backend=self._backend,
                tool=tool,
                error=str(exc),
                error_type=type(exc).__name__,
                ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        self._log.info(
            "tool_call",
            backend=self._backend,
            tool=tool,
            ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return result


# ---------------------------------------------------------------------------
# Startup reconciliation: warn on configured tools that no backend exposes
# ---------------------------------------------------------------------------


async def _reconcile(proxy: FastMCP, index: dict[str, str], log) -> None:
    """List one backend proxy's live tools; warn on transform keys with no match.

    Best-effort and non-fatal: a network/backend hiccup here must not stop the
    daemon from starting. A mismatch almost always means a typo in a tool's
    ``original`` in config.toml.
    """
    try:
        async with Client(proxy) as client:
            live = {t.name for t in await client.list_tools()}
    except Exception as exc:  # noqa: BLE001
        log.warning("reconcile_skipped", reason=str(exc))
        return
    for key, backend in index.items():
        if key not in live:
            log.warning(
                "override_no_match",
                tool=key,
                backend=backend,
                hint="check 'original' in config.toml; backend may not expose it",
            )
    log.info("reconcile_done", live_tools=len(live), overrides=len(index))


# ---------------------------------------------------------------------------
# Per-backend proxy build + mount
# ---------------------------------------------------------------------------


async def _health(_request: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


async def _mount_backend(
    app: Starlette,
    stack: AsyncExitStack,
    b: config_loader.Backend,
    cfg: config_loader.GatewayConfig,
    all_tools: dict[str, list[str]],
    captured_instr: dict[str, str | None],
    registry: dict,
    holders: dict,
    log,
) -> None:
    """Build ONE backend's proxy, apply its transforms + instructions, run its
    http lifespan, and mount it at ``/<backend>/mcp``.

    ``stateless=false`` backends are built from a **persistent connected Client**
    (entered on *stack*, so one warm backend session is reused for the daemon's
    lifetime — no per-call stdio respawn); ``stateless=true`` backends use a
    fresh per-request session. Best-effort: a backend that fails to connect is
    skipped (its endpoint is simply absent) so one down backend never blocks the
    rest of the gateway from booting (issue #9).
    """
    name = f"mcp-gateway-{b.name}"
    try:
        if not b.stateless:
            # Warm: one connected client reused for every call (issues #8/#9).
            client = await stack.enter_async_context(
                Client(config_loader.to_proxy_config_one(b))
            )
            proxy = create_proxy(client, name=name)
        else:
            proxy = create_proxy(config_loader.to_proxy_config_one(b), name=name)

        proxy.add_middleware(CallLogMiddleware(log, b.name))
        transforms, index = config_loader.build_transforms(cfg, b, all_tools)
        await _reconcile(proxy, index, log)  # SOURCE names, before renames apply
        proxy.add_transform(transforms)
        # Per-endpoint instructions: only this backend's blurb -> its own 2KB.
        proxy.instructions = config_loader.backend_instructions(b, captured_instr)

        sub = proxy.http_app(path="/mcp")
        # Starlette only runs the TOP app's lifespan, so run each mounted app's
        # session-manager lifespan ourselves; it stays open for the daemon.
        await stack.enter_async_context(sub.lifespan(sub))
        app.router.routes.append(Mount(f"/{b.name}", app=sub))

        registry[b.name] = proxy
        holders[b.name] = [transforms]
        log.info(
            "backend_mounted",
            backend=b.name,
            path=f"/{b.name}/mcp",
            persistent=not b.stateless,
            tools=len(index),
            instructions_chars=len(proxy.instructions or ""),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("backend_mount_failed", backend=b.name, error=str(exc))


# ---------------------------------------------------------------------------
# Build + run
# ---------------------------------------------------------------------------


async def _run(cfg: config_loader.GatewayConfig, log) -> None:
    # Capture each backend's tools + server instructions (no-op if already
    # cached) so transforms (incl. per-backend always_load) and per-endpoint
    # instructions can be built from the captured baseline.
    await admin.ensure_defaults(cfg, log)
    all_tools = admin.all_tools_from_defaults(cfg)
    captured_instr = admin.captured_instructions(cfg)

    # Populated in the lifespan; the admin closes over both (by reference) so its
    # hot-reload can target the right backend's live proxy + transform holder.
    registry: dict = {}
    holders: dict = {}

    @asynccontextmanager
    async def lifespan(app: Starlette):
        async with AsyncExitStack() as stack:
            for b in cfg.backends:
                await _mount_backend(
                    app,
                    stack,
                    b,
                    cfg,
                    all_tools,
                    captured_instr,
                    registry,
                    holders,
                    log,
                )
            log.info(
                "gateway_starting",
                backends=list(registry),
                endpoints=[f"http://{cfg.host}:{cfg.port}/{n}/mcp" for n in registry],
                admin=f"http://{cfg.host}:{cfg.port}/admin",
            )
            yield

    # Admin + health are static (no backend connection needed); the per-backend
    # MCP mounts are added during the lifespan (they need connected clients).
    parent = Starlette(
        routes=[Route("/health", _health, methods=["GET"])], lifespan=lifespan
    )
    admin.register(parent, CONFIG_PATH, log, registry, holders)

    config = uvicorn.Config(
        parent,
        host=cfg.host,
        port=cfg.port,
        log_level="warning",
        timeout_graceful_shutdown=2,
    )
    await uvicorn.Server(config).serve()


def main() -> None:
    cfg = config_loader.load(CONFIG_PATH)
    log = _configure_logging(cfg.log_file)
    anyio.run(_run, cfg, log)


if __name__ == "__main__":
    main()
