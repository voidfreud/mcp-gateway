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

import hmac
import logging
import os
import sys
import time
from contextlib import AsyncExitStack, asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

import anyio
import structlog
import uvicorn
from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.server import create_proxy
from fastmcp.server.middleware import Middleware, MiddlewareContext
from mcp.server.lowlevel.server import NotificationOptions
from starlette.applications import Starlette
from starlette.middleware import Middleware as StarletteMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

from mcp_gateway import admin, config_loader


def default_config_path() -> str:
    """Where the live config lives, in precedence order: the MCP_GATEWAY_CONFIG
    env var (the launchd install sets it); a ./config.toml in the working
    directory (a repo checkout / dev run); else the XDG-style user path
    ~/.config/mcp-gateway/config.toml — auto-seeded on first run, so a
    `uv tool install` + `mcp-gateway` run works out of the box."""
    env = os.environ.get("MCP_GATEWAY_CONFIG")
    if env:
        return env
    if Path("config.toml").is_file():
        return "config.toml"
    return str(Path("~/.config/mcp-gateway/config.toml").expanduser())


CONFIG_PATH = default_config_path()

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
# tools/list_changed subscription (#43)
# ---------------------------------------------------------------------------


class _AutoRefresh:
    """The #43 auto-refresh plumbing, bundled so the lifespan stays readable:
    a bounded queue + single worker for ``tools/list_changed`` pushes (a push
    must never block the backend session's message pump), the post-mount
    refresh, and the opt-in scheduled sweep. ``close()`` ends both tasks."""

    def __init__(  # noqa: PLR0913 — same lifespan plumbing as _mount_backend
        self, interval: int, config_path: str, registry: dict, holders: dict, log
    ) -> None:
        self.interval = interval
        self._config_path = config_path
        self._registry = registry
        self._holders = holders
        self._log = log
        self.shutdown = anyio.Event()
        self.send, self._recv = anyio.create_memory_object_stream(16)

    async def refresh(self, b, throttle=None):
        """Re-capture one backend's baseline + hot-reload on change (#43).
        Never raises — auto-refresh is best-effort background work."""
        try:
            await admin.refresh_and_reload(
                b,
                self._config_path,
                self._registry,
                self._holders,
                self._log,
                throttle=admin.REFRESH_THROTTLE if throttle is None else throttle,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("auto_refresh_error", backend=b.name, error=str(exc))

    async def worker(self) -> None:
        async for b in self._recv:  # ends when self.send closes
            await self.refresh(b, admin.LIST_CHANGED_THROTTLE)

    async def interval_loop(self) -> None:
        # trigger 4 — scheduled sweep, OFF by default (introspect_interval = 0);
        # a pure safety net for a backend that neither reconnects nor declares
        # tools.listChanged.
        while True:
            with anyio.move_on_after(self.interval):
                await self.shutdown.wait()
            if self.shutdown.is_set():
                return
            live = config_loader.load(self._config_path)
            for b in live.backends:
                if b.enabled and b.name in self._registry:
                    await self.refresh(b)

    def close(self) -> None:
        self.shutdown.set()
        self.send.close()


class _ListChangedHandler(MessageHandler):
    """Enqueue a backend re-introspection when it pushes
    ``notifications/tools/list_changed`` (#43).

    Only wired for STATEFUL backends — stateless ones use a fresh session per
    request, so there is no persistent connection to receive pushes (their
    refresh comes from the other triggers). Passing a ``message_handler``
    replaces FastMCP's default ``TaskNotificationHandler`` on this client;
    fine here — the gateway never starts SEP-1686 background tasks on
    backends. The handler must never block this session's message pump (live
    tool calls share it), so it only ENQUEUES; the lifespan's refresh worker
    does the actual seconds-long re-capture.
    """

    def __init__(self, backend: config_loader.Backend, send_nowait, log) -> None:
        super().__init__()
        self._backend = backend
        self._send_nowait = send_nowait
        self._log = log

    async def on_tool_list_changed(self, _notification) -> None:
        try:
            self._send_nowait(self._backend)
            self._log.info("tools_list_changed", backend=self._backend.name)
        except anyio.WouldBlock:
            pass  # queue full — a refresh for this burst is already pending


# ---------------------------------------------------------------------------
# Optional bearer-token auth for backend endpoints (pure-ASGI middleware)
# ---------------------------------------------------------------------------


class BearerAuthMiddleware:
    """Require ``Authorization: Bearer <token>`` on backend MCP endpoints AND
    the admin API (#26; admin coverage added by the 2026-07-12 audit).

    Defense-in-depth for the loopback bind: with a token configured, a curious
    or compromised local process can't call the backends without it — and the
    admin API had to follow, or the same process could simply rewrite config,
    restart the daemon, or execute backend tools through ``/admin/api/run``
    (the audit did exactly that). ``token`` is resolved ONCE at startup
    (``expand_env`` in ``_build_app``), never per request; a falsy token makes
    the middleware a pure passthrough.

    Open without a token: ``/health`` + ``/ready`` (liveness probes) and the
    bare ``GET /admin`` page — the static UI shell, which needs to load so it
    can prompt for the token; every piece of data it then fetches is behind
    ``/admin/api/*`` and challenged. The comparison is ``hmac.compare_digest``
    on the encoded bytes (no timing side channel); a failure gets a 401 JSON
    body plus the ``WWW-Authenticate: Bearer`` challenge.
    """

    EXEMPT_PREFIXES = ("/health", "/ready")

    def __init__(self, app, *, token: str | None):
        self.app = app
        self._expected = f"Bearer {token}".encode() if token else None

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            self._expected is None  # no token configured -> zero overhead
            or scope["type"] != "http"
            or path.startswith(self.EXEMPT_PREFIXES)
            # the UI shell only — it carries no data and prompts for the token
            or (path == "/admin" and scope.get("method") == "GET")
        ):
            await self.app(scope, receive, send)
            return
        supplied = dict(scope.get("headers", [])).get(b"authorization", b"")
        if supplied and hmac.compare_digest(supplied, self._expected):
            await self.app(scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"www-authenticate", b"Bearer"),
                ],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": b'{"ok": false, "error": "missing or invalid bearer token"}',
            }
        )


# ---------------------------------------------------------------------------
# Origin validation (pure-ASGI middleware) — MCP spec DNS-rebinding protection
# ---------------------------------------------------------------------------


class OriginGuardMiddleware:
    """Reject cross-origin browser requests: MCP spec (Streamable HTTP
    security), normative — servers MUST validate the ``Origin`` header to
    prevent DNS rebinding, and MUST answer an invalid one with 403.

    The attack: a malicious web page rebinds its hostname to 127.0.0.1 and
    fetches the loopback gateway from the victim's browser. Such a request
    always carries the page's own ``Origin``; requests from non-browser
    clients (Claude Code, curl) carry none and pass. Allowed origins are the
    gateway's own (admin UI same-origin fetches) on any loopback host
    spelling; everything else — including ``Origin: null`` from sandboxed
    documents — is 403. Applies to every route: the MCP mounts, the admin
    API, even /health (a rebinding page has no business probing liveness).
    """

    def __init__(self, app, *, host: str, port: int):
        self.app = app
        hosts = {host, "127.0.0.1", "localhost", "[::1]"}
        self._allowed = {f"http://{h}:{port}".encode() for h in hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            origin = dict(scope.get("headers", [])).get(b"origin")
            if origin is not None and origin not in self._allowed:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 403,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"ok": false, "error": "invalid origin"}',
                    }
                )
                return
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Startup reconciliation: warn on configured tools that no backend exposes
# ---------------------------------------------------------------------------


def _reconcile(index: dict[str, str], known_tools, log) -> None:
    """Warn on transform keys (config ``original`` names) that no tool in the
    backend's CAPTURED defaults matches — almost always a typo in config.toml.

    Reconciles against the captured defaults, not a live round-trip (#105): the
    boot path stays connection-free (no extra list_tools per backend). If a
    backend's live tools have drifted from the captured baseline, re-introspect
    to refresh it.
    """
    if not index:
        return  # no overrides to reconcile
    known = set(known_tools)
    for key, backend in index.items():
        if key not in known:
            log.warning(
                "override_no_match",
                tool=key,
                backend=backend,
                hint="not in captured defaults — check 'original' in config.toml, "
                "or re-introspect if the backend changed",
            )
    log.info("reconcile_done", known_tools=len(known), overrides=len(index))


# ---------------------------------------------------------------------------
# Per-backend proxy build + mount
# ---------------------------------------------------------------------------


def _unmount(app: Starlette, name: str, registry: dict, holders: dict) -> None:
    """Remove a backend's live mount — its Mount route plus registry + holder
    entries (#78). Runs from the backend's OWN runner as it unwinds (so a later
    re-enable / hot-add can cleanly re-append the route)."""
    registry.pop(name, None)
    holders.pop(name, None)
    app.router.routes[:] = [
        r for r in app.router.routes if getattr(r, "path", None) != f"/{name}"
    ]


def _suppress_list_changed(proxy) -> None:
    """Stop this proxy advertising ``listChanged`` for tools/resources/prompts.

    FastMCP advertises ``listChanged=true``, but the gateway hot-reloads
    transforms WITHOUT pushing a ``notifications/*/list_changed`` — so promising
    the capability is worse than not (a client would wait for a push that never
    comes; Claude re-lists on its next tool use / reconnect anyway). #90.

    Reaches into FastMCP's ``_mcp_server.notification_options`` (private, like
    hot_reload's ``_transforms``); the test tripwires a FastMCP rename.
    """
    proxy._mcp_server.notification_options = NotificationOptions(
        prompts_changed=False, resources_changed=False, tools_changed=False
    )


async def _health(_request: Request) -> PlainTextResponse:
    # Body starts with "ok" so existing liveness checks still pass; the version
    # tail lets you confirm which build answered (#57). The resolved code path
    # makes path drift visible (#149): after the repo moved, a ghost process
    # started from the OLD clone kept /health green while the installed
    # LaunchAgent pointed nowhere — a /health that names the directory the
    # daemon actually runs from turns "running from a deleted/moved clone"
    # into something a one-line curl can catch.
    here = Path(__file__).resolve().parent
    return PlainTextResponse(f"ok mcp-gateway {admin.gateway_version()} @ {here}")


async def _mount_backend(  # noqa: PLR0913 — the mount needs the full lifespan plumbing; a param object would just rename the coupling
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
    message_handler: MessageHandler | None = None,
) -> bool:
    """Build ONE backend's proxy, apply its transforms + instructions, run its
    http lifespan, and mount it at ``/<backend>/mcp``. Returns True on success.

    *message_handler* (stateful backends only) subscribes the persistent client
    to backend notifications — the ``tools/list_changed`` trigger of #43.

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
                Client(
                    config_loader.to_proxy_config_one(b),
                    message_handler=message_handler,
                )
            )
            proxy = create_proxy(client, name=name)
        else:
            proxy = create_proxy(config_loader.to_proxy_config_one(b), name=name)

        _suppress_list_changed(proxy)  # #90
        proxy.add_middleware(CallLogMiddleware(log, b.name))
        transforms, index = config_loader.build_transforms(
            cfg, b, all_tools, captured_meta
        )
        # Reconcile config `original` names against captured defaults (no live
        # round-trip, #105); SOURCE names, before renames apply.
        _reconcile(index, all_tools.get(b.name, []), log)
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


def _build_app(  # noqa: PLR0913, PLR0915 — composition root; takes what it wires, and its lifespan owns the per-runner anyio scope rules (splitting would scatter them)
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
    # #26: resolve the optional gateway bearer token ONCE at build time — a
    # missing ${ENV} ref must fail loudly at startup (expand_env raises
    # ConfigError), never surface per request.
    bearer_token = (
        config_loader.expand_env(cfg.bearer_token) if cfg.bearer_token else None
    )
    # Populated in the lifespan; the admin closes over both (by reference) so its
    # hot-reload can target the right backend's live proxy + transform holder.
    registry: dict = {}
    holders: dict = {}
    hooks: dict = {}  # lifespan installs hooks["add"] (hot-add) for the admin

    @asynccontextmanager
    async def lifespan(app: Starlette):
        # One teardown event per backend (#78): setting one unmounts JUST that
        # backend — its runner unwinds its OWN AsyncExitStack (the anyio LIFO rule
        # from #7, killing a warm client / stdio child), then _unmount drops its
        # route. On shutdown we set them all. A disabled backend is never mounted
        # (boot skips it), so its endpoint is simply absent (404) until re-enabled.
        stops: dict[str, anyio.Event] = {}
        # #43: list_changed queue + worker, post-mount refresh, interval sweep.
        refresher = _AutoRefresh(
            cfg.introspect_interval, config_path, registry, holders, log
        )

        async def runner(  # noqa: PLR0913 — mirrors _mount_backend's signature
            b,
            cfg_,
            all_tools_,
            meta_,
            captured_,
            *,
            task_status=anyio.TASK_STATUS_IGNORED,
        ):
            ev = anyio.Event()
            stops[b.name] = ev
            # Stateful backends get a persistent client -> subscribe it to
            # tools/list_changed (#43); stateless ones have no standing session.
            handler = (
                None
                if b.stateless
                else _ListChangedHandler(b, refresher.send.send_nowait, log)
            )
            try:
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
                        handler,
                    )
                    task_status.started(ok)
                    if ok:
                        # #43 trigger 1 (the load-bearing one): a (re)connect
                        # means possibly-new backend state — re-capture the
                        # baseline in the background (throttled; boot after an
                        # upgrade catches e.g. gitnexus 13 -> 17 tools).
                        tg.start_soon(refresher.refresh, b)
                        await ev.wait()
            finally:
                stops.pop(b.name, None)
                _unmount(app, b.name, registry, holders)

        async with anyio.create_task_group() as tg:
            tg.start_soon(refresher.worker)  # #43: list_changed consumer
            if refresher.interval > 0:
                tg.start_soon(refresher.interval_loop)
            # #61: start every backend's runner CONCURRENTLY and don't block
            # readiness on any of them — boot ≈ the slowest backend instead of
            # the sum, and a slow/hung backend delays only its own endpoint
            # (each runner appends its Mount when ready, same path as hot-add).
            for b in cfg.backends:
                if b.enabled:  # #78: disabled backends aren't mounted (404)
                    tg.start_soon(
                        runner, b, cfg, all_tools, captured_meta, captured_instr
                    )

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

            def hot_remove(name: str) -> None:
                """Unmount a backend live (#78): set its teardown event; the runner
                unwinds its own stack and _unmount drops the route + registry."""
                ev = stops.get(name)
                if ev is not None:
                    ev.set()

            hooks["add"] = hot_add
            hooks["remove"] = hot_remove
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
                hooks.pop("remove", None)
                refresher.close()  # ends the worker + interval loop (#43)
                for ev in list(stops.values()):
                    ev.set()  # graceful: every runner exits its stack, tg drains
                # A runner stuck mid-connect never reaches its event wait; without a
                # deadline it would hang shutdown forever. Grace, then cancel.
                tg.cancel_scope.deadline = anyio.current_time() + SHUTDOWN_GRACE

    # Admin + health are static (no backend connection needed); the per-backend
    async def _ready(_request: Request) -> JSONResponse:
        # Readiness (#94), distinct from /health liveness: a configured + ENABLED
        # backend that hasn't mounted yet (or failed to) -> 503, so a monitor can
        # tell "up" from "up but degraded". Read the LIVE config (not the boot cfg)
        # so a backend disabled at runtime (#78) isn't falsely reported missing;
        # `registry` is populated by the runners as each backend connects.
        live = config_loader.load(config_path)
        want = [b.name for b in live.backends if b.enabled]
        missing = [n for n in want if n not in registry]
        return JSONResponse(
            {
                "ready": not missing,
                "mounted": sorted(registry),
                "enabled": want,
                "missing": missing,
            },
            status_code=200 if not missing else 503,
        )

    # MCP mounts are added during the lifespan (they need connected clients).
    parent = Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Route("/ready", _ready, methods=["GET"]),
        ],
        lifespan=lifespan,
        middleware=[
            # Origin guard FIRST — a rebinding page's request dies before any
            # body buffering or auth work (MCP spec MUST).
            StarletteMiddleware(OriginGuardMiddleware, host=cfg.host, port=cfg.port),
            StarletteMiddleware(BodyLimitMiddleware, max_bytes=ADMIN_BODY_LIMIT),
            StarletteMiddleware(BearerAuthMiddleware, token=bearer_token),
        ],
    )
    admin.register(parent, config_path, log, registry, holders, hooks)
    return parent


async def _run(cfg: config_loader.GatewayConfig, log) -> None:
    # Capture each backend's tools + server instructions (no-op if already
    # cached) so transforms (incl. per-backend always_load) and per-endpoint
    # instructions can be built from the captured baseline.
    await admin.ensure_defaults(cfg, log)
    # #156: remove any captured-defaults files for backends no longer in config
    # (predate prune-on-remove) so a stale baseline can't resurrect a ghost.
    admin.sweep_orphan_defaults(cfg, log)
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
    # `mcp-gateway --version` prints and exits — the install smoke check
    # (anything else on argv is a usage error; the daemon takes no arguments).
    args = sys.argv[1:]
    if args == ["--version"]:
        print(
            f"mcp-gateway {admin.gateway_version()} @ {Path(__file__).resolve().parent}"
        )
        return
    if args:
        print(
            f"usage: mcp-gateway [--version]  (unexpected: {' '.join(args)})",
            file=sys.stderr,
        )
        raise SystemExit(2)
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
