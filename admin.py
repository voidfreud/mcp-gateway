"""Admin UI + API for mcp-gateway, served by the same daemon at /admin.

Lets you see every backend MCP, every tool it broadcasts, and edit what Claude
Code sees — tool title/description, parameter names/descriptions, hide a param,
disable a tool — while the backend's *original* names stay fixed. Each backend's
original broadcast is captured once as an immutable baseline (the "default") so
any field can be reset; config.toml is snapshotted on every save.

Text edits hot-reload into the running proxy in-process (no restart, no client
disconnect). Backend changes (import/remove/url/auth) need a connection rebuild,
so those write config and restart the daemon.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import subprocess
import time
from pathlib import Path

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from fastmcp import Client

import config_loader as cl
from config_loader import Backend, GatewayConfig, ParamOverride, ToolOverride

STATE_DIR = Path("~/.local/state/mcp-gateway").expanduser()
DEFAULTS_DIR = STATE_DIR / "defaults"
BACKUP_DIR = STATE_DIR / "backups"
HERE = Path(__file__).resolve().parent
LAUNCHD_LABEL = "com.void.mcp-gateway"


def gateway_version() -> str:
    """The gateway's own version, from a single source (package metadata, else
    the ``version = "..."`` line in pyproject.toml). Surfaced in the admin UI and
    ``/health`` so the running build is visible after a restart/upgrade (#57)."""
    try:
        return importlib.metadata.version("mcp-gateway")
    except importlib.metadata.PackageNotFoundError:
        pass
    try:
        text = (HERE / "pyproject.toml").read_text(encoding="utf-8")
        m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "unknown"


# ---------------------------------------------------------------------------
# Introspection — capture a backend's original broadcast (the "default")
# ---------------------------------------------------------------------------


def _client_config(b: Backend) -> dict:
    """Single-backend client config so tool names come back un-prefixed (bare).

    Delegates to :func:`config_loader.to_proxy_config_one` so every transport
    (http / streamable-http / sse / stdio) is handled identically to the live
    proxy — otherwise import-time introspection of an sse/streamable-http backend
    would mis-treat it as stdio and fail (issue #5).
    """
    return cl.to_proxy_config_one(b)


def _annotations_to_dict(ann) -> dict | None:
    """Serialize a tool's ``annotations`` (an MCP ``ToolAnnotations`` pydantic
    model) to a plain JSON dict for the defaults file, dropping unset hints.
    Returns None when there is nothing to store. Tolerant of a plain dict or None
    so the reader survives MCP-SDK shape changes."""
    if ann is None:
        return None
    if hasattr(ann, "model_dump"):
        d = ann.model_dump(exclude_none=True, mode="json")
    elif isinstance(ann, dict):
        d = {k: v for k, v in ann.items() if v is not None}
    else:
        d = getattr(ann, "__dict__", None)
    return d or None


async def capture_defaults(b: Backend) -> dict:
    """Connect to one backend and return its original broadcast as a baseline.

    Captures the per-tool text AND the server-level ``initialize`` data the proxy
    otherwise drops: ``instructions`` (the always-loaded server blurb), plus
    ``serverInfo`` and ``capabilities`` (surfaced read-only in the admin UI).
    """
    async with Client(_client_config(b)) as c:
        tools = await c.list_tools()
        init = c.initialize_result  # populated after the context entered
    out_tools = []
    for t in tools:
        schema = t.inputSchema or {}
        props = schema.get("properties", {})
        required = set(schema.get("required") or [])
        params = [
            {
                "original": name,
                "description": (spec or {}).get("description"),
                # whether the backend's inputSchema marks this param required —
                # surfaced in the UI and used to block hiding it (Claude could
                # never supply it, so every call would break).
                "required": name in required,
            }
            for name, spec in props.items()
        ]
        tool = {
            "original": t.name,
            "title": getattr(t, "title", None),
            "description": t.description,
            "params": params,
        }
        # Read-only schema surface (issue #2): the wire tools/list also carries an
        # outputSchema, _meta (FastMCP tags + our anthropic/alwaysLoad pin), and
        # ToolAnnotations (readOnly/destructive/idempotent/openWorld). Capture each
        # only when present so pre-#2 defaults files (which lack these keys) still
        # load — downstream readers use ``dt.get(...)``.
        if getattr(t, "outputSchema", None):
            tool["output_schema"] = t.outputSchema
        meta = getattr(t, "meta", None)  # mcp.types.Tool.meta (wire alias `_meta`)
        if meta:
            tool["meta"] = meta
        annotations = _annotations_to_dict(getattr(t, "annotations", None))
        if annotations:
            tool["annotations"] = annotations
        out_tools.append(tool)
    server_info = None
    capabilities = None
    instructions = None
    if init is not None:
        instructions = init.instructions
        si = getattr(init, "serverInfo", None)
        if si is not None:
            server_info = {"name": si.name, "version": si.version}
        caps = getattr(init, "capabilities", None)
        if caps is not None:
            capabilities = caps.model_dump(exclude_none=True, mode="json")
    return {
        "backend": b.name,
        "captured_at": time.time(),
        # `instructions` is ALWAYS present (even if None) so its key presence marks
        # a defaults file as carrying the server-level capture (see ensure_defaults).
        "instructions": instructions,
        "server_info": server_info,
        "capabilities": capabilities,
        "tools": out_tools,
    }


def load_defaults(name: str) -> dict | None:
    p = DEFAULTS_DIR / f"{name}.json"
    if p.is_file():
        return json.loads(p.read_text(encoding="utf-8"))
    return None


def save_defaults(data: dict) -> None:
    DEFAULTS_DIR.mkdir(parents=True, exist_ok=True)
    (DEFAULTS_DIR / f"{data['backend']}.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )


async def ensure_defaults(cfg: GatewayConfig, log, force: str | None = None) -> None:
    """Capture defaults for any backend missing them (or *force* one by name).

    A defaults file written before server-level capture lacks the ``instructions``
    key; treat such a file as stale and re-capture it once, so old installs gain
    instructions/serverInfo without a manual re-introspect.
    """
    for b in cfg.backends:
        if force and b.name != force:
            continue
        if not force:
            existing = load_defaults(b.name)
            if existing is not None and "instructions" in existing:
                continue
        try:
            save_defaults(await capture_defaults(b))
            log.info("defaults_captured", backend=b.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("defaults_capture_failed", backend=b.name, error=str(exc))


def backup_config(config_path: str) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    src = Path(config_path).expanduser()
    if src.is_file():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (BACKUP_DIR / f"config-{stamp}.toml").write_text(
            src.read_text(encoding="utf-8"), encoding="utf-8"
        )
        # keep last 30
        backups = sorted(BACKUP_DIR.glob("config-*.toml"))
        for old in backups[:-30]:
            old.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# State for the UI — merge each backend's real tools (defaults) with overrides
# ---------------------------------------------------------------------------


def _find_tool_override(b: Backend, original: str) -> ToolOverride | None:
    return next((t for t in b.tools if t.original == original), None)


def all_tools_from_defaults(cfg: GatewayConfig) -> dict[str, list[str]]:
    """``backend name -> [original tool names]`` from captured defaults. Lets
    build_transforms apply a per-backend ``always_load`` to un-overridden tools."""
    out: dict[str, list[str]] = {}
    for b in cfg.backends:
        d = load_defaults(b.name)
        if d:
            out[b.name] = [t["original"] for t in d.get("tools", [])]
    return out


def captured_instructions(cfg: GatewayConfig) -> dict[str, str | None]:
    """``backend name -> its captured original server instructions (or None)``.
    Feeds :func:`config_loader.backend_instructions` so each backend endpoint can
    hand its always-loaded blurb back to Claude (the proxy otherwise drops it)."""
    out: dict[str, str | None] = {}
    for b in cfg.backends:
        d = load_defaults(b.name)
        out[b.name] = (d or {}).get("instructions")
    return out


def apply_backend_instructions(proxy, cfg: GatewayConfig, b: Backend) -> str | None:
    """(Re)set one backend proxy's live server-level ``instructions`` from its
    effective blurb (override else captured original). Each backend endpoint
    carries only its own, so each keeps Claude Code's full per-server budget."""
    instr = cl.backend_instructions(b, captured_instructions(cfg))
    proxy.instructions = instr
    return instr


def effective_tools(cfg: GatewayConfig) -> list[dict]:
    """Every ENABLED tool's effective broadcast (name, description) across all
    backends, computed from defaults + overrides. Used to detect collisions —
    Claude can't tell two tools apart if they share a broadcast name."""
    out: list[dict] = []
    for b in cfg.backends:
        defaults = load_defaults(b.name) or {}
        for dt in defaults.get("tools", []):
            orig = dt["original"]
            ov = _find_tool_override(b, orig)
            if ov is not None and not ov.enabled:
                continue  # disabled -> not broadcast -> can't collide
            name = ov.name if (ov and ov.name) else cl.exposed_name(cfg, b, orig)
            desc = ov.description if (ov and ov.description) else dt.get("description")
            out.append(
                {"backend": b.name, "original": orig, "name": name, "description": desc}
            )
    return out


def check_no_collision(
    cfg: GatewayConfig, backend: str, original: str, new: ToolOverride
) -> None:
    """Reject if *new* would make the edited tool share a broadcast NAME with any
    other enabled tool, or a deliberately-set DESCRIPTION identical to another's.
    Passthrough never collides (prefixed names are unique), so this only fires on
    a real rename/description clash."""
    if not new.enabled:
        return  # not broadcast
    b = next(x for x in cfg.backends if x.name == backend)
    eff_name = new.name or cl.exposed_name(cfg, b, original)
    for other in effective_tools(cfg):
        # Each backend is its own endpoint/MCP server now, so broadcast names
        # only need to be unique WITHIN a backend — a clash across backends can't
        # confuse Claude (different server namespaces).
        if other["backend"] != backend:
            continue
        if other["original"] == original:
            continue
        if other["name"] == eff_name:
            raise cl.ConfigError(
                f"broadcast name {eff_name!r} is already used by tool "
                f"{other['original']!r} in backend {other['backend']!r} — names "
                f"must be unique; pick a different one"
            )
        if new.description is not None and new.description == other["description"]:
            raise cl.ConfigError(
                f"that description is identical to tool {other['original']!r} in "
                f"backend {other['backend']!r} — make it unique so Claude can tell "
                f"them apart"
            )


def build_state(cfg: GatewayConfig) -> dict:
    backends = []
    for b in cfg.backends:
        defaults = load_defaults(b.name)
        default_tools = (defaults or {}).get("tools", [])
        tools_state = []
        for dt in default_tools:
            ov = _find_tool_override(b, dt["original"])
            ov_params = {p.original: p for p in (ov.params if ov else [])}
            params = []
            for dp in dt.get("params", []):
                p = ov_params.get(dp["original"])
                params.append(
                    {
                        "original": dp["original"],
                        # default broadcast name of a param == its original name
                        # (params are never prefixed); the field prefills with this.
                        "default_name": dp["original"],
                        "default_description": dp.get("description"),
                        # backend marks this param required (from inputSchema) —
                        # the UI flags it and hiding it is rejected on save.
                        "required": dp.get("required", False),
                        "name": p.name if p else None,
                        "description": p.description if p else None,
                        "hide": p.hide if p else False,
                    }
                )
            tools_state.append(
                {
                    "original": dt["original"],
                    # default broadcast name == the exposed (possibly prefixed) name
                    "default_name": cl.exposed_name(cfg, b, dt["original"]),
                    "default_title": dt.get("title"),
                    "default_description": dt.get("description"),
                    "name": ov.name if ov else None,
                    "title": ov.title if ov else None,
                    "description": ov.description if ov else None,
                    "enabled": ov.enabled if ov else True,
                    "always_load": ov.always_load if ov else False,
                    # Read-only schema surface (issue #2): surfaced as-captured;
                    # None when the backend didn't advertise them or the defaults
                    # file predates the capture (use .get so old files degrade).
                    "output_schema": dt.get("output_schema"),
                    "meta": dt.get("meta"),
                    "annotations": dt.get("annotations"),
                    "params": params,
                }
            )
        backends.append(
            {
                "name": b.name,
                "display_name": b.display_name,
                "enabled": b.enabled,
                "endpoint": f"/{b.name}/mcp",
                "transport": b.transport,
                "url": b.url,
                "command": b.command,
                "args": list(b.args),
                "auth_header": b.auth_header,
                "auth_value": b.auth_value,
                "stateless": b.stateless,
                "always_load": b.always_load,
                "introspected": defaults is not None,
                # server-level instructions: captured original + this backend's
                # override (None = inherit the original); plus serverInfo.
                "default_instructions": (defaults or {}).get("instructions"),
                "instructions": b.instructions,
                "server_info": (defaults or {}).get("server_info"),
                "tools": tools_state,
            }
        )
    return {
        "host": cfg.host,
        "port": cfg.port,
        "version": gateway_version(),
        # Each backend is its own MCP endpoint with its own instructions now (no
        # single cross-backend "gateway instructions"); the UI shows an endpoints
        # overview + per-backend server-instructions editing.
        "backends": backends,
    }


# ---------------------------------------------------------------------------
# Mutation helpers (empty string -> None, i.e. "no override / inherit original")
# ---------------------------------------------------------------------------


def _clean(v):
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_name(name: str | None, what: str) -> None:
    """MCP-safe identifier guard. Conservative ([A-Za-z0-9_-]) so an edited name
    can't break the tool listing or `mcp__server__tool` resolution."""
    if name is not None and not _NAME_RE.match(name):
        raise cl.ConfigError(
            f"invalid {what} {name!r}: use only letters, digits, '_' or '-'"
        )


def _override_vs_default(value, default) -> str | None:
    """Return an override value, or None to inherit the default.

    Empty -> inherit. Equal to the default -> inherit (keeps config.toml minimal,
    so a field prefilled with its default and left unchanged stores nothing).
    """
    v = _clean(value)
    if v is None:
        return None
    if default is not None and v == default:
        return None
    return v


def apply_tool_override(cfg: GatewayConfig, backend: str, payload: dict) -> None:
    """Replace one tool's override from a UI payload, diffing against defaults.

    Every editable field arrives prefilled with its effective value; we only
    store a value that actually differs from the backend's default.
    """
    b = next((x for x in cfg.backends if x.name == backend), None)
    if b is None:
        raise cl.ConfigError(f"unknown backend {backend!r}")
    original = payload["tool_original"]
    ov = payload.get("override", {})

    # Defaults for this tool (original broadcast captured at introspection).
    defaults = load_defaults(backend) or {}
    dtool = next(
        (t for t in defaults.get("tools", []) if t["original"] == original), {}
    )
    default_name = cl.exposed_name(cfg, b, original)
    dparams = {p["original"]: p for p in dtool.get("params", [])}

    name = _override_vs_default(ov.get("name"), default_name)
    title = _override_vs_default(ov.get("title"), dtool.get("title"))
    description = _override_vs_default(ov.get("description"), dtool.get("description"))
    _validate_name(name, "tool name")

    params = []
    for p in ov.get("params", []):
        po = p["original"]
        dp = dparams.get(po, {})
        # a param's default broadcast name is its original name
        pname = _override_vs_default(p.get("name"), po)
        pdesc = _override_vs_default(p.get("description"), dp.get("description"))
        _validate_name(pname, "parameter name")
        hide = bool(p.get("hide", False))
        # Correctness guardrail (alongside check_no_collision): a param the
        # backend marks required can't be hidden — Claude could never supply it,
        # so every call would break. (No param-default injection exists yet.)
        if hide and dp.get("required", False):
            raise cl.ConfigError(
                f"parameter {po!r} is required by the backend — hiding it "
                f"would break the tool"
            )
        if pname or pdesc or hide:
            params.append(
                ParamOverride(original=po, name=pname, description=pdesc, hide=hide)
            )

    enabled = bool(ov.get("enabled", True))
    always_load = bool(ov.get("always_load", False))
    new = ToolOverride(
        original=original,
        name=name,
        title=title,
        description=description,
        enabled=enabled,
        always_load=always_load,
        params=params,
    )
    has_override = bool(
        name or title or description or not enabled or always_load or params
    )
    # Reject a rename/description that would collide with another broadcast tool.
    check_no_collision(cfg, backend, original, new)
    b.tools = [t for t in b.tools if t.original != original]
    if has_override:
        b.tools.append(new)


# ---------------------------------------------------------------------------
# Hot reload (text) and restart (backend topology)
# ---------------------------------------------------------------------------


def set_instructions(cfg: GatewayConfig, backend: str, value) -> None:
    """Set a per-backend server-instructions override from a UI value.

    Diff against the captured original — empty or equal-to-default inherits
    (stores nothing), so config stays minimal; a different value (incl. one added
    where the backend sends none) is stored.
    """
    b = next((x for x in cfg.backends if x.name == backend), None)
    if b is None:
        raise cl.ConfigError(f"unknown backend {backend!r}")
    default = (load_defaults(backend) or {}).get("instructions")
    b.instructions = _override_vs_default(value, default)


def hot_reload(
    registry: dict, holders: dict, cfg: GatewayConfig, backend: str, log
) -> None:
    """Rebuild ONE backend's transforms from cfg and swap them into its live
    proxy in-process (no restart); also re-set that backend's instructions.

    ``registry`` maps backend name -> live proxy, ``holders`` maps backend name
    -> [current transform]; both are populated by the server lifespan."""
    b = next((x for x in cfg.backends if x.name == backend), None)
    proxy = registry.get(backend)
    if b is None or proxy is None:
        log.warning("hot_reload_skipped", backend=backend)
        return
    new_transform, _index = cl.build_transforms(cfg, b, all_tools_from_defaults(cfg))
    holder = holders.get(backend) or []
    old = holder[0] if holder else None
    if old is not None and old in proxy._transforms:
        proxy._transforms.remove(old)
    proxy.add_transform(new_transform)
    holders[backend] = [new_transform]
    apply_backend_instructions(proxy, cfg, b)
    # Live immediately (next tools/list reflects it). FastMCP has no
    # tools/list_changed helper, so a connected Claude session refreshes on its
    # next list / reconnect / new session.
    log.info("hot_reload", backend=backend)


def under_launchd() -> bool:
    """True iff *this* process is the one launchd manages, i.e. a kickstart would
    actually restart us. We ask launchctl for the loaded job's pid and compare it
    to our own — so a stale/other launchd copy, or a foreground dev run with no
    job loaded, both correctly report False. Lets callers tell the UI the truth
    instead of a blanket "restarting" (#53)."""
    uid = os.getuid()
    try:
        r = subprocess.run(
            ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    m = re.search(r"(?m)^\s*pid\s*=\s*(\d+)", r.stdout)
    return m is not None and int(m.group(1)) == os.getpid()


def restart_daemon(log) -> None:
    """Restart the launchd service (for backend topology changes).

    Run as a Starlette BackgroundTask, i.e. AFTER the HTTP response has flushed,
    so no `sleep` shell is needed. ``subprocess.run`` waits on (reaps) the child,
    so it never leaves a zombie; in production launchd then kills+restarts us.
    Callers gate this on ``under_launchd()`` so it's only scheduled when it will
    actually take effect; the kickstart-failed warning stays as a safety net.
    """
    uid = os.getuid()
    target = f"gui/{uid}/{LAUNCHD_LABEL}"
    try:
        r = subprocess.run(
            ["launchctl", "kickstart", "-k", target],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode != 0:
            log.warning("restart_failed", target=target, err=r.stderr.strip())
        else:
            log.info("restart_done", target=target)
    except Exception as exc:  # noqa: BLE001
        log.warning("restart_error", error=str(exc))


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------


def _needs_json(handler):
    """Wrap a body-reading route so a missing/malformed JSON body returns 400
    instead of an unhandled ``JSONDecodeError`` → 500 + traceback (issue #48).

    Starlette caches the parsed body on the request object, so the wrapped
    handler's own ``await request.json()`` reuses this parse at no extra cost.
    """

    async def guarded(request: Request):
        try:
            await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "malformed or missing JSON body"},
                status_code=400,
            )
        return await handler(request)

    return guarded


def register(app, config_path: str, log, registry: dict, holders: dict) -> None:
    """Attach the admin UI + API routes to the parent Starlette *app*.

    ``registry`` (backend name -> live proxy) and ``holders`` (backend name ->
    [current transform]) are populated during the server lifespan and shared by
    reference, so hot-reload targets the right backend's live proxy.
    """

    def _load() -> GatewayConfig:
        return cl.load(config_path)

    def _restart_response(extra: dict) -> JSONResponse:
        """Response for a topology change that needs a full restart. Only
        schedules (and claims) the restart when we're actually launchd-managed;
        in dev/foreground it says so honestly instead of a stuck "restarting"
        (#53). Config is already written either way — it takes effect on the next
        real restart."""
        if under_launchd():
            return JSONResponse(
                {"ok": True, "reloaded": "restarting", **extra},
                background=BackgroundTask(restart_daemon, log),
            )
        return JSONResponse({"ok": True, "reloaded": "dev-no-restart", **extra})

    async def admin_page(_request: Request):
        # no-cache so a plain browser reload always revalidates (ETag → 304 when
        # unchanged) and picks up admin.html edits. Without it the browser serves
        # a stale cached page and even a daemon restart doesn't refresh the UI.
        return FileResponse(HERE / "admin.html", headers={"Cache-Control": "no-cache"})

    async def get_state(_request: Request):
        return JSONResponse(build_state(_load()))

    async def put_override(request: Request):
        payload = await request.json()
        cfg = _load()
        try:
            apply_tool_override(cfg, payload["backend"], payload)
        except (cl.ConfigError, KeyError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        backup_config(config_path)
        cl.save(cfg, config_path)
        hot_reload(registry, holders, cfg, payload["backend"], log)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def reset_tool(request: Request):
        """Clear all overrides for one tool (revert to the backend default)."""
        payload = await request.json()
        cfg = _load()
        b = next((x for x in cfg.backends if x.name == payload["backend"]), None)
        if b is None:
            return JSONResponse(
                {"ok": False, "error": "unknown backend"}, status_code=400
            )
        b.tools = [t for t in b.tools if t.original != payload["tool_original"]]
        backup_config(config_path)
        cl.save(cfg, config_path)
        hot_reload(registry, holders, cfg, payload["backend"], log)
        return JSONResponse({"ok": True})

    async def put_instructions(request: Request):
        """Set a per-backend server-instructions override (``backend`` = name).
        Hot-reloads that backend's endpoint — it only changes the blurb Claude
        reads at initialize, no connection rebuild."""
        payload = await request.json()
        cfg = _load()
        backend = payload.get("backend")
        try:
            set_instructions(cfg, backend, payload.get("value"))
        except (cl.ConfigError, KeyError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        backup_config(config_path)
        cl.save(cfg, config_path)
        if backend is not None:
            hot_reload(registry, holders, cfg, backend, log)
        return JSONResponse({"ok": True})

    async def pin_backend(request: Request):
        """Toggle per-backend always_load (pin all its tools upfront). Hot-reload —
        it only adds `_meta`, no connection change."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = _load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return JSONResponse(
                {"ok": False, "error": "unknown backend"}, status_code=400
            )
        b.always_load = bool(payload.get("value", False))
        backup_config(config_path)
        cl.save(cfg, config_path)
        hot_reload(registry, holders, cfg, name, log)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def enable_backend(request: Request):
        """Toggle a backend's broadcast switch (#38). Hot-reload — every tool's
        enabled state flips in-process; the endpoint stays mounted, no restart."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = _load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return JSONResponse(
                {"ok": False, "error": "unknown backend"}, status_code=400
            )
        b.enabled = bool(payload.get("value", True))
        backup_config(config_path)
        cl.save(cfg, config_path)
        hot_reload(registry, holders, cfg, name, log)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def enable_all(request: Request):
        """Master switch (#40): set every backend's enabled, then hot-reload each.
        No topology change, so it's in-process like a per-backend toggle."""
        payload = await request.json()
        value = bool(payload.get("value", True))
        cfg = _load()
        for b in cfg.backends:
            b.enabled = value
        backup_config(config_path)
        cl.save(cfg, config_path)
        for b in cfg.backends:
            hot_reload(registry, holders, cfg, b.name, log)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def set_display_name(request: Request):
        """Set a backend's display-only name (#42). Purely cosmetic — routing,
        endpoint URL, config keys and Claude Code registration all stay ``name`` —
        so there's no hot-reload; empty clears it (falls back to ``name``)."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = _load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return JSONResponse(
                {"ok": False, "error": "unknown backend"}, status_code=400
            )
        val = (payload.get("value") or "").strip()
        b.display_name = val or None
        backup_config(config_path)
        cl.save(cfg, config_path)
        return JSONResponse({"ok": True})

    async def add_backend(request: Request):
        """Import a new backend MCP. Validates + introspects, then restarts."""
        payload = await request.json()
        cfg = _load()
        if any(b.name == payload.get("name") for b in cfg.backends):
            return JSONResponse(
                {"ok": False, "error": "backend name already exists"}, status_code=400
            )
        try:
            b = Backend(
                name=payload["name"],
                transport=payload["transport"],
                url=_clean(payload.get("url")),
                command=_clean(payload.get("command")),
                args=payload.get("args") or [],
                auth_header=_clean(payload.get("auth_header")),
                auth_value=_clean(payload.get("auth_value")),
                stateless=bool(payload.get("stateless", False)),
            )
        except Exception as exc:  # noqa: BLE001 (pydantic/validation)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        # Probe + capture defaults before committing it to config.
        try:
            save_defaults(await capture_defaults(b))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": f"could not connect to backend: {exc}"},
                status_code=400,
            )
        cfg.backends.append(b)
        backup_config(config_path)
        cl.save(cfg, config_path)
        return _restart_response({"backend": b.name})

    async def remove_backend(request: Request):
        name = request.path_params["name"]
        cfg = _load()
        before = len(cfg.backends)
        cfg.backends = [b for b in cfg.backends if b.name != name]
        if len(cfg.backends) == before:
            return JSONResponse(
                {"ok": False, "error": "unknown backend"}, status_code=400
            )
        backup_config(config_path)
        cl.save(cfg, config_path)
        return _restart_response({})

    async def restart_gateway(_request: Request):
        """Manual on-demand restart of the daemon (#56). Same launchd-gated
        semantics as a topology change: restarts when managed, honest no-op in
        dev/foreground."""
        return _restart_response({})

    async def reintrospect(request: Request):
        name = request.path_params["name"]
        cfg = _load()
        await ensure_defaults(cfg, log, force=name)
        return JSONResponse({"ok": True})

    app.router.routes.extend(
        [
            Route("/admin", admin_page, methods=["GET"]),
            Route("/admin/api/state", get_state, methods=["GET"]),
            Route("/admin/api/override", _needs_json(put_override), methods=["PUT"]),
            Route("/admin/api/reset", _needs_json(reset_tool), methods=["POST"]),
            Route(
                "/admin/api/instructions",
                _needs_json(put_instructions),
                methods=["PUT"],
            ),
            Route(
                "/admin/api/backend/{name}/pin",
                _needs_json(pin_backend),
                methods=["POST"],
            ),
            Route(
                "/admin/api/backend/{name}/enabled",
                _needs_json(enable_backend),
                methods=["POST"],
            ),
            Route("/admin/api/enabled", _needs_json(enable_all), methods=["POST"]),
            Route(
                "/admin/api/backend/{name}/display-name",
                _needs_json(set_display_name),
                methods=["POST"],
            ),
            Route("/admin/api/backend", _needs_json(add_backend), methods=["POST"]),
            Route("/admin/api/backend/{name}", remove_backend, methods=["DELETE"]),
            Route("/admin/api/restart", restart_gateway, methods=["POST"]),
            Route("/admin/api/introspect/{name}", reintrospect, methods=["POST"]),
        ]
    )
