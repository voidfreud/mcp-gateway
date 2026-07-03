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

import asyncio
import functools
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

# Upper bound on a single backend introspection probe (connect + list_tools).
# A hung/slow backend must not block daemon startup or an admin import (#85).
# Generous enough for a cold stdio backend (e.g. gitnexus' ~13s re-index).
CAPTURE_TIMEOUT = 30.0


@functools.cache
def gateway_version() -> str:
    """The gateway's own version, from a single source (package metadata, else
    the ``version = "..."`` line in pyproject.toml). Surfaced in the admin UI and
    ``/health`` so the running build is visible after a restart/upgrade (#57).

    Cached: the version is constant for a process, so we don't re-read pyproject
    on every /health and /admin/api/state request (#79)."""
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
    # Bound the probe so a hung backend can't block startup or an import (#85).
    async with asyncio.timeout(CAPTURE_TIMEOUT):
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


def all_meta_from_defaults(cfg: GatewayConfig) -> dict[str, dict[str, dict]]:
    """``backend name -> {original tool name -> its captured _meta}`` from
    defaults. Lets build_transforms MERGE a pin's alwaysLoad flag into the
    backend's original ``_meta`` instead of replacing it, so reserved keys like
    ``io.modelcontextprotocol/related-task`` survive a pin (#91)."""
    out: dict[str, dict[str, dict]] = {}
    for b in cfg.backends:
        d = load_defaults(b.name)
        if not d:
            continue
        metas = {t["original"]: t["meta"] for t in d.get("tools", []) if t.get("meta")}
        if metas:
            out[b.name] = metas
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


def effective_tools(cfg: GatewayConfig, backend: str | None = None) -> list[dict]:
    """Every ENABLED tool's effective broadcast (name, description), computed from
    defaults + overrides. Used to detect collisions — Claude can't tell two tools
    apart if they share a broadcast name. Pass *backend* to scope to ONE backend
    (collisions are per-endpoint now, so a save only needs that backend's tools) —
    avoids reading every backend's defaults on each save (#79)."""
    out: list[dict] = []
    for b in cfg.backends:
        if backend is not None and b.name != backend:
            continue
        defaults = load_defaults(b.name) or {}
        for dt in defaults.get("tools", []):
            orig = dt["original"]
            ov = _find_tool_override(b, orig)
            if ov is not None and not ov.enabled:
                continue  # disabled -> not broadcast -> can't collide
            name = ov.name if (ov and ov.name) else orig
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
    Passthrough never collides (bare original names are unique within a backend),
    so this only fires on a real rename/description clash."""
    if not new.enabled:
        return  # not broadcast
    eff_name = new.name or original
    for other in effective_tools(cfg, backend):  # #79: only this backend's tools
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
                    "default_name": dt["original"],
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


def _validate_text(v: str, what: str = "text") -> None:
    """Reject text that can't be serialized to the config file.

    A lone UTF-16 surrogate (e.g. ``\\ud83d`` with no pair) arrives via the JSON
    API — ``json.loads`` accepts it — but ``config_loader.save`` raises
    ``UnicodeEncodeError`` when it writes the file as UTF-8, which would surface
    as a 500 + traceback. Reject it at the mutation boundary with a clean 400
    (issue #95), consistent with the #48/#49 "bad admin input -> 400" hardening.
    """
    try:
        v.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise cl.ConfigError(
            f"invalid {what}: contains characters that can't be encoded "
            f"(unpaired surrogate at position {exc.start})"
        ) from exc


def _clean(v):
    if isinstance(v, str):
        v = v.strip()
        if v:
            _validate_text(v)
        return v or None
    return v


_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Claude Code truncates each MCP server's `instructions` at ~2KB (issue #29). The
# admin UI shows a 2048-byte counter; enforce the same cap server-side so an
# over-cap blurb is rejected (400) rather than silently truncated by Claude Code —
# a byte-boundary truncation could split a multibyte char (issue #93).
INSTRUCTIONS_MAX_BYTES = 2048


def _validate_name(name: str | None, what: str) -> None:
    """MCP-safe identifier guard. Conservative ([A-Za-z0-9_-], max 64 chars —
    #41) so an edited name can't break the tool listing or `mcp__server__tool`
    resolution."""
    if name is not None and not _NAME_RE.match(name):
        raise cl.ConfigError(
            f"invalid {what} {name!r}: use only letters, digits, '_' or '-' "
            f"(max 64 chars)"
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
    """Upsert one tool's override from a UI payload, diffing against defaults.

    Every editable field arrives prefilled with its effective value; we only
    store a value that actually differs from the backend's default.

    Merge semantics (#139): a key ABSENT from the payload preserves the stored
    override's value instead of resetting it to the default — so a scripted
    partial PUT (e.g. description only) can't silently resurrect a tool the UI
    disabled. The UI always sends every field, so it is unaffected; to reset a
    field explicitly, send its default value.
    """
    b = next((x for x in cfg.backends if x.name == backend), None)
    if b is None:
        raise cl.ConfigError(f"unknown backend {backend!r}")
    original = payload["tool_original"]
    ov = payload.get("override", {})
    prev = next((t for t in b.tools if t.original == original), None)

    def _field(key, computed, kept):
        return computed() if key in ov else kept

    # Defaults for this tool (original broadcast captured at introspection).
    defaults = load_defaults(backend) or {}
    dtool = next(
        (t for t in defaults.get("tools", []) if t["original"] == original), {}
    )
    default_name = original
    dparams = {p["original"]: p for p in dtool.get("params", [])}

    name = _field(
        "name",
        lambda: _override_vs_default(ov.get("name"), default_name),
        prev.name if prev else None,
    )
    title = _field(
        "title",
        lambda: _override_vs_default(ov.get("title"), dtool.get("title")),
        prev.title if prev else None,
    )
    description = _field(
        "description",
        lambda: _override_vs_default(ov.get("description"), dtool.get("description")),
        prev.description if prev else None,
    )
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

    if "params" not in ov and prev is not None:
        params = prev.params
    enabled = _field(
        "enabled", lambda: bool(ov["enabled"]), prev.enabled if prev else True
    )
    always_load = _field(
        "always_load",
        lambda: bool(ov["always_load"]),
        prev.always_load if prev else False,
    )
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
    override = _override_vs_default(value, default)
    if override is not None:
        n = len(override.encode("utf-8"))
        if n > INSTRUCTIONS_MAX_BYTES:
            raise cl.ConfigError(
                f"instructions are {n} bytes; the cap is {INSTRUCTIONS_MAX_BYTES} "
                f"(Claude Code truncates beyond ~2KB) — shorten them"
            )
    b.instructions = override


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
    # #79: read THIS backend's defaults once (not every backend's) and build the
    # single-entry maps that build_transforms / backend_instructions consume — a
    # hot-reload only concerns one backend, so enable_all drops from O(N^2) to O(N).
    d = load_defaults(backend) or {}
    tools = [t["original"] for t in d.get("tools", [])]
    metas = {t["original"]: t["meta"] for t in d.get("tools", []) if t.get("meta")}
    new_transform, _index = cl.build_transforms(
        cfg, b, {backend: tools}, {backend: metas} if metas else {}
    )
    holder = holders.get(backend) or []
    old = holder[0] if holder else None
    if old is not None and old in proxy._transforms:
        proxy._transforms.remove(old)
    proxy.add_transform(new_transform)
    holders[backend] = [new_transform]
    # Re-set this backend's live server-level instructions (override else captured
    # original) — each endpoint carries only its own, keeping the full per-server
    # budget.
    proxy.instructions = cl.backend_instructions(b, {backend: d.get("instructions")})
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


def register(
    app,
    config_path: str,
    log,
    registry: dict,
    holders: dict,
    hooks: dict | None = None,
) -> None:
    """Attach the admin UI + API routes to the parent Starlette *app*.

    ``registry`` (backend name -> live proxy) and ``holders`` (backend name ->
    [current transform]) are populated during the server lifespan and shared by
    reference, so hot-reload targets the right backend's live proxy. ``hooks``
    is likewise filled by the lifespan: ``hooks["add"]`` mounts a just-imported
    backend live (#7) so an import needs no daemon restart.
    """
    hooks = hooks if hooks is not None else {}

    def _load() -> GatewayConfig:
        return cl.load(config_path)

    # Guards every config read-modify-write (#52). Uvicorn runs a single worker,
    # so load->mutate->save WAS implicitly atomic as long as no handler awaited
    # between load and save — a fragile invariant (add_backend already awaits a
    # network probe mid-section). The explicit lock makes it safe to add an
    # `await` inside a critical section without silently losing concurrent edits.
    config_lock = asyncio.Lock()

    def _locked(handler):
        """Serialize a whole mutating handler under ``config_lock``. Fine for the
        in-process handlers (they only parse JSON + touch local files);
        add_backend manages the lock itself so its network probe stays outside."""

        async def inner(request: Request):
            async with config_lock:
                return await handler(request)

        return inner

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

    async def _apply_enabled(b: Backend, value: bool) -> None:
        """Bring one backend's live mount in line with its enabled flag (#78):
        enable -> mount it if not already mounted; disable -> unmount it (so a
        disabled backend runs no subprocess and its endpoint 404s)."""
        if value:
            if b.name not in registry:  # not mounted -> mount it live (#7 path)
                add = hooks.get("add")
                if add is not None:
                    await add(b)
            else:  # already mounted (defensive) -> just refresh its transforms
                hot_reload(registry, holders, _load(), b.name, log)
        else:
            remove = hooks.get("remove")
            if remove is not None:
                remove(b.name)

    async def enable_backend(request: Request):
        """Enable/disable a backend (#38). Enable MOUNTS it live; disable UNMOUNTS
        it (#78) — no subprocess and a 404 endpoint while disabled — until it's
        re-enabled. No daemon restart either way."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = _load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return JSONResponse(
                {"ok": False, "error": "unknown backend"}, status_code=400
            )
        value = bool(payload.get("value", True))
        b.enabled = value
        backup_config(config_path)
        cl.save(cfg, config_path)
        await _apply_enabled(b, value)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def enable_all(request: Request):
        """Master switch (#40): enable/disable every backend, mounting or
        unmounting each to match (#78)."""
        payload = await request.json()
        value = bool(payload.get("value", True))
        cfg = _load()
        for b in cfg.backends:
            b.enabled = value
        backup_config(config_path)
        cl.save(cfg, config_path)
        for b in cfg.backends:
            await _apply_enabled(b, value)
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
        try:
            b.display_name = _clean(payload.get("value"))  # validates encodability
        except cl.ConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        backup_config(config_path)
        cl.save(cfg, config_path)
        return JSONResponse({"ok": True})

    async def add_backend(request: Request):
        """Import a new backend MCP. Validates + introspects, then restarts."""
        payload = await request.json()
        if any(b.name == payload.get("name") for b in _load().backends):
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
                headers=payload.get("headers") or {},
                auth=_clean(payload.get("auth")),
                headers_helper=_clean(payload.get("headers_helper")),
                stateless=bool(payload.get("stateless", False)),
            )
        except Exception as exc:  # noqa: BLE001 (pydantic/validation)
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        # Probe + capture defaults before committing it to config — and BEFORE
        # taking config_lock, so a slow backend can't block other admin edits.
        try:
            save_defaults(await capture_defaults(b))
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": f"could not connect to backend: {exc}"},
                status_code=400,
            )
        async with config_lock:
            cfg = _load()  # re-load + re-check: the probe await is a real gap
            if any(x.name == b.name for x in cfg.backends):
                return JSONResponse(
                    {"ok": False, "error": "backend name already exists"},
                    status_code=400,
                )
            cfg.backends.append(b)
            backup_config(config_path)
            cl.save(cfg, config_path)
        # Hot-add (#7): mount the new backend into the RUNNING daemon — no
        # restart, no /health polling race. Config is already saved either way,
        # so a failed mount still lands the backend on the next real restart.
        hot_add = hooks.get("add")
        if hot_add is not None:
            if await hot_add(b):
                log.info("backend_hot_added", backend=b.name)
                return JSONResponse(
                    {"ok": True, "reloaded": "hot-add", "backend": b.name}
                )
            return JSONResponse(
                {"ok": True, "reloaded": "mount-failed", "backend": b.name}
            )
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
        # prune the captured defaults so removed backends don't accumulate
        # orphaned files (#54); best-effort — the file may never have existed
        (DEFAULTS_DIR / f"{name}.json").unlink(missing_ok=True)
        return _restart_response({})

    async def restart_gateway(_request: Request):
        """Manual on-demand restart of the daemon (#56). Same launchd-gated
        semantics as a topology change: restarts when managed, honest no-op in
        dev/foreground."""
        return _restart_response({})

    async def run_tool(request: Request):
        """Mini-Inspector (#3): execute one tool through the LIVE proxy — the
        same path Claude uses, so renames/transforms apply and reverse-map —
        and return structured + unstructured content + error state. Read-only
        w.r.t. config, so no config_lock; call_tool_mcp doesn't raise on a
        tool-level error (isError comes back in the payload)."""
        payload = await request.json()
        backend = payload.get("backend")
        proxy = registry.get(backend)
        if proxy is None:
            return JSONResponse(
                {"ok": False, "error": "backend not mounted"}, status_code=400
            )
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool:
            return JSONResponse(
                {"ok": False, "error": "missing or invalid tool (must be a string)"},
                status_code=400,
            )
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            return JSONResponse(
                {"ok": False, "error": "args must be an object"}, status_code=400
            )
        started = time.perf_counter()
        try:
            async with Client(proxy) as c:
                res = await c.call_tool_mcp(tool, args, timeout=60)
        except Exception as exc:  # noqa: BLE001 — surface, don't 500
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=502,
            )
        return JSONResponse(
            {
                "ok": True,
                "is_error": bool(res.isError),
                "ms": round((time.perf_counter() - started) * 1000, 1),
                "content": [
                    blk.model_dump(mode="json", exclude_none=True)
                    for blk in (res.content or [])
                ],
                "structured": res.structuredContent,
            }
        )

    async def reintrospect(request: Request):
        name = request.path_params["name"]
        cfg = _load()
        await ensure_defaults(cfg, log, force=name)
        return JSONResponse({"ok": True})

    app.router.routes.extend(
        [
            Route("/admin", admin_page, methods=["GET"]),
            Route("/admin/api/state", get_state, methods=["GET"]),
            Route(
                "/admin/api/override",
                _needs_json(_locked(put_override)),
                methods=["PUT"],
            ),
            Route(
                "/admin/api/reset", _needs_json(_locked(reset_tool)), methods=["POST"]
            ),
            Route(
                "/admin/api/instructions",
                _needs_json(_locked(put_instructions)),
                methods=["PUT"],
            ),
            Route(
                "/admin/api/backend/{name}/pin",
                _needs_json(_locked(pin_backend)),
                methods=["POST"],
            ),
            Route(
                "/admin/api/backend/{name}/enabled",
                _needs_json(_locked(enable_backend)),
                methods=["POST"],
            ),
            Route(
                "/admin/api/enabled", _needs_json(_locked(enable_all)), methods=["POST"]
            ),
            Route(
                "/admin/api/backend/{name}/display-name",
                _needs_json(_locked(set_display_name)),
                methods=["POST"],
            ),
            # add_backend takes config_lock itself (probe stays outside the lock)
            Route("/admin/api/backend", _needs_json(add_backend), methods=["POST"]),
            Route(
                "/admin/api/backend/{name}", _locked(remove_backend), methods=["DELETE"]
            ),
            Route("/admin/api/run", _needs_json(run_tool), methods=["POST"]),
            Route("/admin/api/restart", restart_gateway, methods=["POST"]),
            Route("/admin/api/introspect/{name}", reintrospect, methods=["POST"]),
        ]
    )
