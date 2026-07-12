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
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastmcp import Client
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

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

# #43: minimum gap between automatic re-introspections of one backend, so
# reconnect storms / rapid dashboard reloads can't hammer a backend. Manual
# Re-inspect (force=True) bypasses it; a tools/list_changed push uses a short
# floor instead (LIST_CHANGED_THROTTLE) since the backend itself asked.
REFRESH_THROTTLE = 300.0
LIST_CHANGED_THROTTLE = 2.0

# #23: upper bound on one backend's liveness probe for /admin/api/status.
STATUS_TIMEOUT = 5.0

# #43: per-backend timestamp of the last (attempted) auto-refresh. In-process
# only — resets on restart, which is exactly right: a fresh daemon means fresh
# backend connections, whose baselines should re-capture once.
_last_refresh: dict[str, float] = {}


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
            # stamp the #43 throttle: a just-captured baseline is fresh, the
            # post-mount auto-refresh shouldn't immediately re-capture it
            _last_refresh[b.name] = time.monotonic()
            log.info("defaults_captured", backend=b.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("defaults_capture_failed", backend=b.name, error=str(exc))


async def refresh_defaults(
    b: Backend, log, *, force: bool = False, throttle: float = REFRESH_THROTTLE
) -> dict:
    """Re-capture ONE backend's baseline, throttled (#43). Returns a result
    dict: ``{"status": "refreshed"|"throttled"|"error", ...}``; on refresh it
    carries ``added``/``removed`` (original tool names vs the previous baseline)
    and ``changed`` (tools OR server instructions differ).

    Override safety: this only rewrites the immutable baseline; user overrides
    are stored separately as diffs and merged by original name, so a refresh
    never clobbers edits — new tools appear un-overridden, removed tools drop.

    The throttle stamp is set BEFORE the capture await (storms coalesce) and
    kept on failure (a down backend is retried at the throttle cadence, not on
    every trigger).
    """
    now = time.monotonic()
    last = _last_refresh.get(b.name)
    if not force and last is not None and now - last < throttle:
        return {"status": "throttled"}
    _last_refresh[b.name] = now
    old = load_defaults(b.name)
    try:
        data = await capture_defaults(b)
    except Exception as exc:  # noqa: BLE001 — a down backend is a result, not a crash
        log.warning("defaults_refresh_failed", backend=b.name, error=str(exc))
        return {"status": "error", "error": str(exc)}
    save_defaults(data)
    old_names = {t["original"] for t in (old or {}).get("tools", [])}
    new_names = {t["original"] for t in data.get("tools", [])}
    added = sorted(new_names - old_names)
    removed = sorted(old_names - new_names)
    changed = bool(
        added or removed or (old or {}).get("instructions") != data.get("instructions")
    )
    if changed:
        log.info(
            "defaults_refreshed",
            backend=b.name,
            tools=len(new_names),
            added=added,
            removed=removed,
        )
    return {
        "status": "refreshed",
        "added": added,
        "removed": removed,
        "changed": changed,
        "tools": len(new_names),
    }


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
        captured = set()
        for dt in defaults.get("tools", []):
            orig = dt["original"]
            captured.add(orig)
            ov = _find_tool_override(b, orig)
            if ov is not None and not ov.enabled:
                continue  # disabled -> not broadcast -> can't collide
            name = ov.name if (ov and ov.name) else orig
            desc = ov.description if (ov and ov.description) else dt.get("description")
            out.append(
                {"backend": b.name, "original": orig, "name": name, "description": desc}
            )
        # DANGLING overrides — original absent from captured defaults (e.g. the
        # backend renamed the tool upstream and a #43 refresh moved the
        # baseline). They no longer broadcast, but their entries still land in
        # the transforms, and FastMCP rejects a duplicate transform TARGET name
        # at build time — an invisible-to-validation 500 landmine (found live
        # while migrating the openrouter drift). Their names count as taken.
        for ov in b.tools:
            if ov.original in captured or not ov.enabled:
                continue
            out.append(
                {
                    "backend": b.name,
                    "original": ov.original,
                    "name": ov.name or ov.original,
                    "description": ov.description,
                }
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


def uniquify_name(base: str, taken: set[str]) -> str:
    """Deterministically suffix *base* (``_2``, then ``_3``, …) until it is not
    in *taken* (#22). The result always satisfies the ``[A-Za-z0-9_-]{1,64}``
    name rule: when appending the suffix would overflow 64 chars, the base is
    trimmed so the suffix fits. Returns *base* unchanged when it doesn't
    collide."""
    if base not in taken:
        return base
    n = 2
    while True:
        suffix = f"_{n}"
        candidate = base[: 64 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        n += 1


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
                        # injected fixed value (#35) — None = no injection
                        "default": p.default if p else None,
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


def _clean_param_default(v, param: str):
    """Validate an injected param default (#35): scalars only, mirroring
    FastMCP ``ArgTransformConfig.default`` (str | int | float | bool). An empty
    string means "no default" (the UI's cleared field); anything non-scalar is
    a clean 400 instead of a pydantic 500 downstream."""
    if v is None:
        return None
    if isinstance(v, str):
        return _clean(v)
    if isinstance(v, (int, float, bool)):
        return v
    raise cl.ConfigError(
        f"parameter {param!r}: injected default must be a string, number, or "
        f"boolean (got {type(v).__name__})"
    )


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


def apply_tool_override(cfg: GatewayConfig, backend: str, payload: dict) -> str | None:
    """Upsert one tool's override from a UI payload, diffing against defaults.

    Every editable field arrives prefilled with its effective value; we only
    store a value that actually differs from the backend's default.

    Merge semantics (#139): a key ABSENT from the payload preserves the stored
    override's value instead of resetting it to the default — so a scripted
    partial PUT (e.g. description only) can't silently resurrect a tool the UI
    disabled. The UI always sends every field, so it is unaffected; to reset a
    field explicitly, send its default value.

    Returns the final broadcast name when the opt-in ``"on_collision":
    "uniquify"`` flag (payload top level, #22) auto-suffixed a colliding
    rename, else None (the strict-reject default behaviour is unchanged).
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
        default = _clean_param_default(p.get("default"), po)
        # Correctness guardrail (alongside check_no_collision): a param the
        # backend marks required can't be hidden UNLESS a fixed default is
        # injected (#35) — without one Claude could never supply it, so every
        # call would break.
        if hide and dp.get("required", False) and default is None:
            raise cl.ConfigError(
                f"parameter {po!r} is required by the backend — hiding it "
                f"would break the tool; set an injected default value to hide "
                f"it safely"
            )
        if pname or pdesc or hide or default is not None:
            params.append(
                ParamOverride(
                    original=po,
                    name=pname,
                    description=pdesc,
                    hide=hide,
                    default=default,
                )
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
    # Opt-in escape hatch (#22): `"on_collision": "uniquify"` at the payload
    # top level suffixes a colliding broadcast NAME (_2, _3, …) into uniqueness
    # instead of rejecting. Description collisions still reject below — a
    # duplicated description can't be "uniquified" into something meaningful.
    uniquified: str | None = None
    if payload.get("on_collision") == "uniquify" and new.enabled:
        eff_name = new.name or original
        taken = {
            t["name"]
            for t in effective_tools(cfg, backend)
            if t["original"] != original
        }
        # Also dodge DISABLED entries' target names: they don't broadcast, but
        # they still occupy a transform target (FastMCP rejects duplicates at
        # build time regardless of enabled), so a suffix landing on one would
        # bounce off the dry-build guard below.
        taken |= {
            t.name or t.original
            for t in b.tools
            if t.original != original and not t.enabled
        }
        if eff_name in taken:
            final = uniquify_name(eff_name, taken)
            # equal-to-default still inherits (config stays minimal)
            new.name = final if final != default_name else None
            uniquified = final
    has_override = bool(
        new.name or title or description or not enabled or always_load or params
    )
    # Reject a rename/description that would collide with another broadcast tool.
    check_no_collision(cfg, backend, original, new)
    b.tools = [t for t in b.tools if t.original != original]
    if has_override:
        b.tools.append(new)
    _reject_transform_landmines(cfg, b)
    return uniquified


def _reject_transform_landmines(cfg: GatewayConfig, b: Backend) -> None:
    """Transform-level dry run: FastMCP rejects duplicate transform TARGET
    names at build time — including combinations the broadcast-level collision
    check deliberately allows (a DISABLED entry, a dangling override for a tool
    the backend renamed away). Without this, the save persists, the very next
    hot-reload/mount raises, and the backend fails to mount on every boot — a
    config landmine (found live migrating the openrouter drift)."""
    try:
        cl.build_transforms(cfg, b)
    except ValueError as exc:
        raise cl.ConfigError(
            f"override rejected — it would break the tool transforms: {exc}. "
            f"If the clashing entry is a stale override for a tool the backend "
            f"no longer exposes, reset that tool first."
        ) from None


# ---------------------------------------------------------------------------
# Settings export / import (#136)
# ---------------------------------------------------------------------------

EXPORT_KIND = "mcp-gateway-settings"
EXPORT_VERSION = 1


def export_settings(  # noqa: PLR0912 — one branch per serialized override field
    cfg: GatewayConfig, full: bool = False
) -> dict:
    """The COMPLETE stored settings as one JSON-safe bundle: per-backend
    instructions override, display name, pin, and every tool/param override —
    exactly what config.toml stores beyond topology, so an import round-trips
    with zero loss. ``full`` adds each backend's captured defaults for context
    (read-only; import ignores them)."""
    backends: dict = {}
    for b in cfg.backends:
        entry: dict = {}
        if b.display_name:
            entry["display_name"] = b.display_name
        if b.always_load:
            entry["always_load"] = True
        if b.instructions is not None:
            entry["instructions"] = b.instructions
        tools: dict = {}
        for t in b.tools:
            td: dict = {}
            for key in ("name", "title", "description"):
                if getattr(t, key):
                    td[key] = getattr(t, key)
            if not t.enabled:
                td["enabled"] = False
            if t.always_load:
                td["always_load"] = True
            if t.params:
                td["params"] = [
                    {
                        "original": p.original,
                        **({"name": p.name} if p.name else {}),
                        **({"description": p.description} if p.description else {}),
                        **({"hide": True} if p.hide else {}),
                        **({"default": p.default} if p.default is not None else {}),
                    }
                    for p in t.params
                ]
            if td:
                tools[t.original] = td
        if tools:
            entry["tools"] = tools
        if entry or full:
            backends[b.name] = entry
        if full:
            entry["defaults"] = load_defaults(b.name)
    return {"kind": EXPORT_KIND, "version": EXPORT_VERSION, "backends": backends}


def import_settings(  # noqa: PLR0912 — one validation branch per bundle field
    cfg: GatewayConfig, bundle: dict, mode: str = "merge"
) -> tuple[list[str], list[str]]:
    """Apply an exported bundle onto *cfg*. Returns (affected_backends, errors).

    All-or-nothing contract is the CALLER's: mutate a throwaway cfg, and only
    persist it when errors is empty. Validation is the same path as single
    saves (apply_tool_override / set_instructions — collisions, charset, caps),
    with each failure reported per item.

    ``mode="replace"``: a backend named in the bundle is first reset to
    defaults (its stored overrides cleared), then the bundle applies — the
    result is exactly the bundle. ``mode="merge"``: the bundle applies on top;
    keys absent from a tool entry preserve stored values (#139 semantics).

    Backend topology (``enabled``, transport, auth) is deliberately NOT
    imported — this is a settings bundle, not a config replacement. Overrides
    for a currently-disabled backend import fine (stored, effective on enable).
    """
    if bundle.get("kind") not in (None, EXPORT_KIND):
        return [], [f"not a settings bundle (kind={bundle.get('kind')!r})"]
    if mode not in ("merge", "replace"):
        return [], [f"unknown mode {mode!r} (use merge or replace)"]
    errors: list[str] = []
    affected: list[str] = []
    for name, entry in (bundle.get("backends") or {}).items():
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            errors.append(f"{name}: backend not configured on this gateway")
            continue
        affected.append(name)
        if mode == "replace":
            b.tools = []
            b.instructions = None
            b.display_name = None
            b.always_load = False
        if "display_name" in entry:
            b.display_name = entry["display_name"] or None
        if "always_load" in entry:
            b.always_load = bool(entry["always_load"])
        if "instructions" in entry:
            try:
                set_instructions(cfg, name, entry["instructions"])
            except cl.ConfigError as exc:
                errors.append(f"{name}: instructions: {exc}")
        known = {t["original"] for t in (load_defaults(name) or {}).get("tools", [])}
        for original, td in (entry.get("tools") or {}).items():
            if known and original not in known:
                errors.append(f"{name}/{original}: tool unknown to this backend")
                continue
            try:
                apply_tool_override(
                    cfg, name, {"tool_original": original, "override": dict(td)}
                )
            except (cl.ConfigError, KeyError) as exc:
                errors.append(f"{name}/{original}: {exc}")
    return affected, errors


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


async def refresh_and_reload(  # noqa: PLR0913 — mirrors hot_reload's plumbing args
    b: Backend,
    config_path: str,
    registry: dict,
    holders: dict,
    log,
    *,
    force: bool = False,
    throttle: float = REFRESH_THROTTLE,
) -> dict:
    """Re-capture one backend's baseline and, if it changed, hot-reload its
    live transforms + instructions so pins/enabled reconcile with the fresh
    tool list (#43). The shared tail of every auto-refresh trigger (post-mount,
    tools/list_changed, dashboard load, interval, manual Re-inspect)."""
    res = await refresh_defaults(b, log, force=force, throttle=throttle)
    if res.get("changed"):
        cfg = cl.load(config_path)
        hot_reload(registry, holders, cfg, b.name, log)
    return res


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
            check=False,  # returncode is inspected below
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
            check=False,  # failure is logged, not raised (best-effort restart)
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
# Claude Code registration (#45) — via the `claude` CLI, never its config files
# ---------------------------------------------------------------------------

# `claude mcp add/remove` scopes (see `claude mcp add --help`).
CLAUDE_SCOPES = ("local", "user", "project")
# Upper bound on one CLI invocation; `claude mcp` is local bookkeeping, so a
# longer hang means a broken install — surface it, don't wait on it.
CLAUDE_CLI_TIMEOUT = 30


def claude_mcp_command(
    action: str,
    name: str,
    url: str | None = None,
    scope: str = "local",
    bearer_token: str | None = None,
) -> list[str]:
    """Argv to register/deregister ONE backend in Claude Code via the CLI.

    Registration name convention: ``gateway-<backend name>``; the endpoint is
    the backend's own gateway mount (``http://host:port/<name>/mcp``). Pure —
    builds the argv only (the route runs it), so the exact command is testable
    and surfaceable to the UI.

    *bearer_token* (#26 × #45): when the gateway requires a bearer token, a
    registration without it would 401 on every call — so `add` carries it via
    ``--header``, RESOLVED (the CLI stores the literal header in Claude's
    config; the route redacts it from anything echoed back to the browser).
    """
    if scope not in CLAUDE_SCOPES:
        raise cl.ConfigError(
            f"invalid scope {scope!r}: use one of {', '.join(CLAUDE_SCOPES)}"
        )
    registration = f"gateway-{name}"
    if action == "add":
        if not url:
            raise cl.ConfigError("register needs the backend's endpoint url")
        argv = [
            "claude",
            "mcp",
            "add",
            "--transport",
            "http",
            "--scope",
            scope,
            registration,
            url,
        ]
        # --header is a VARIADIC option (like -e/--env): placed before the
        # positionals it swallows <name> <url> and the CLI errors with
        # "missing required argument 'name'" — found live (#123). The CLI's
        # own --help example puts --header last; do the same.
        if bearer_token:
            argv += ["--header", f"Authorization: Bearer {bearer_token}"]
        return argv
    if action == "remove":
        return ["claude", "mcp", "remove", "--scope", scope, registration]
    raise cl.ConfigError(f"unknown action {action!r} (use add or remove)")


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


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


@dataclass
class _AdminCtx:
    """Shared plumbing for the admin route groups (#89): config access, the
    read-modify-write lock, the live-proxy registry/holders, and the lifespan
    hooks. Route-group factories take this instead of a 13-handler closure."""

    config_path: str
    log: Any
    registry: dict
    holders: dict
    hooks: dict
    # Guards every config read-modify-write (#52). Uvicorn runs a single worker,
    # so load->mutate->save WAS implicitly atomic as long as no handler awaited
    # between load and save — a fragile invariant (add_backend already awaits a
    # network probe mid-section). The explicit lock makes it safe to add an
    # `await` inside a critical section without silently losing concurrent edits.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def load(self) -> GatewayConfig:
        return cl.load(self.config_path)

    def commit(self, cfg: GatewayConfig, *backends: str) -> None:
        """The shared tail of every mutating handler: backup, atomic save, then
        hot-reload each named backend that is currently mounted (hot_reload
        itself skips unmounted ones with a warning)."""
        backup_config(self.config_path)
        cl.save(cfg, self.config_path)
        for name in backends:
            hot_reload(self.registry, self.holders, cfg, name, self.log)

    def locked(self, handler):
        """Serialize a whole mutating handler under ``lock``. Fine for the
        in-process handlers (they only parse JSON + touch local files);
        add_backend manages the lock itself so its network probe stays outside."""

        async def inner(request: Request):
            async with self.lock:
                return await handler(request)

        return inner

    def restart_response(self, extra: dict) -> JSONResponse:
        """Response for a topology change that needs a full restart. Only
        schedules (and claims) the restart when we're actually launchd-managed;
        in dev/foreground it says so honestly instead of a stuck "restarting"
        (#53). Config is already written either way — it takes effect on the next
        real restart."""
        if under_launchd():
            return JSONResponse(
                {"ok": True, "reloaded": "restarting", **extra},
                background=BackgroundTask(restart_daemon, self.log),
            )
        return JSONResponse({"ok": True, "reloaded": "dev-no-restart", **extra})


def _general_routes(ctx: _AdminCtx) -> list[Route]:
    """Page, state, export, mini-inspector, restart, reintrospect."""

    async def admin_page(_request: Request):
        # no-cache so a plain browser reload always revalidates (ETag → 304 when
        # unchanged) and picks up admin.html edits. Without it the browser serves
        # a stale cached page and even a daemon restart doesn't refresh the UI.
        return FileResponse(HERE / "admin.html", headers={"Cache-Control": "no-cache"})

    async def get_state(_request: Request):
        return JSONResponse(build_state(ctx.load()))

    async def get_export(request: Request):
        """One-call settings bundle (#136): every stored override + instruction,
        JSON, zero loss on re-import. ?full=true adds captured defaults."""
        full = request.query_params.get("full") in ("true", "1")
        return JSONResponse(export_settings(ctx.load(), full=full))

    return [
        Route("/admin", admin_page, methods=["GET"]),
        Route("/admin/api/state", get_state, methods=["GET"]),
        Route("/admin/api/export", get_export, methods=["GET"]),
    ]


def _settings_routes(ctx: _AdminCtx) -> list[Route]:
    """Text overrides: tool override, reset, instructions, settings import."""

    async def put_override(request: Request):
        payload = await request.json()
        cfg = ctx.load()
        try:
            uniquified = apply_tool_override(cfg, payload["backend"], payload)
        except (cl.ConfigError, KeyError) as exc:
            return _err(str(exc))
        ctx.commit(cfg, payload["backend"])
        out: dict = {"ok": True, "reloaded": "in-process"}
        if uniquified is not None:
            # #22: the opt-in uniquify stored a suffixed name — hand the final
            # name back so the UI can reflect what actually shipped.
            out.update({"name": uniquified, "uniquified": True})
        return JSONResponse(out)

    async def reset_tool(request: Request):
        """Clear all overrides for one tool (revert to the backend default)."""
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == payload["backend"]), None)
        if b is None:
            return _err("unknown backend")
        b.tools = [t for t in b.tools if t.original != payload["tool_original"]]
        ctx.commit(cfg, payload["backend"])
        return JSONResponse({"ok": True})

    async def put_instructions(request: Request):
        """Set a per-backend server-instructions override (``backend`` = name).
        Hot-reloads that backend's endpoint — it only changes the blurb Claude
        reads at initialize, no connection rebuild."""
        payload = await request.json()
        cfg = ctx.load()
        backend = payload.get("backend")
        try:
            set_instructions(cfg, backend, payload.get("value"))
        except (cl.ConfigError, KeyError) as exc:
            return _err(str(exc))
        ctx.commit(cfg, backend)  # set_instructions validated the name
        return JSONResponse({"ok": True})

    async def post_import(request: Request):
        """Atomic settings import (#136): validate the whole bundle against a
        fresh cfg; persist and hot-reload only if EVERY item passes."""
        payload = await request.json()
        bundle = payload.get("settings") or payload
        mode = payload.get("mode", "merge")
        cfg = ctx.load()
        affected, errors = import_settings(cfg, bundle, mode)
        if errors:
            return JSONResponse(
                {"ok": False, "errors": errors, "applied": False}, status_code=400
            )
        # disabled backends: stored, effective on enable (commit skips unmounted)
        ctx.commit(cfg, *affected)
        return JSONResponse({"ok": True, "backends": affected, "mode": mode})

    return [
        Route(
            "/admin/api/override",
            _needs_json(ctx.locked(put_override)),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/reset", _needs_json(ctx.locked(reset_tool)), methods=["POST"]
        ),
        Route(
            "/admin/api/instructions",
            _needs_json(ctx.locked(put_instructions)),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/import",
            _needs_json(ctx.locked(post_import)),
            methods=["POST"],
        ),
    ]


def _backend_routes(ctx: _AdminCtx) -> list[Route]:  # noqa: PLR0915 — statement count is the nested handlers; the group is cohesive
    """Per-backend flags and topology: pin, enable, display name, rename,
    add, remove."""

    async def pin_backend(request: Request):
        """Toggle per-backend always_load (pin all its tools upfront). Hot-reload —
        it only adds `_meta`, no connection change."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return _err("unknown backend")
        b.always_load = bool(payload.get("value", False))
        ctx.commit(cfg, name)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def _apply_enabled(b: Backend, value: bool) -> None:
        """Bring one backend's live mount in line with its enabled flag (#78):
        enable -> mount it if not already mounted; disable -> unmount it (so a
        disabled backend runs no subprocess and its endpoint 404s)."""
        if value:
            if b.name not in ctx.registry:  # not mounted -> mount it live (#7)
                add = ctx.hooks.get("add")
                if add is not None:
                    await add(b)
            else:  # already mounted (defensive) -> just refresh its transforms
                hot_reload(ctx.registry, ctx.holders, ctx.load(), b.name, ctx.log)
        else:
            remove = ctx.hooks.get("remove")
            if remove is not None:
                remove(b.name)

    async def enable_backend(request: Request):
        """Enable/disable a backend (#38). Enable MOUNTS it live; disable UNMOUNTS
        it (#78) — no subprocess and a 404 endpoint while disabled — until it's
        re-enabled. No daemon restart either way."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return _err("unknown backend")
        value = bool(payload.get("value", True))
        b.enabled = value
        ctx.commit(cfg)
        await _apply_enabled(b, value)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def enable_all(request: Request):
        """Master switch (#40): enable/disable every backend, mounting or
        unmounting each to match (#78)."""
        payload = await request.json()
        value = bool(payload.get("value", True))
        cfg = ctx.load()
        for b in cfg.backends:
            b.enabled = value
        ctx.commit(cfg)
        for b in cfg.backends:
            await _apply_enabled(b, value)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def set_display_name(request: Request):
        """Set a backend's display-only name (#42). Purely cosmetic — routing,
        endpoint URL, config keys and Claude Code registration all stay ``name`` —
        so there's no hot-reload; empty clears it (falls back to ``name``)."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return _err("unknown backend")
        try:
            b.display_name = _clean(payload.get("value"))  # validates encodability
        except cl.ConfigError as exc:
            return _err(str(exc))
        ctx.commit(cfg)
        return JSONResponse({"ok": True})

    async def rename_backend(request: Request):
        """Hard-rename a backend (#44) — a REAL identity change, unlike the
        cosmetic display_name (#42). ``name`` drives the endpoint mount
        (``/{name}/mcp``), the Claude Code registration (``gateway-{name}``),
        the config key, and the captured-defaults file — all move together.
        Topology change → restart; the response carries old/new endpoint and
        registration so the UI can say exactly what to reconfigure in
        Claude Code."""
        name = request.path_params["name"]
        payload = await request.json()
        value = payload.get("value")
        new_name = value.strip() if isinstance(value, str) else ""
        if not _NAME_RE.match(new_name):
            return _err(
                f"invalid backend name {new_name!r}: use only letters, digits, "
                f"'_' or '-' (max 64 chars)"
            )
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return _err("unknown backend")
        if any(x.name == new_name for x in cfg.backends):
            return _err(
                f"backend name {new_name!r} already exists — pick a different one"
            )
        b.name = new_name  # display_name, tools, params — everything else rides
        # Topology change: commit WITHOUT a hot-reload arg (the endpoint itself
        # moves; the restart below rebuilds the mounts under the new name).
        ctx.commit(cfg)
        # Migrate the captured defaults (the immutable baseline) old → new so
        # overrides keep diffing against it; tolerate a never-introspected
        # backend (no file — the restart re-captures under the new name).
        old_defaults = DEFAULTS_DIR / f"{name}.json"
        if old_defaults.is_file():
            data = json.loads(old_defaults.read_text(encoding="utf-8"))
            data["backend"] = new_name
            save_defaults(data)
            old_defaults.unlink(missing_ok=True)
        base = f"http://{cfg.host}:{cfg.port}"
        return ctx.restart_response(
            {
                "backend": new_name,
                "old_endpoint": f"{base}/{name}/mcp",
                "new_endpoint": f"{base}/{new_name}/mcp",
                "old_registration": f"gateway-{name}",
                "new_registration": f"gateway-{new_name}",
            }
        )

    async def add_backend(request: Request):  # noqa: PLR0911 — one early return per validation/probe/mount outcome
        """Import a new backend MCP. Validates + introspects, then restarts."""
        payload = await request.json()
        if any(b.name == payload.get("name") for b in ctx.load().backends):
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
            _last_refresh[b.name] = time.monotonic()  # fresh — see #43 throttle
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(
                {"ok": False, "error": f"could not connect to backend: {exc}"},
                status_code=400,
            )
        async with ctx.lock:
            cfg = ctx.load()  # re-load + re-check: the probe await is a real gap
            if any(x.name == b.name for x in cfg.backends):
                return JSONResponse(
                    {"ok": False, "error": "backend name already exists"},
                    status_code=400,
                )
            cfg.backends.append(b)
            ctx.commit(cfg)
        # Hot-add (#7): mount the new backend into the RUNNING daemon — no
        # restart, no /health polling race. Config is already saved either way,
        # so a failed mount still lands the backend on the next real restart.
        hot_add = ctx.hooks.get("add")
        if hot_add is not None:
            if await hot_add(b):
                ctx.log.info("backend_hot_added", backend=b.name)
                return JSONResponse(
                    {"ok": True, "reloaded": "hot-add", "backend": b.name}
                )
            return JSONResponse(
                {"ok": True, "reloaded": "mount-failed", "backend": b.name}
            )
        return ctx.restart_response({"backend": b.name})

    async def remove_backend(request: Request):
        name = request.path_params["name"]
        cfg = ctx.load()
        before = len(cfg.backends)
        cfg.backends = [b for b in cfg.backends if b.name != name]
        if len(cfg.backends) == before:
            return _err("unknown backend")
        ctx.commit(cfg)
        # prune the captured defaults so removed backends don't accumulate
        # orphaned files (#54); best-effort — the file may never have existed
        (DEFAULTS_DIR / f"{name}.json").unlink(missing_ok=True)
        return ctx.restart_response({})

    return [
        Route(
            "/admin/api/backend/{name}/pin",
            _needs_json(ctx.locked(pin_backend)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/enabled",
            _needs_json(ctx.locked(enable_backend)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/enabled", _needs_json(ctx.locked(enable_all)), methods=["POST"]
        ),
        Route(
            "/admin/api/backend/{name}/display-name",
            _needs_json(ctx.locked(set_display_name)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/rename",
            _needs_json(ctx.locked(rename_backend)),
            methods=["POST"],
        ),
        # add_backend takes ctx.lock itself (probe stays outside the lock)
        Route("/admin/api/backend", _needs_json(add_backend), methods=["POST"]),
        Route(
            "/admin/api/backend/{name}", ctx.locked(remove_backend), methods=["DELETE"]
        ),
    ]


async def admin_refresh(ctx: _AdminCtx, b: Backend, *, force: bool = False) -> dict:
    """ctx-bound :func:`refresh_and_reload` (#43) for the admin routes."""
    return await refresh_and_reload(
        b, ctx.config_path, ctx.registry, ctx.holders, ctx.log, force=force
    )


def _claude_routes(ctx: _AdminCtx) -> list[Route]:
    """One-click Claude Code registration (#45): shell out to `claude mcp
    add/remove` for a backend's gateway endpoint (never edit Claude's config
    files by hand). The CLI runs in a thread so the event loop stays free.
    A CLI failure comes back as ``ok: false`` with its output at HTTP 200 (the
    HTTP call itself succeeded); missing binary / bad scope are 400."""

    async def _run_cli(argv: list[str], redact: str | None = None) -> JSONResponse:
        try:
            r = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True,
                text=True,
                timeout=CLAUDE_CLI_TIMEOUT,
                check=False,  # a CLI failure is surfaced as ok:false, not raised
            )
            rc, stdout, stderr = r.returncode, r.stdout, r.stderr
        except (subprocess.SubprocessError, OSError) as exc:
            rc, stdout, stderr = -1, "", f"{type(exc).__name__}: {exc}"

        def _hide(s: str) -> str:
            # never echo the bearer token (#26) back to the browser
            return s.replace(redact, "***") if redact else s

        return JSONResponse(
            {
                "ok": rc == 0,
                "exit": rc,
                "stdout": _hide(stdout),
                "stderr": _hide(stderr),
                "command": _hide(" ".join(argv)),
                "note": "Claude Code may need a reload/restart to pick up the change",
            }
        )

    def _missing_cli() -> JSONResponse | None:
        if shutil.which("claude") is None:
            return _err(
                "claude CLI not found on the daemon's PATH — install Claude "
                "Code (or expose `claude` to the daemon's environment), then "
                "retry"
            )
        return None

    async def register_backend(request: Request):
        """``claude mcp add`` for one backend as ``gateway-<name>``. Requires
        the backend to exist in config so the registered URL is real."""
        name = request.path_params["name"]
        payload = await request.json()
        scope = payload.get("scope") or "local"
        cfg = ctx.load()
        if not any(x.name == name for x in cfg.backends):
            return _err("unknown backend")
        missing = _missing_cli()
        if missing is not None:
            return missing
        url = f"http://{cfg.host}:{cfg.port}/{name}/mcp"
        try:
            # #26 × #45: a bearer-protected gateway needs the header in the
            # registration or every call would 401. Resolved once, redacted
            # from the response.
            token = cl.expand_env(cfg.bearer_token) if cfg.bearer_token else None
            argv = claude_mcp_command(
                "add", name, url=url, scope=scope, bearer_token=token
            )
        except cl.ConfigError as exc:
            return _err(str(exc))
        return await _run_cli(argv, redact=token)

    async def deregister_backend(request: Request):
        """``claude mcp remove`` for ``gateway-<name>``. Deliberately does NOT
        require the backend to exist in config — this is the cleanup path after
        a remove/rename, when the backend is already gone."""
        name = request.path_params["name"]
        payload = await request.json()
        scope = payload.get("scope") or "local"
        missing = _missing_cli()
        if missing is not None:
            return missing
        try:
            argv = claude_mcp_command("remove", name, scope=scope)
        except cl.ConfigError as exc:
            return _err(str(exc))
        return await _run_cli(argv)

    return [
        Route(
            "/admin/api/backend/{name}/register",
            _needs_json(register_backend),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/deregister",
            _needs_json(deregister_backend),
            methods=["POST"],
        ),
    ]


def _ops_routes(ctx: _AdminCtx) -> list[Route]:
    """Operational endpoints: mini-inspector, manual restart, re-introspect,
    liveness status (#23), and the dashboard-load refresh sweep (#43)."""

    async def restart_gateway(_request: Request):
        """Manual on-demand restart of the daemon (#56). Same launchd-gated
        semantics as a topology change: restarts when managed, honest no-op in
        dev/foreground."""
        return ctx.restart_response({})

    async def run_tool(request: Request):  # noqa: PLR0911 — one early return per input-validation failure
        """Mini-Inspector (#3): execute one tool through the LIVE proxy — the
        same path Claude uses, so renames/transforms apply and reverse-map —
        and return structured + unstructured content + error state. Read-only
        w.r.t. config, so no lock; call_tool_mcp doesn't raise on a
        tool-level error (isError comes back in the payload)."""
        payload = await request.json()
        backend = payload.get("backend")
        proxy = ctx.registry.get(backend)
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
        """Manual Re-inspect: force a re-capture (bypasses the #43 throttle)
        and hot-reload so pins/enabled reconcile with the fresh tool list.
        Unlike the auto triggers, a failure here is surfaced (502), not just
        logged — the user explicitly asked and deserves the answer."""
        name = request.path_params["name"]
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return _err("unknown backend")
        res = await admin_refresh(ctx, b, force=True)
        if res["status"] == "error":
            return _err(f"introspection failed: {res['error']}", status=502)
        return JSONResponse({"ok": True, **res})

    async def get_status(_request: Request):
        """#23: per-backend liveness — one concurrent probe per backend through
        its LIVE mounted proxy (the same path Claude's list_tools takes), each
        bounded by STATUS_TIMEOUT so a hung backend marks itself, not the UI."""

        async def one(b: Backend) -> tuple[str, dict]:
            if not b.enabled:
                return b.name, {"state": "disabled"}
            proxy = ctx.registry.get(b.name)
            if proxy is None:
                return b.name, {"state": "unmounted"}
            started = time.perf_counter()
            try:
                async with asyncio.timeout(STATUS_TIMEOUT):
                    async with Client(proxy) as c:
                        tools = await c.list_tools()
                return b.name, {
                    "state": "ok",
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                    "tools": len(tools),
                }
            except Exception as exc:  # noqa: BLE001 — the error IS the status
                err = f"{type(exc).__name__}: {exc}"
                return b.name, {"state": "error", "error": err}

        cfg = ctx.load()
        results = await asyncio.gather(*(one(b) for b in cfg.backends))
        return JSONResponse({"backends": dict(results)})

    async def refresh_all(_request: Request):
        """#43 dashboard-load trigger: throttled re-introspect of every
        enabled+mounted backend, concurrently and per-backend isolated — a
        down/slow backend reports itself and never stalls the others."""

        async def one(b: Backend) -> tuple[str, dict]:
            if not b.enabled or b.name not in ctx.registry:
                return b.name, {"status": "skipped"}
            return b.name, await admin_refresh(ctx, b)

        cfg = ctx.load()
        results = await asyncio.gather(*(one(b) for b in cfg.backends))
        return JSONResponse({"ok": True, "backends": dict(results)})

    return [
        Route("/admin/api/run", _needs_json(run_tool), methods=["POST"]),
        Route("/admin/api/restart", restart_gateway, methods=["POST"]),
        Route("/admin/api/introspect/{name}", reintrospect, methods=["POST"]),
        Route("/admin/api/status", get_status, methods=["GET"]),
        Route("/admin/api/refresh", refresh_all, methods=["POST"]),
    ]


def register(  # noqa: PLR0913 — public API; callers pass the lifespan plumbing
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
    ctx = _AdminCtx(
        config_path=config_path,
        log=log,
        registry=registry,
        holders=holders,
        hooks=hooks if hooks is not None else {},
    )
    app.router.routes.extend(
        [
            *_general_routes(ctx),
            *_settings_routes(ctx),
            *_backend_routes(ctx),
            *_claude_routes(ctx),
            *_ops_routes(ctx),
        ]
    )
