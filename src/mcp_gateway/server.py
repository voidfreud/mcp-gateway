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
import httpx
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

from mcp_gateway import admin, config_loader, runtime, virtual_tools


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

# #161: minimum gap between automatic recycles of ONE warm backend. A hard-down
# backend would otherwise flap — every failing call and every status probe would
# fire another teardown+remount. One recycle per backend per this window; a
# trigger inside the cooldown is skipped with a log line. Module-scoped so it
# resets on restart (and is patchable/resettable in tests, like admin._last_refresh).
RECYCLE_COOLDOWN = 30.0

# #161: per-backend monotonic timestamp of the last (attempted) recycle.
_last_recycle: dict[str, float] = {}


# ---------------------------------------------------------------------------
# Warm-session death detection (#161)
# ---------------------------------------------------------------------------

# Exception TYPES that mean the underlying MCP transport/session is gone (not a
# tool-level error). Conservative on purpose (a normal ToolError/ValueError from
# a backend tool must NOT match) — we key off transport/stream categories, plus
# the message signatures below.
_SESSION_DEATH_TYPES: tuple[type[BaseException], ...] = (
    anyio.ClosedResourceError,
    anyio.BrokenResourceError,
    anyio.EndOfStream,
    BrokenPipeError,
    ConnectionError,  # incl. ConnectionResetError / ConnectionAbortedError
    httpx.RemoteProtocolError,  # httpx "server disconnected" mid-stream
)

# Lowercased substrings that mark a dead session when they appear in the
# exception's type name or message. Categories, not a blanket match.
_SESSION_DEATH_SIGNS: tuple[str, ...] = (
    "closedresourceerror",
    "brokenresourceerror",
    "closed stream",
    "closed resource",
    "session terminated",
    "session closed",
    "session not found",
    "disconnected",
    "broken pipe",
    "connection closed",
    "connection reset",
    "peer closed",
)


def is_session_death(exc: BaseException) -> bool:
    """True iff *exc* looks like a dead warm MCP session (as opposed to a normal
    tool-level failure). Conservative: matches known transport/stream exception
    types, then a small set of message signatures — never a blanket Exception."""
    if isinstance(exc, _SESSION_DEATH_TYPES):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(sign in text for sign in _SESSION_DEATH_SIGNS)


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
    """Log every tool call and its outcome/latency to the structured log.

    #161: for a WARM backend (persistent session), a call that dies because the
    remote session went away is the trigger to recycle the backend — fastmcp's
    shared clients never self-heal. When *on_session_death* is set (warm backends
    only) and the raised exception matches a session-death signature, we
    fire-and-forget that callback (it schedules the recycle; we never await it in
    the call path) and then re-raise as normal. The failing call still fails; the
    NEXT call finds a freshly re-mounted session. A cooldown in the recycle path
    keeps a hard-down backend from flapping.
    """

    def __init__(
        self,
        log: structlog.BoundLogger,
        backend: str,
        on_session_death=None,
    ) -> None:
        self._log = log
        self._backend = backend
        # None for stateless backends (fresh session per call — nothing to
        # recycle); a zero-arg fire-and-forget trigger for warm backends.
        self._on_session_death = on_session_death

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
            # #161: warm session looks dead -> schedule a recycle (never awaited
            # here), then re-raise so this call still fails as today.
            if self._on_session_death is not None and is_session_death(exc):
                self._log.warning(
                    "warm_session_death", backend=self._backend, tool=tool
                )
                self._on_session_death()
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
        self,
        interval: int,
        baseline_max_age: int,
        config_path: str,
        backend_runtime: runtime.BackendRuntime,
        log,
    ) -> None:
        self.interval = interval
        self.baseline_max_age = baseline_max_age
        self._config_path = config_path
        self._runtime = backend_runtime
        self._log = log
        self.shutdown = anyio.Event()
        self.send, self._recv = anyio.create_memory_object_stream(16)

    async def refresh(self, b, throttle=None, max_age=0.0):
        """Re-capture one backend's baseline + hot-reload on change (#43).
        Never raises — auto-refresh is best-effort background work."""
        try:
            await admin.refresh_and_reload(
                b,
                self._config_path,
                self._runtime,
                self._log,
                throttle=admin.REFRESH_THROTTLE if throttle is None else throttle,
                max_age=max_age,
            )
        except Exception as exc:  # noqa: BLE001
            self._log.warning("auto_refresh_error", backend=b.name, error=str(exc))

    async def post_mount(self, b):
        """#43 trigger 1 — the (re)mount refresh, and the ONLY age-gated one
        (#157): a baseline younger than ``baseline_max_age`` is skipped, so a
        routine boot doesn't pay every slow stdio backend a second cold start.
        Event-driven triggers stay ungated — they're explicit change signals."""
        await self.refresh(b, None, self.baseline_max_age)

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
                if b.enabled and b.name in self._runtime.proxies:
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


def _unmount(
    app: Starlette, name: str, backend_runtime: runtime.BackendRuntime
) -> None:
    """Remove a backend's live mount — its Mount route plus registry + holder
    entries (#78). Runs from the backend's OWN runner as it unwinds (so a later
    re-enable / hot-add can cleanly re-append the route)."""
    backend_runtime.unmount(name)
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
    all_tools: runtime.CapturedTools,
    captured_meta: runtime.CapturedMeta,
    captured_instr: runtime.CapturedInstructions,
    backend_runtime: runtime.BackendRuntime,
    log,
    message_handler: MessageHandler | None = None,
    on_session_death=None,
) -> bool:
    """Build ONE backend's proxy, apply its transforms + instructions, run its
    http lifespan, and mount it at ``/<backend>/mcp``. Returns True on success.

    *message_handler* (stateful backends only) subscribes the persistent client
    to backend notifications — the ``tools/list_changed`` trigger of #43.

    *on_session_death* (warm backends only, #161) is a zero-arg fire-and-forget
    trigger the call-log middleware calls when a live call dies from a dead
    session, so the runner recycles the backend (fresh session) automatically.

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
            proxy = create_proxy(
                client,
                name=name,
                list_page_size=config_loader.DOWNSTREAM_TOOLS_PAGE_SIZE,
            )
        else:
            proxy = create_proxy(
                config_loader.to_proxy_config_one(b),
                name=name,
                list_page_size=config_loader.DOWNSTREAM_TOOLS_PAGE_SIZE,
            )

        _suppress_list_changed(proxy)  # #90
        proxy.add_middleware(CallLogMiddleware(log, b.name, on_session_death))
        transforms, index = config_loader.build_transforms(
            cfg, b, all_tools, captured_meta
        )
        # Reconcile config `original` names against captured defaults (no live
        # round-trip, #105); SOURCE names, before renames apply.
        _reconcile(index, all_tools.get(b.name, []), log)
        proxy.add_transform(transforms)
        # #15: resource/prompt text rewrites ride the same transform chain (the
        # holder carries every gateway-owned transform so hot_reload swaps all).
        holder = [transforms]
        rp_transform = config_loader.build_resource_prompt_transform(b)
        if rp_transform is not None:
            proxy.add_transform(rp_transform)
            holder.append(rp_transform)
        # Per-endpoint instructions: only this backend's blurb -> its own 2KB.
        proxy.instructions = config_loader.backend_instructions(b, captured_instr)

        sub = proxy.http_app(path="/mcp")
        # Starlette only runs the TOP app's lifespan, so run each mounted app's
        # session-manager lifespan ourselves; it stays open for the daemon.
        await stack.enter_async_context(sub.lifespan(sub))
        app.router.routes.append(Mount(f"/{b.name}", app=sub))

        backend_runtime.mount(b.name, proxy, holder)
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
    all_tools: runtime.CapturedTools,
    captured_meta: runtime.CapturedMeta,
    captured_instr: runtime.CapturedInstructions,
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
    backend_runtime = runtime.BackendRuntime()
    hooks: dict = {}  # lifespan installs hooks["add"] (hot-add) for the admin

    @asynccontextmanager
    async def lifespan(app: Starlette):  # noqa: PLR0915 — the per-runner lifecycle + #43/#161 plumbing is one cohesive scope
        # One teardown event per backend (#78): setting one unmounts JUST that
        # backend — its runner unwinds its OWN AsyncExitStack (the anyio LIFO rule
        # from #7, killing a warm client / stdio child), then _unmount drops its
        # route. On shutdown we set them all. A disabled backend is never mounted
        # (boot skips it), so its endpoint is simply absent (404) until re-enabled.
        stops: dict[str, anyio.Event] = {}
        # #43: list_changed queue + worker, post-mount refresh, interval sweep.
        refresher = _AutoRefresh(
            cfg.introspect_interval,
            cfg.baseline_max_age,
            config_path,
            backend_runtime,
            log,
        )
        # #161: recycle requests (backend name) enqueued from arbitrary tasks —
        # the call-log middleware, the status probe, the stateless-toggle route.
        # A single worker task INSIDE this task group drains them, so the actual
        # teardown+remount always runs in the group that owns the runners (the
        # same safe pattern as the #43 list_changed stream). Never awaited by the
        # trigger sites — they only send_nowait.
        recycle_send, recycle_recv = anyio.create_memory_object_stream(32)

        def fire_recycle(name: str) -> None:
            """Fire-and-forget: enqueue a recycle of *name*. Swallows a full or
            closed queue (a recycle for this backend is already pending / we're
            shutting down)."""
            try:
                recycle_send.send_nowait(name)
            except (anyio.WouldBlock, anyio.ClosedResourceError):
                pass

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
            # #161: only WARM backends recycle (a stateless backend already uses
            # a fresh session per call — nothing to heal).
            on_death = None if b.stateless else (lambda: fire_recycle(b.name))
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
                        backend_runtime,
                        log,
                        handler,
                        on_death,
                    )
                    task_status.started(ok)
                    if ok:
                        # #43 trigger 1 (the load-bearing one): a (re)connect
                        # means possibly-new backend state — re-capture the
                        # baseline in the background (throttled; boot after an
                        # upgrade catches e.g. gitnexus 13 -> 17 tools).
                        # #157: age-gated — skipped while the stored baseline
                        # is younger than cfg.baseline_max_age.
                        tg.start_soon(refresher.post_mount, b)
                        await ev.wait()
            finally:
                stops.pop(b.name, None)
                _unmount(app, b.name, backend_runtime)

        async def virtual_runner():
            """Own the permanent gateway-authored ``/virtual/mcp`` endpoint.

            Unlike backend runners, absence is never an acceptable steady state:
            the endpoint mounts even with an empty tool list, and readiness stays
            red if its build or lifespan fails.
            """
            ev = anyio.Event()
            stops[virtual_tools.VIRTUAL_ROUTE] = ev
            hooks.pop("virtual_mount_error", None)
            try:
                async with AsyncExitStack() as stack:
                    try:
                        server = virtual_tools.build_virtual_server(
                            cfg,
                            lambda: config_loader.load(config_path),
                            backend_runtime.proxies,
                            log,
                            hooks.setdefault("virtual_status", {}),
                        )
                        # Virtual Tools hot-swap their provider map from Admin
                        # routes but cannot yet broadcast to every connected
                        # downstream session. Keep the negotiated capability
                        # truthful until a server-wide session registry exists.
                        _suppress_list_changed(server)
                        sub = server.http_app(path="/mcp")
                        await stack.enter_async_context(sub.lifespan(sub))
                        app.router.routes.append(
                            Mount(f"/{virtual_tools.VIRTUAL_ROUTE}", app=sub)
                        )
                        hooks["virtual_server"] = server
                        log.info(
                            "virtual_tools_mounted",
                            path=f"/{virtual_tools.VIRTUAL_ROUTE}/mcp",
                            tools=[
                                tool.name for tool in cfg.virtual_tools if tool.enabled
                            ],
                        )
                    except Exception as exc:  # noqa: BLE001
                        hooks["virtual_mount_error"] = f"{type(exc).__name__}: {exc}"
                        log.error("virtual_tools_mount_failed", error=str(exc))
                        return
                    await ev.wait()
            finally:
                stops.pop(virtual_tools.VIRTUAL_ROUTE, None)
                hooks.pop("virtual_server", None)
                app.router.routes[:] = [
                    route
                    for route in app.router.routes
                    if getattr(route, "path", None) != f"/{virtual_tools.VIRTUAL_ROUTE}"
                ]

        async def hot_recycle(name: str) -> None:
            """Tear a warm backend's runner down like hot_remove AND immediately
            re-run it — fresh AsyncExitStack, fresh client, re-mount (#161). This
            is the automatic heal fastmcp's shared clients don't do: a warm remote
            session that died is replaced without a daemon restart. Debounced by
            RECYCLE_COOLDOWN per backend so a hard-down backend can't flap.

            A re-run reads FRESH config, so it also picks up a changed
            ``stateless`` flag (the stateless-toggle route commits then recycles)."""
            now = time.monotonic()
            last = _last_recycle.get(name)
            if last is not None and now - last < RECYCLE_COOLDOWN:
                log.info("recycle_skipped", backend=name, reason="cooldown")
                return
            _last_recycle[name] = now
            cfg2 = config_loader.load(config_path)
            b = next((x for x in cfg2.backends if x.name == name), None)
            if b is None or not b.enabled:
                log.info("recycle_skipped", backend=name, reason="absent-or-disabled")
                return
            log.info("recycle_start", backend=name)
            ev = stops.get(name)
            if ev is not None:
                ev.set()  # tear the old runner down (unwinds its stack, _unmount)
                # Wait for the old runner to FULLY unwind before re-running, so the
                # new mount can't race the old runner's _unmount (which strips every
                # route on this path). stops.pop happens in the runner's finally,
                # immediately before _unmount — so `name not in stops` means done.
                with anyio.move_on_after(SHUTDOWN_GRACE + 2):
                    while name in stops:
                        await anyio.sleep(0.02)
            ok = await tg.start(
                runner,
                b,
                cfg2,
                admin.all_tools_from_defaults(cfg2),
                admin.all_meta_from_defaults(cfg2),
                admin.captured_instructions(cfg2),
            )
            log.info("recycle_done", backend=name, ok=ok)

        async def recycle_worker() -> None:
            async for name in recycle_recv:  # ends when recycle_send closes
                try:
                    await hot_recycle(name)
                except Exception as exc:  # noqa: BLE001 — best-effort background heal
                    log.warning("recycle_error", backend=name, error=str(exc))

        async with anyio.create_task_group() as tg:
            tg.start_soon(refresher.worker)  # #43: list_changed consumer
            tg.start_soon(recycle_worker)  # #161: warm-session recycle consumer
            if refresher.interval > 0:
                tg.start_soon(refresher.interval_loop)
            tg.start_soon(virtual_runner)
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
            hooks["recycle"] = fire_recycle  # #161
            log.info(
                "gateway_starting",
                backends=[b.name for b in cfg.backends],
                endpoints=[
                    f"http://{cfg.host}:{cfg.port}/{b.name}/mcp" for b in cfg.backends
                ]
                + [f"http://{cfg.host}:{cfg.port}/virtual/mcp"],
                admin=f"http://{cfg.host}:{cfg.port}/admin",
            )
            try:
                yield
            finally:
                hooks.pop("add", None)
                hooks.pop("remove", None)
                hooks.pop("recycle", None)
                recycle_send.close()  # ends the recycle worker (#161)
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
        missing = [n for n in want if n not in backend_runtime.proxies]
        virtual_ready = "virtual_server" in hooks
        ready = not missing and virtual_ready
        return JSONResponse(
            {
                "ready": ready,
                "mounted": sorted(backend_runtime.proxies),
                "enabled": want,
                "missing": missing,
                "virtual": {
                    "mounted": virtual_ready,
                    "endpoint": "/virtual/mcp",
                    "error": hooks.get("virtual_mount_error"),
                },
            },
            status_code=200 if ready else 503,
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
    admin.register(parent, config_path, log, backend_runtime, hooks=hooks)
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
