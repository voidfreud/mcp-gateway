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
from logging.handlers import RotatingFileHandler
from pathlib import Path

import anyio
import structlog
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware, MiddlewareContext

import admin
import config_loader

CONFIG_PATH = os.environ.get("MCP_GATEWAY_CONFIG", "config.toml")

# Cap admin-API request bodies. Admin payloads are tiny (tool text, backend
# config); anything larger is rejected before it's buffered/parsed. Issue #49.
ADMIN_BODY_LIMIT = 64 * 1024

# On shutdown, how long runners get to unwind gracefully before a backend stuck
# mid-connect is cancelled (it never reaches stop.wait(), so without this the
# daemon could hang forever on shutdown). Issue #61.
SHUTDOWN_GRACE = 5.0


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _configure_logging(log_file: str) -> structlog.BoundLogger:
    """JSON structlog to a rotating *log_file* (created if missing).

    Issue #50: the app's log must not grow unbounded, and neither must launchd's
    ``err.log``/``out.log``. launchd owns those two file descriptors, so nothing
    in-process can rotate them — instead we route ALL stdlib logging (uvicorn,
    fastmcp, everything) into the single rotating ``gateway.log`` handler, so the
    launchd files only ever catch rare pre-init / hard-crash text (and #48 already
    removed the bad-JSON traceback flood that used to fill err.log). A single
    shared handler on the root logger avoids two handlers racing on one file.
    """
    path = Path(log_file).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    # One rotating handler for the whole process: 5 MB × 5 files.
    handler = RotatingFileHandler(
        path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))

    # Root owns the single file handler; every logger propagates into it.
    root = logging.getLogger()
    # Close any handler from a prior call before replacing it, or repeated setup
    # (tests, a future in-process reload) leaks the open file descriptor (#87).
    for h in root.handlers:
        h.close()
    root.handlers = [handler]
    root.setLevel(logging.WARNING)
    # Quiet FastMCP's benign INFO chatter (e.g. "reusing existing session").
    logging.getLogger("fastmcp").setLevel(logging.WARNING)
    # Our own events emit at INFO and propagate up to the root handler (no own
    # handler, so there's no second writer on the file).
    app_logger = logging.getLogger("mcp-gateway")
    app_logger.setLevel(logging.INFO)
    app_logger.handlers = []
    app_logger.propagate = True

    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
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
# Admin request-body size limit (pure-ASGI middleware)
# ---------------------------------------------------------------------------


class BodyLimitMiddleware:
    """Reject oversized admin-API request bodies with 413 before they're parsed.

    Caps the large-body CPU + err.log amplification (issue #49). Only paths under
    ``path_prefix`` are guarded; the per-backend MCP mounts pass straight through.
    Rejects on a declared Content-Length over the cap, and — for chunked or
    length-less bodies — buffers under the cap and rejects once it's exceeded,
    replaying the buffered body to the wrapped app when it fits.

    Note (#81): the whole body is buffered in memory before dispatch even when it
    fits, and on a mid-stream reject we return 413 without draining the rest of
    the in-flight body. Both are fine here — loopback only, 64 KB cap — but would
    need revisiting for a larger cap or a non-loopback bind.
    """

    def __init__(self, app, *, max_bytes: int, path_prefix: str = "/admin/api"):
        self.app = app
        self.max_bytes = max_bytes
        self.path_prefix = path_prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith(
            self.path_prefix
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._reject(send)
                    return
            except ValueError:
                await self._reject(send)
                return

        buffered: list[dict] = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] != "http.request":
                buffered.append(message)
                break
            total += len(message.get("body", b""))
            if total > self.max_bytes:
                await self._reject(send)
                return
            buffered.append(message)
            more = message.get("more_body", False)

        pending = iter(buffered)

        async def replay():
            try:
                return next(pending)
            except StopIteration:
                return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(send):
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"ok": false, "error": "request body too large"}',
            }
        )


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
    # Body starts with "ok" so existing liveness checks still pass; the version
    # tail lets you confirm which build answered (#57).
    return PlainTextResponse(f"ok mcp-gateway {admin.gateway_version()}")


async def _mount_backend(
    app: Starlette,
    stack: AsyncExitStack,
    b: config_loader.Backend,
    cfg: config_loader.GatewayConfig,
    all_tools: dict[str, list[str]],
    captured_meta: dict[str, dict[str, dict]],
    captured_instr: dict[str, str | None],
    registry: dict,
    holders: dict,
    log,
) -> bool:
    """Build ONE backend's proxy, apply its transforms + instructions, run its
    http lifespan, and mount it at ``/<backend>/mcp``. Returns True on success.

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
        transforms, index = config_loader.build_transforms(
            cfg, b, all_tools, captured_meta
        )
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
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("backend_mount_failed", backend=b.name, error=str(exc))
        return False


# ---------------------------------------------------------------------------
# Build + run
# ---------------------------------------------------------------------------


def _build_app(
    cfg: config_loader.GatewayConfig,
    log,
    all_tools: dict,
    captured_meta: dict,
    captured_instr: dict,
    config_path: str = CONFIG_PATH,
) -> Starlette:
    """Assemble the parent Starlette app: /health, admin, and a lifespan that
    runs one **runner task per backend**, each owning that backend's
    AsyncExitStack for its whole life.

    Per-task ownership is load-bearing: the anyio cancel scopes inside a
    backend's client/session-manager lifespans must be entered and exited by
    the SAME task in LIFO order. A runner enters its stack, parks on the
    shared ``stop`` event, and unwinds in itself on graceful shutdown — so a
    request handler can never touch the scopes directly. Hot-add (#7) just
    starts one more runner in the same task group and awaits its mount result
    via ``tg.start``; the import endpoint responds when the backend is live.
    Boot starts all runners concurrently and yields immediately (#61): the app
    is ready as soon as admin/health are, and each endpoint appears the moment
    its backend connects — boot cost ≈ the slowest backend, not the sum.
    """
    # Populated in the lifespan; the admin closes over both (by reference) so its
    # hot-reload can target the right backend's live proxy + transform holder.
    registry: dict = {}
    holders: dict = {}
    hooks: dict = {}  # lifespan installs hooks["add"] (hot-add) for the admin

    @asynccontextmanager
    async def lifespan(app: Starlette):
        stop = anyio.Event()  # set on shutdown -> every runner unwinds itself

        async def runner(
            b,
            cfg_,
            all_tools_,
            meta_,
            captured_,
            *,
            task_status=anyio.TASK_STATUS_IGNORED,
        ):
            async with AsyncExitStack() as stack:
                ok = await _mount_backend(
                    app,
                    stack,
                    b,
                    cfg_,
                    all_tools_,
                    meta_,
                    captured_,
                    registry,
                    holders,
                    log,
                )
                task_status.started(ok)
                if ok:
                    await stop.wait()

        async with anyio.create_task_group() as tg:
            # #61: start every backend's runner CONCURRENTLY and don't block
            # readiness on any of them — boot ≈ the slowest backend instead of
            # the sum, and a slow/hung backend delays only its own endpoint
            # (each runner appends its Mount when ready, same path as hot-add).
            for b in cfg.backends:
                tg.start_soon(runner, b, cfg, all_tools, captured_meta, captured_instr)

            async def hot_add(b: config_loader.Backend) -> bool:
                """Mount a just-imported backend live (#7). Config is already
                saved; a fresh load picks the new backend's transforms up."""
                cfg2 = config_loader.load(config_path)
                return await tg.start(
                    runner,
                    b,
                    cfg2,
                    admin.all_tools_from_defaults(cfg2),
                    admin.all_meta_from_defaults(cfg2),
                    admin.captured_instructions(cfg2),
                )

            hooks["add"] = hot_add
            log.info(
                "gateway_starting",
                backends=[b.name for b in cfg.backends],
                endpoints=[
                    f"http://{cfg.host}:{cfg.port}/{b.name}/mcp" for b in cfg.backends
                ],
                admin=f"http://{cfg.host}:{cfg.port}/admin",
            )
            try:
                yield
            finally:
                hooks.pop("add", None)
                stop.set()  # graceful: runners exit their stacks, tg drains
                # A runner stuck mid-connect never reaches stop.wait(); without a
                # deadline it would hang shutdown forever. Grace, then cancel.
                tg.cancel_scope.deadline = anyio.current_time() + SHUTDOWN_GRACE

    # Admin + health are static (no backend connection needed); the per-backend
    # MCP mounts are added during the lifespan (they need connected clients).
    parent = Starlette(
        routes=[Route("/health", _health, methods=["GET"])],
        lifespan=lifespan,
        middleware=[
            StarletteMiddleware(BodyLimitMiddleware, max_bytes=ADMIN_BODY_LIMIT)
        ],
    )
    admin.register(parent, config_path, log, registry, holders, hooks)
    return parent


async def _run(cfg: config_loader.GatewayConfig, log) -> None:
    # Capture each backend's tools + server instructions (no-op if already
    # cached) so transforms (incl. per-backend always_load) and per-endpoint
    # instructions can be built from the captured baseline.
    await admin.ensure_defaults(cfg, log)
    all_tools = admin.all_tools_from_defaults(cfg)
    captured_meta = admin.all_meta_from_defaults(cfg)
    captured_instr = admin.captured_instructions(cfg)

    parent = _build_app(cfg, log, all_tools, captured_meta, captured_instr)

    config = uvicorn.Config(
        parent,
        host=cfg.host,
        port=cfg.port,
        log_level="warning",
        # log_config=None → uvicorn installs no stderr handlers of its own, so
        # its loggers propagate to our root rotating handler instead of launchd's
        # err.log (issue #50).
        log_config=None,
        # #88: uvicorn must wait LONGER than the gateway's own runner-unwind
        # deadline (SHUTDOWN_GRACE) or it force-cancels the lifespan mid-unwind
        # and can briefly orphan a backend's stdio child. +2s covers the unwind
        # that happens after a hung runner is cancelled at the deadline.
        timeout_graceful_shutdown=int(SHUTDOWN_GRACE) + 2,
    )
    await uvicorn.Server(config).serve()


def _load_config_or_recover(log) -> config_loader.GatewayConfig:
    """Load the config; on ANY failure recover from the most recent VALID backup.

    A malformed/invalid ``config.toml`` would otherwise crash-loop the daemon
    under launchd (KeepAlive, no throttle) and flood err.log with a traceback
    every ~10s (#96). Instead we fall back to the newest good snapshot in
    ``admin.BACKUP_DIR`` so the daemon keeps running on the last known-good
    config while the operator fixes the file. Re-raises only if NO backup loads —
    ``main`` then logs one clean line and exits (still a clean one-liner per
    respawn, not a traceback flood). First-run seeding is preserved because
    ``load`` calls ``ensure_config`` before parsing.
    """
    try:
        return config_loader.load(CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001 — any bad config triggers recovery
        log.error("config_load_failed", path=CONFIG_PATH, error=str(exc))
        for backup in sorted(admin.BACKUP_DIR.glob("config-*.toml"), reverse=True):
            try:
                cfg = config_loader.load(str(backup))
            except Exception:  # noqa: BLE001 — skip a bad backup, try an older one
                continue
            log.warning(
                "config_recovered_from_backup",
                backup=str(backup),
                hint="config.toml is invalid — running on the last good backup; "
                "fix it and restart",
            )
            return cfg
        raise


def main() -> None:
    # Configure logging BEFORE loading config, so a load failure is a clean
    # structured line in gateway.log rather than a raw traceback in launchd's
    # err.log (#96). Start on the default log path; re-point if the loaded cfg
    # names a different one (the prior handler is closed — #87).
    default_log = config_loader.GatewayConfig.model_fields["log_file"].default
    log = _configure_logging(default_log)
    try:
        cfg = _load_config_or_recover(log)
    except Exception as exc:  # noqa: BLE001
        log.error(
            "config_unrecoverable",
            error=str(exc),
            hint="no valid backup found — fix config.toml, then restart",
        )
        raise SystemExit(1) from None
    if cfg.log_file != default_log:
        log = _configure_logging(cfg.log_file)
    anyio.run(_run, cfg, log)


if __name__ == "__main__":
    main()
