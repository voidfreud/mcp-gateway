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
    """Construct the proxy. Returns (mcp, cfg, log, transforms, index).

    The transform is intentionally NOT applied here — the caller reconciles
    against the live *source* tool names first (so a renamed tool isn't flagged
    as missing), then applies it.
    """
    cfg = config_loader.load(CONFIG_PATH)
    log = _configure_logging(cfg.log_file)

    proxy_config = config_loader.to_proxy_config(cfg)
    transforms, index = config_loader.build_transforms(cfg)

    mcp = create_proxy(proxy_config, name="mcp-gateway")
    mcp.add_middleware(CallLogMiddleware(log))

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    log.info(
        "gateway_built",
        backends=[b.name for b in cfg.backends],
        overrides=len(index),
        host=cfg.host,
        port=cfg.port,
    )
    return mcp, cfg, log, transforms, index


def main() -> None:
    import anyio

    mcp, cfg, log, transforms, index = build()

    # Pre-flight against SOURCE names (before the transform renames them).
    anyio.run(_reconcile, mcp, index, log)

    # Now apply the rewrites.
    mcp.add_transform(transforms)

    log.info("gateway_starting", url=f"http://{cfg.host}:{cfg.port}/mcp")
    mcp.run(transport="http", host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
