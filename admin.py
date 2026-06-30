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

import json
import os
import re
import subprocess
import time
from pathlib import Path

from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse

from fastmcp import Client

import config_loader as cl
from config_loader import Backend, GatewayConfig, ParamOverride, ToolOverride

STATE_DIR = Path("~/.local/state/mcp-gateway").expanduser()
DEFAULTS_DIR = STATE_DIR / "defaults"
BACKUP_DIR = STATE_DIR / "backups"
HERE = Path(__file__).resolve().parent
LAUNCHD_LABEL = "com.void.mcp-gateway"


# ---------------------------------------------------------------------------
# Introspection — capture a backend's original broadcast (the "default")
# ---------------------------------------------------------------------------


def _client_config(b: Backend) -> dict:
    """Single-backend client config so tool names come back un-prefixed (bare)."""
    if b.transport == "http":
        entry: dict = {"url": cl.expand_env(b.url or ""), "transport": "http"}
        if b.auth_header and b.auth_value:
            entry["headers"] = {b.auth_header: cl.expand_env(b.auth_value)}
    else:
        entry = {"command": b.command, "args": list(b.args), "transport": "stdio"}
        if b.env:
            entry["env"] = {k: cl.expand_env(v) for k, v in b.env.items()}
    return {"mcpServers": {b.name: entry}}


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
        out_tools.append(
            {
                "original": t.name,
                "title": getattr(t, "title", None),
                "description": t.description,
                "params": params,
            }
        )
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
    Feeds :func:`config_loader.compose_instructions` so the gateway can hand each
    backend's always-loaded blurb back to Claude (the proxy otherwise drops it)."""
    out: dict[str, str | None] = {}
    for b in cfg.backends:
        d = load_defaults(b.name)
        out[b.name] = (d or {}).get("instructions")
    return out


def apply_instructions(mcp, cfg: GatewayConfig) -> str | None:
    """Compose and set the gateway's live server-level ``instructions``.
    Returns the composed value (for logging). Read fresh per ``initialize``."""
    composed = cl.compose_instructions(cfg, captured_instructions(cfg))
    mcp.instructions = composed
    return composed


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
        if other["backend"] == backend and other["original"] == original:
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
                    "params": params,
                }
            )
        backends.append(
            {
                "name": b.name,
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
        # gateway-level instructions: the manual override (None = auto-compose)
        # and the composed value actually broadcast to Claude (read-only preview).
        "instructions": cfg.instructions,
        "composed_instructions": cl.compose_instructions(
            cfg, captured_instructions(cfg)
        ),
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


def set_instructions(cfg: GatewayConfig, backend: str | None, value) -> None:
    """Set the gateway-level (``backend is None``) or a per-backend instructions
    override from a UI value.

    * Per-backend: diff against the captured original — empty or equal-to-default
      inherits (stores nothing), so config stays minimal; a different value (incl.
      one added where the backend sends none) is stored.
    * Gateway: empty inherits (``None`` -> auto-compose from backends); any value
      becomes the full manual override. No diff-vs-composed (the composed output
      shifts with the backends, so freezing it on a match would surprise).
    """
    if backend is None:
        cfg.instructions = _clean(value)
        return
    b = next((x for x in cfg.backends if x.name == backend), None)
    if b is None:
        raise cl.ConfigError(f"unknown backend {backend!r}")
    default = (load_defaults(backend) or {}).get("instructions")
    b.instructions = _override_vs_default(value, default)


def hot_reload(mcp, cfg: GatewayConfig, holder: list, log) -> None:
    """Rebuild transforms from cfg and swap them into the live proxy in-process."""
    new_transform, _index = cl.build_transforms(cfg, all_tools_from_defaults(cfg))
    old = holder[0] if holder else None
    if old is not None and old in mcp._transforms:
        mcp._transforms.remove(old)
    mcp.add_transform(new_transform)
    if holder:
        holder[0] = new_transform
    else:
        holder.append(new_transform)
    # Re-compose + set the gateway's server instructions (read per initialize).
    apply_instructions(mcp, cfg)
    # The change is live in the gateway immediately (next tools/list reflects it).
    # FastMCP exposes no tools/list_changed helper, so an already-connected Claude
    # session refreshes on its next list / reconnect / new session.
    log.info("hot_reload", backends=[b.name for b in cfg.backends])


def restart_daemon(log) -> None:
    """Restart the launchd service (for backend topology changes).

    Run as a Starlette BackgroundTask, i.e. AFTER the HTTP response has flushed,
    so no `sleep` shell is needed. ``subprocess.run`` waits on (reaps) the child,
    so it never leaves a zombie; in production launchd then kills+restarts us.
    No-op (logs a warning) if not running under launchd (dev/foreground).
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


def register(mcp, config_path: str, log, holder: list) -> None:
    """Attach the admin UI + API routes to the FastMCP server's HTTP app."""

    def _load() -> GatewayConfig:
        return cl.load(config_path)

    @mcp.custom_route("/admin", methods=["GET"])
    async def admin_page(_request: Request):
        return FileResponse(HERE / "admin.html")

    @mcp.custom_route("/admin/api/state", methods=["GET"])
    async def get_state(_request: Request):
        return JSONResponse(build_state(_load()))

    @mcp.custom_route("/admin/api/override", methods=["PUT"])
    async def put_override(request: Request):
        payload = await request.json()
        cfg = _load()
        try:
            apply_tool_override(cfg, payload["backend"], payload)
        except (cl.ConfigError, KeyError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        backup_config(config_path)
        cl.save(cfg, config_path)
        hot_reload(mcp, cfg, holder, log)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    @mcp.custom_route("/admin/api/reset", methods=["POST"])
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
        hot_reload(mcp, cfg, holder, log)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/admin/api/instructions", methods=["PUT"])
    async def put_instructions(request: Request):
        """Set the gateway-level (``backend`` null/absent) or a per-backend
        server-instructions override. Hot-reloads — it only changes the blurb
        Claude reads at initialize, no connection rebuild."""
        payload = await request.json()
        cfg = _load()
        try:
            set_instructions(cfg, payload.get("backend"), payload.get("value"))
        except (cl.ConfigError, KeyError) as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        backup_config(config_path)
        cl.save(cfg, config_path)
        hot_reload(mcp, cfg, holder, log)
        composed = cl.compose_instructions(cfg, captured_instructions(cfg))
        return JSONResponse({"ok": True, "composed": composed})

    @mcp.custom_route("/admin/api/backend/{name}/pin", methods=["POST"])
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
        hot_reload(mcp, cfg, holder, log)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    @mcp.custom_route("/admin/api/backend", methods=["POST"])
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
        return JSONResponse(
            {"ok": True, "reloaded": "restarting", "backend": b.name},
            background=BackgroundTask(restart_daemon, log),
        )

    @mcp.custom_route("/admin/api/backend/{name}", methods=["DELETE"])
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
        return JSONResponse(
            {"ok": True, "reloaded": "restarting"},
            background=BackgroundTask(restart_daemon, log),
        )

    @mcp.custom_route("/admin/api/introspect/{name}", methods=["POST"])
    async def reintrospect(request: Request):
        name = request.path_params["name"]
        cfg = _load()
        await ensure_defaults(cfg, log, force=name)
        return JSONResponse({"ok": True})
