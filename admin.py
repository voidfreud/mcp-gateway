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
    """Connect to one backend and return its original broadcast as a baseline."""
    async with Client(_client_config(b)) as c:
        tools = await c.list_tools()
    out_tools = []
    for t in tools:
        schema = t.inputSchema or {}
        props = schema.get("properties", {})
        params = [
            {"original": name, "description": (spec or {}).get("description")}
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
    return {"backend": b.name, "captured_at": time.time(), "tools": out_tools}


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
    """Capture defaults for any backend missing them (or *force* one by name)."""
    for b in cfg.backends:
        if force and b.name != force:
            continue
        if not force and load_defaults(b.name) is not None:
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
                "introspected": defaults is not None,
                "tools": tools_state,
            }
        )
    return {"host": cfg.host, "port": cfg.port, "backends": backends}


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
        if pname or pdesc or hide:
            params.append(
                ParamOverride(original=po, name=pname, description=pdesc, hide=hide)
            )

    enabled = bool(ov.get("enabled", True))
    new = ToolOverride(
        original=original,
        name=name,
        title=title,
        description=description,
        enabled=enabled,
        params=params,
    )
    has_override = bool(name or title or description or not enabled or params)
    b.tools = [t for t in b.tools if t.original != original]
    if has_override:
        b.tools.append(new)


# ---------------------------------------------------------------------------
# Hot reload (text) and restart (backend topology)
# ---------------------------------------------------------------------------


def hot_reload(mcp, cfg: GatewayConfig, holder: list, log) -> None:
    """Rebuild transforms from cfg and swap them into the live proxy in-process."""
    new_transform, _index = cl.build_transforms(cfg)
    old = holder[0] if holder else None
    if old is not None and old in mcp._transforms:
        mcp._transforms.remove(old)
    mcp.add_transform(new_transform)
    if holder:
        holder[0] = new_transform
    else:
        holder.append(new_transform)
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
