"""mcp-gateway: a thin FastMCP proxy that rewrites every broadcast text.

One persistent loopback HTTP daemon, shared by all Claude Code sessions. It
proxies one or more backend MCP servers and rewrites the tool name, title,
description, and every parameter name/description that the backend broadcasts —
while forwarding the actual tool calls untouched. See README.md and
config.toml.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import structlog
from starlette.requests import Request
from starlette.responses import PlainTextResponse

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
# Tool-call logging middleware (High requirement #7)
# ---------------------------------------------------------------------------


class CallLogMiddleware(Middleware):
    """Log every tool call and its outcome/latency to the structured log."""

    def __init__(self, log: structlog.BoundLogger) -> None:
        self._log = log

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        msg = getattr(context, "message", None)
        tool = getattr(msg, "name", "?")
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:  # noqa: BLE001 - log and re-raise
            self._log.error(
                "tool_call_error",
                tool=tool,
                error=str(exc),
                error_type=type(exc).__name__,
                ms=round((time.perf_counter() - started) * 1000, 2),
            )
            raise
        self._log.info(
            "tool_call",
            tool=tool,
            ms=round((time.perf_counter() - started) * 1000, 2),
        )
        return result


# ---------------------------------------------------------------------------
# Startup reconciliation: warn on configured tools that no backend exposes
# ---------------------------------------------------------------------------


async def _reconcile(mcp: FastMCP, index: dict[str, str], log) -> None:
    """List live tools through the proxy; warn on transform keys with no match.

    Best-effort and non-fatal: a network/backend hiccup here must not stop the
    daemon from starting (KeepAlive would just thrash). A mismatch almost
    always means a typo in a tool's ``original`` in config.toml.
    """
    try:
        async with Client(mcp) as client:
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
# Build + run
# ---------------------------------------------------------------------------


def build():
    """Load config + logging and decide the session strategy. Returns
    (cfg, log, proxy_cfg, persistent). The proxy is assembled in `_assemble`:
    a persistent backend must be created from an already-connected Client
    (see `main`), so it cannot be constructed here."""
    cfg = config_loader.load(CONFIG_PATH)
    log = _configure_logging(cfg.log_file)
    proxy_cfg = config_loader.to_proxy_config(cfg)
    # stateless=false → keep ONE warm backend session for the daemon's lifetime
    # instead of FastMCP's default per-request session, which over HTTP spawns a
    # fresh stdio subprocess on every call (the gitnexus respawn — issue #8).
    persistent = any(not b.stateless for b in cfg.backends)
    return cfg, log, proxy_cfg, persistent


def _assemble(target, cfg, log):
    """create_proxy(target) + middleware + health. `target` is the config dict
    (fresh per-request sessions) or an already-connected Client (one reused
    session for every call). Transforms are applied later in `_startup`."""
    mcp = create_proxy(target, name="mcp-gateway")
    mcp.add_middleware(CallLogMiddleware(log))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    log.info(
        "gateway_built",
        backends=[b.name for b in cfg.backends],
        host=cfg.host,
        port=cfg.port,
        persistent=any(not b.stateless for b in cfg.backends),
    )
    return mcp


async def _startup(mcp, cfg, log, holder: list) -> None:
    """Capture defaults, build transforms (with the tool list so a per-backend
    always_load can pin un-overridden tools), reconcile against source names,
    then apply the transform."""
    await admin.ensure_defaults(cfg, log)
    all_tools = admin.all_tools_from_defaults(cfg)
    transforms, index = config_loader.build_transforms(cfg, all_tools)
    await _reconcile(mcp, index, log)  # pre-flight: SOURCE names, before renames
    mcp.add_transform(transforms)
    holder.append(transforms)
    # Compose + set the gateway's server-level instructions from the backends'
    # captured originals (the proxy would otherwise drop them). Read per initialize.
    composed = admin.apply_instructions(mcp, cfg)
    log.info("instructions_set", chars=len(composed) if composed else 0)


async def _serve(mcp, cfg, log, holder: list) -> None:
    """Startup (defaults → transforms → reconcile), register admin, run http."""
    await _startup(mcp, cfg, log, holder)
    admin.register(mcp, CONFIG_PATH, log, holder)
    log.info(
        "gateway_starting",
        mcp=f"http://{cfg.host}:{cfg.port}/mcp",
        admin=f"http://{cfg.host}:{cfg.port}/admin",
    )
    await mcp.run_async(transport="http", host=cfg.host, port=cfg.port)


def main() -> None:
    import anyio

    cfg, log, proxy_cfg, persistent = build()

    async def _run() -> None:
        holder: list = []
        if persistent:
            # One backend client, connected for the whole daemon lifetime, so a
            # stateless=false backend (gitnexus) is spawned once and reused
            # across every call instead of cold-starting per request.
            async with Client(proxy_cfg) as backend_client:
                mcp = _assemble(backend_client, cfg, log)
                await _serve(mcp, cfg, log, holder)
        else:
            mcp = _assemble(proxy_cfg, cfg, log)
            await _serve(mcp, cfg, log, holder)

    anyio.run(_run)


if __name__ == "__main__":
    main()
