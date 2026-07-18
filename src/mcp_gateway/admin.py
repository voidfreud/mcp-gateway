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

from mcp_gateway import config_loader as cl
from mcp_gateway import hooks as hooks_mod
from mcp_gateway import virtual_tools as virtual_mod
from mcp_gateway.config_loader import (
    Backend,
    GatewayConfig,
    ParamOverride,
    PromptArgOverride,
    PromptOverride,
    ResourceOverride,
    ToolOverride,
)

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
        # repo checkout fallback: pyproject sits at the repo root (src layout)
        text = (HERE.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
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
            # #15: resources/templates/prompts too. Most backends broadcast
            # none — and one without the capability errors the request — so
            # each list degrades to empty rather than failing the capture.
            resources, templates, prompts = [], [], []
            try:
                resources = await c.list_resources()
            except Exception:  # noqa: BLE001, S110 — capability absent / unsupported
                pass
            try:
                templates = await c.list_resource_templates()
            except Exception:  # noqa: BLE001, S110
                pass
            try:
                prompts = await c.list_prompts()
            except Exception:  # noqa: BLE001, S110
                pass
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
    # #15: resources + templates share one shape ("uri" holds the uriTemplate
    # for a template) so overrides key on one field; prompts mirror tools.
    out_resources = [
        {
            "uri": str(r.uri),
            "name": r.name,
            "title": getattr(r, "title", None),
            "description": r.description,
            "mime_type": getattr(r, "mimeType", None),
        }
        for r in resources
    ]
    out_templates = [
        {
            "uri": t.uriTemplate,
            "name": t.name,
            "title": getattr(t, "title", None),
            "description": t.description,
            "mime_type": getattr(t, "mimeType", None),
        }
        for t in templates
    ]
    out_prompts = [
        {
            "original": p.name,
            "title": getattr(p, "title", None),
            "description": p.description,
            "args": [
                {
                    "original": a.name,
                    "description": a.description,
                    "required": bool(getattr(a, "required", False)),
                }
                for a in (p.arguments or [])
            ],
        }
        for p in prompts
    ]
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
        # `resources` also marks the file as carrying the #15 capture — a file
        # without the key is stale and re-captured once (see ensure_defaults).
        "resources": out_resources,
        "resource_templates": out_templates,
        "prompts": out_prompts,
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


def baseline_age(name: str) -> float | None:
    """Age in seconds of *name*'s captured baseline, or ``None`` when there is
    no baseline, no ``captured_at`` stamp (pre-#43 file), or the stamp lies in
    the future (clock went backwards) — all of which mean "treat as stale,
    refresh" to the #157 age gate."""
    ts = (load_defaults(name) or {}).get("captured_at")
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return None
    age = time.time() - ts
    return age if age >= 0 else None


def sweep_orphan_defaults(cfg: GatewayConfig, log) -> list[str]:
    """Delete captured-defaults files whose stem is not a configured backend name.

    Such files predate prune-on-remove (#54) — a backend removed before that
    landed left its ``<name>.json`` behind, and a stale baseline can resurrect a
    ghost backend's overrides on a re-import. Runs once at boot (#156). Deletes
    each orphan, logs one line per file, returns the removed stems. A DISABLED
    backend is still configured (it stays in ``cfg.backends``) so its file is
    kept; a missing defaults dir is tolerated (nothing to sweep).

    Disjoint-config guard (#157): if MORE THAN HALF the defaults files would
    be deleted, the loaded config almost certainly isn't the one that captured
    them — e.g. a scratch daemon booted against a test config while sharing
    the real state dir (this wiped every real baseline on 2026-07-14). Real
    orphans are removed one backend at a time by prune-on-remove (#54); a
    majority sweep is refused LOUDLY (one warning naming every would-be
    orphan) and nothing is deleted.
    """
    if not DEFAULTS_DIR.is_dir():
        return []
    configured = {b.name for b in cfg.backends}
    files = sorted(DEFAULTS_DIR.glob("*.json"))
    orphans = [p for p in files if p.stem not in configured]
    if orphans and len(orphans) * 2 > len(files):
        log.warning(
            "orphan_sweep_refused",
            reason="more than half the captured baselines would be deleted — "
            "loaded config looks disjoint from the state dir (scratch/test "
            "config?); nothing was removed",
            would_remove=[p.stem for p in orphans],
            configured=sorted(configured),
            defaults_dir=str(DEFAULTS_DIR),
        )
        return []
    removed: list[str] = []
    for p in orphans:
        p.unlink(missing_ok=True)
        removed.append(p.stem)
        log.info("orphan_defaults_swept", backend=p.stem, file=str(p))
    return removed


async def ensure_defaults(cfg: GatewayConfig, log, force: str | None = None) -> None:
    """Capture defaults for any backend missing them (or *force* one by name).

    A defaults file written before server-level capture lacks the ``instructions``
    key; treat such a file as stale and re-capture it once, so old installs gain
    instructions/serverInfo without a manual re-introspect.

    Captures run CONCURRENTLY (each already bounded by CAPTURE_TIMEOUT, each
    failure its own log line): a first run / fresh install pays the slowest
    backend, not the sum — the same rule boot mounting follows (#61).
    """

    async def one(b: Backend) -> None:
        try:
            save_defaults(await capture_defaults(b))
            # stamp the #43 throttle: a just-captured baseline is fresh, the
            # post-mount auto-refresh shouldn't immediately re-capture it
            _last_refresh[b.name] = time.monotonic()
            log.info("defaults_captured", backend=b.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("defaults_capture_failed", backend=b.name, error=str(exc))

    def needs(b: Backend) -> bool:
        if force:
            return b.name == force
        existing = load_defaults(b.name)
        # pre-instructions and pre-#15 (no resources key) files are both stale
        return (
            existing is None
            or "instructions" not in existing
            or "resources" not in existing
        )

    targets = [b for b in cfg.backends if needs(b)]
    if targets:
        await asyncio.gather(*(one(b) for b in targets))


async def refresh_defaults(
    b: Backend,
    log,
    *,
    force: bool = False,
    throttle: float = REFRESH_THROTTLE,
    max_age: float = 0.0,
) -> dict:
    """Re-capture ONE backend's baseline, throttled (#43). Returns a result
    dict: ``{"status": "refreshed"|"throttled"|"fresh"|"error", ...}``; on refresh it
    carries ``added``/``removed`` (original tool names vs the previous baseline)
    and ``changed`` (tools OR server instructions differ).

    Override safety: this only rewrites the immutable baseline; user overrides
    are stored separately as diffs and merged by original name, so a refresh
    never clobbers edits — new tools appear un-overridden, removed tools drop.

    The throttle stamp is set BEFORE the capture await (storms coalesce) and
    kept on failure (a down backend is retried at the throttle cadence, not on
    every trigger).

    Age gate (#157): a non-zero *max_age* skips the re-capture entirely when
    the STORED baseline (its persisted ``captured_at``) is younger than that
    many seconds — ``{"status": "fresh"}``. Only the post-mount trigger passes
    it (a boot seconds after the last capture learns nothing new, and slow
    stdio backends pay a full second cold start); the event-driven triggers
    (tools/list_changed, admin page load, manual Re-inspect) never do. A
    skip does NOT stamp the in-process throttle — those triggers stay live.
    """
    if max_age > 0 and not force:
        age = baseline_age(b.name)
        if age is not None and age < max_age:
            log.info(
                "baseline_fresh_skipped",
                backend=b.name,
                age_s=round(age),
                max_age_s=round(max_age),
            )
            return {"status": "fresh", "age": age}
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

    def _rp_sections(d: dict) -> tuple:
        # #15: resource/template/prompt captures count toward "changed" so a
        # backend that only edits those still hot-reloads its transforms.
        return (
            d.get("resources") or [],
            d.get("resource_templates") or [],
            d.get("prompts") or [],
        )

    changed = bool(
        added
        or removed
        or (old or {}).get("instructions") != data.get("instructions")
        or _rp_sections(old or {}) != _rp_sections(data)
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


def dangling_overrides(cfg: GatewayConfig, backend: str) -> list[dict]:
    """Stored overrides whose ``original`` no longer matches a captured tool —
    the backend renamed the tool upstream and a #43 baseline refresh moved the
    baseline out from under the override (#153). Their tuned text silently stops
    applying (reconcile logs ``override_no_match``), yet the entry still occupies
    a transform target. Surfaced in the UI so it can be migrated or discarded.

    Each entry: the stored ``original``, its ``name`` (effective broadcast name,
    == original when un-renamed), ``has_description`` (whether tuned description
    text would be lost), and ``enabled``.
    """
    b = next((x for x in cfg.backends if x.name == backend), None)
    if b is None:
        return []
    captured = {t["original"] for t in (load_defaults(backend) or {}).get("tools", [])}
    return [
        {
            "original": ov.original,
            "name": ov.name or ov.original,
            "has_description": ov.description is not None,
            "enabled": ov.enabled,
        }
        for ov in b.tools
        if ov.original not in captured
    ]


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


def _resource_state(b: Backend, dr: dict, template: bool) -> dict:
    """One resource/template row for the UI: captured defaults + override (#15)."""
    ov = next((r for r in b.resources if r.uri == dr["uri"]), None)
    return {
        "uri": dr["uri"],
        "template": template,
        "default_name": dr.get("name"),
        "default_title": dr.get("title"),
        "default_description": dr.get("description"),
        "mime_type": dr.get("mime_type"),
        "name": ov.name if ov else None,
        "title": ov.title if ov else None,
        "description": ov.description if ov else None,
        "enabled": ov.enabled if ov else True,
    }


def _prompt_state(b: Backend, dp: dict) -> dict:
    """One prompt row for the UI: captured defaults + override (#15)."""
    ov = next((p for p in b.prompts if p.original == dp["original"]), None)
    ov_args = {a.original: a for a in (ov.args if ov else [])}
    args = [
        {
            "original": da["original"],
            "default_description": da.get("description"),
            "required": da.get("required", False),
            "description": (
                ov_args[da["original"]].description
                if da["original"] in ov_args
                else None
            ),
        }
        for da in dp.get("args", [])
    ]
    return {
        "original": dp["original"],
        "default_name": dp["original"],
        "default_title": dp.get("title"),
        "default_description": dp.get("description"),
        "name": ov.name if ov else None,
        "title": ov.title if ov else None,
        "description": ov.description if ov else None,
        "enabled": ov.enabled if ov else True,
        "args": args,
    }


def _hook_error(ov: ToolOverride | None) -> str | None:
    """Current load status of a tool override's behavior hooks (#16), for the
    read-only admin display: None when there are no hooks or they all load,
    else the joined load error(s). Cheap — modules are mtime-cached — and
    always current (a fixed hook file clears the error on the next state read).
    """
    if ov is None:
        return None
    errs = []
    for spec in (ov.validate_, ov.post_process):
        if not spec:
            continue
        try:
            hooks_mod.load_hook(spec)
        except hooks_mod.HookError as exc:
            errs.append(str(exc))
    return "; ".join(errs) or None


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
                    # #162: per-tool output cap (chars) — None = client default
                    "max_result_chars": ov.max_result_chars if ov else None,
                    # #16: behavior hooks — hand-authored in config.toml, shown
                    # read-only (specs + current load status; None = no hooks /
                    # loading fine).
                    "validate": ov.validate_ if ov else None,
                    "post_process": ov.post_process if ov else None,
                    "hook_error": _hook_error(ov),
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
                "id": virtual_mod.stable_backend_id(b),
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
                # #153: stored overrides whose original no longer matches a
                # captured tool (backend renamed it upstream) — the UI flags
                # these for one-click migrate/discard.
                "dangling": dangling_overrides(cfg, b.name),
                "tools": tools_state,
                # #15: resources (templates flagged) + prompts, defaults merged
                # with overrides — empty lists when the backend broadcasts none
                # (or the defaults file predates the capture).
                "resources": [
                    _resource_state(b, dr, False)
                    for dr in (defaults or {}).get("resources", [])
                ]
                + [
                    _resource_state(b, dr, True)
                    for dr in (defaults or {}).get("resource_templates", [])
                ],
                "prompts": [
                    _prompt_state(b, dp) for dp in (defaults or {}).get("prompts", [])
                ],
            }
        )
    return {
        "host": cfg.host,
        "port": cfg.port,
        "version": gateway_version(),
        # #155: gateway-wide settings surfaced for the settings card's prefill.
        # bearer_token is the ${ENV} REF as stored — never the resolved secret.
        "bearer_token": cfg.bearer_token,
        "introspect_interval": cfg.introspect_interval,
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


def _clean_max_result_chars(v) -> int | None:
    """Validate a per-tool output cap (#162): a positive integer or None. The
    UI's cleared number field sends null/""; a whole-number float (JSON has no
    int type) is accepted; anything else is a clean 400 — mirroring the model
    validator so nonsense never reaches a persisted config."""
    if v is None or v == "":
        return None
    if isinstance(v, str) and v.strip().isdigit():
        v = int(v.strip())
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise cl.ConfigError(f"max_result_chars must be a positive integer (got {v!r})")
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
    max_result_chars = _field(
        "max_result_chars",
        lambda: _clean_max_result_chars(ov["max_result_chars"]),
        prev.max_result_chars if prev else None,
    )
    new = ToolOverride(
        original=original,
        name=name,
        title=title,
        description=description,
        enabled=enabled,
        always_load=always_load,
        max_result_chars=max_result_chars,
        # #16: hooks are hand-authored in config.toml, not admin-editable — a UI
        # save must carry them through unchanged, never silently drop them.
        validate_=prev.validate_ if prev else None,
        post_process=prev.post_process if prev else None,
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
        new.name
        or title
        or description
        or not enabled
        or always_load
        or max_result_chars is not None
        or params
        or new.validate_
        or new.post_process
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
    config landmine (found live migrating the openrouter drift). #15: the
    resource/prompt transform dry-builds too (duplicate prompt target names
    raise the same way)."""
    try:
        cl.build_transforms(cfg, b)
        cl.build_resource_prompt_transform(b)
    except ValueError as exc:
        raise cl.ConfigError(
            f"override rejected — it would break the transforms: {exc}. "
            f"If the clashing entry is a stale override for a tool the backend "
            f"no longer exposes, reset that tool first."
        ) from None


def apply_resource_override(cfg: GatewayConfig, backend: str, payload: dict) -> None:
    """Upsert one resource/template override from a UI payload (#15), diffing
    against the captured defaults — the same semantics as tools: prefilled
    fields store only real diffs, a key ABSENT from the payload preserves the
    stored value (#139), and a no-diff result removes the entry entirely.

    Keyed by ``uri`` (a resource's URI or a template's uriTemplate — the
    identity, never rewritten). Names are free-form display text (MCP puts no
    identifier charset on them), so unlike tools/prompts there is no name-rule
    or collision validation.
    """
    b = next((x for x in cfg.backends if x.name == backend), None)
    if b is None:
        raise cl.ConfigError(f"unknown backend {backend!r}")
    uri = payload["uri"]
    ov = payload.get("override", {})
    prev = next((r for r in b.resources if r.uri == uri), None)

    defaults = load_defaults(backend) or {}
    dres = next(
        (
            r
            for r in defaults.get("resources", [])
            + defaults.get("resource_templates", [])
            if r["uri"] == uri
        ),
        {},
    )

    def _field(key, computed, kept):
        return computed() if key in ov else kept

    name = _field(
        "name",
        lambda: _override_vs_default(ov.get("name"), dres.get("name")),
        prev.name if prev else None,
    )
    title = _field(
        "title",
        lambda: _override_vs_default(ov.get("title"), dres.get("title")),
        prev.title if prev else None,
    )
    description = _field(
        "description",
        lambda: _override_vs_default(ov.get("description"), dres.get("description")),
        prev.description if prev else None,
    )
    enabled = _field(
        "enabled", lambda: bool(ov["enabled"]), prev.enabled if prev else True
    )
    new = ResourceOverride(
        uri=uri, name=name, title=title, description=description, enabled=enabled
    )
    b.resources = [r for r in b.resources if r.uri != uri]
    if name or title or description or not enabled:
        b.resources.append(new)


def apply_prompt_override(cfg: GatewayConfig, backend: str, payload: dict) -> None:
    """Upsert one prompt's override from a UI payload (#15) — the tool model
    applied to prompts: diff-vs-default storage, #139 merge semantics for
    absent keys, identifier + collision validation on renames, and a
    transform dry-build so a save can never persist a config that fails the
    next mount. Argument descriptions are rewritable; argument NAMES are not
    (the args dict is forwarded to the backend verbatim)."""
    b = next((x for x in cfg.backends if x.name == backend), None)
    if b is None:
        raise cl.ConfigError(f"unknown backend {backend!r}")
    original = payload["prompt_original"]
    ov = payload.get("override", {})
    prev = next((p for p in b.prompts if p.original == original), None)

    defaults = load_defaults(backend) or {}
    dprompt = next(
        (p for p in defaults.get("prompts", []) if p["original"] == original), {}
    )
    dargs = {a["original"]: a for a in dprompt.get("args", [])}

    def _field(key, computed, kept):
        return computed() if key in ov else kept

    name = _field(
        "name",
        lambda: _override_vs_default(ov.get("name"), original),
        prev.name if prev else None,
    )
    title = _field(
        "title",
        lambda: _override_vs_default(ov.get("title"), dprompt.get("title")),
        prev.title if prev else None,
    )
    description = _field(
        "description",
        lambda: _override_vs_default(ov.get("description"), dprompt.get("description")),
        prev.description if prev else None,
    )
    _validate_name(name, "prompt name")

    args = []
    for a in ov.get("args", []):
        ao = a["original"]
        adesc = _override_vs_default(
            a.get("description"), dargs.get(ao, {}).get("description")
        )
        if adesc:
            args.append(PromptArgOverride(original=ao, description=adesc))
    if "args" not in ov and prev is not None:
        args = prev.args
    enabled = _field(
        "enabled", lambda: bool(ov["enabled"]), prev.enabled if prev else True
    )
    new = PromptOverride(
        original=original,
        name=name,
        title=title,
        description=description,
        enabled=enabled,
        args=args,
    )
    # Broadcast-level collision check: an enabled prompt's effective name must
    # be unique within the backend (Claude can't tell two apart otherwise).
    if new.enabled:
        eff_name = new.name or original
        for dp in defaults.get("prompts", []):
            other = dp["original"]
            if other == original:
                continue
            other_ov = next((p for p in b.prompts if p.original == other), None)
            if other_ov is not None and not other_ov.enabled:
                continue
            other_name = other_ov.name if (other_ov and other_ov.name) else other
            if other_name == eff_name:
                raise cl.ConfigError(
                    f"broadcast name {eff_name!r} is already used by prompt "
                    f"{other!r} in backend {backend!r} — names must be unique; "
                    f"pick a different one"
                )
    has_override = bool(new.name or title or description or not enabled or args)
    b.prompts = [p for p in b.prompts if p.original != original]
    if has_override:
        b.prompts.append(new)
    _reject_transform_landmines(cfg, b)


def migrate_override(cfg: GatewayConfig, backend: str, frm: str, to: str) -> dict:
    """Carry a DANGLING override's tuned text onto the tool's new original (#153).

    *frm* must be a stored override that is dangling (its ``original`` absent
    from the captured baseline — the backend renamed the tool away). *to* must be
    a captured tool that has no stored override yet. The override's fields (name,
    title, description, enabled, pin) move onto *to* through the normal
    :func:`apply_tool_override` path (so collision + transform validation still
    run); param overrides survive ONLY where the param's ``original`` still
    exists in *to*'s captured schema — dropped ones are reported. The old *frm*
    entry is then removed. Raises :class:`ConfigError` (→ 400) on any bad target.

    Mutates *cfg* in place; the caller commits (backup + save + hot-reload).
    """
    b = next((x for x in cfg.backends if x.name == backend), None)
    if b is None:
        raise cl.ConfigError(f"unknown backend {backend!r}")
    captured = {
        t["original"]: t for t in (load_defaults(backend) or {}).get("tools", [])
    }
    if to not in captured:
        raise cl.ConfigError(
            f"cannot migrate to {to!r}: it is not a captured tool of backend "
            f"{backend!r} — re-inspect the backend, or pick its new tool name"
        )
    src = next((t for t in b.tools if t.original == frm), None)
    if src is None:
        raise cl.ConfigError(f"no stored override for {frm!r} in backend {backend!r}")
    if frm in captured:
        raise cl.ConfigError(
            f"{frm!r} is still a live tool of backend {backend!r}; only a "
            f"dangling override (its tool renamed away upstream) can be migrated"
        )
    if any(t.original == to for t in b.tools):
        raise cl.ConfigError(
            f"cannot migrate to {to!r}: it already has a stored override — reset "
            f"it first"
        )
    # Params survive only where the target still has that param (#153).
    to_params = {p["original"] for p in captured[to].get("params", [])}
    kept, dropped = [], []
    for p in src.params:
        if p.original in to_params:
            kept.append(
                {
                    "original": p.original,
                    "name": p.name,
                    "description": p.description,
                    "hide": p.hide,
                    "default": p.default,
                }
            )
        else:
            dropped.append(p.original)
    override = {
        "name": src.name,
        "title": src.title,
        "description": src.description,
        "enabled": src.enabled,
        "always_load": src.always_load,
        "max_result_chars": src.max_result_chars,
        "params": kept,
    }
    # Drop the dangling entry BEFORE applying, so its (soon-obsolete) transform
    # target name can't collide with the migrated name on the target tool.
    b.tools = [t for t in b.tools if t.original != frm]
    apply_tool_override(cfg, backend, {"tool_original": to, "override": override})
    return {
        "migrated_to": to,
        "carried_params": [p["original"] for p in kept],
        "dropped_params": dropped,
    }


# ---------------------------------------------------------------------------
# Settings export / import (#136)
# ---------------------------------------------------------------------------

EXPORT_KIND = "mcp-gateway-settings"
EXPORT_VERSION = 1


def export_settings(  # noqa: PLR0912, PLR0915 — one branch per serialized override field
    cfg: GatewayConfig, full: bool = False
) -> dict:
    """The COMPLETE stored settings as one JSON-safe bundle: per-backend
    instructions override, display name, pin, and every tool/param override —
    exactly what config.toml stores beyond topology, so an import round-trips
    with zero loss. ``full`` adds each backend's captured defaults for context
    (read-only; import ignores them).

    Deliberately NOT bundled (like topology): behavior hooks (#16). A hook
    spec is a reference to machine-local code in this machine's hooks dir —
    exporting it to another gateway would import a dangling (fail-closed)
    reference. Merge-mode imports preserve stored hooks (the same #139
    absent-key semantics as UI saves); a replace-mode import resets the
    backend's stored overrides, hooks included."""
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
            if t.max_result_chars is not None:  # #162
                td["max_result_chars"] = t.max_result_chars
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
        # #15: resource + prompt overrides round-trip too.
        resources: dict = {}
        for r in b.resources:
            rd: dict = {}
            for key in ("name", "title", "description"):
                if getattr(r, key):
                    rd[key] = getattr(r, key)
            if not r.enabled:
                rd["enabled"] = False
            if rd:
                resources[r.uri] = rd
        if resources:
            entry["resources"] = resources
        prompts: dict = {}
        for p in b.prompts:
            pd: dict = {}
            for key in ("name", "title", "description"):
                if getattr(p, key):
                    pd[key] = getattr(p, key)
            if not p.enabled:
                pd["enabled"] = False
            if p.args:
                pd["args"] = [
                    {"original": a.original, "description": a.description}
                    for a in p.args
                ]
            if pd:
                prompts[p.original] = pd
        if prompts:
            entry["prompts"] = prompts
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
            b.resources = []
            b.prompts = []
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
        # #15: resource + prompt overrides, through the same validated paths.
        _import_rp_overrides(cfg, name, entry, errors)
    return affected, errors


def _import_rp_overrides(
    cfg: GatewayConfig, name: str, entry: dict, errors: list[str]
) -> None:
    """The #15 slice of :func:`import_settings`: apply one backend's resource
    and prompt override entries through the validated single-save paths,
    reporting each failure per item."""
    d = load_defaults(name) or {}
    known_uris = {
        r["uri"] for r in d.get("resources", []) + d.get("resource_templates", [])
    }
    for uri, rd in (entry.get("resources") or {}).items():
        if known_uris and uri not in known_uris:
            errors.append(f"{name}/{uri}: resource unknown to this backend")
            continue
        try:
            apply_resource_override(cfg, name, {"uri": uri, "override": dict(rd)})
        except (cl.ConfigError, KeyError) as exc:
            errors.append(f"{name}/{uri}: {exc}")
    known_prompts = {p["original"] for p in d.get("prompts", [])}
    for original, pd in (entry.get("prompts") or {}).items():
        if known_prompts and original not in known_prompts:
            errors.append(f"{name}/{original}: prompt unknown to this backend")
            continue
        try:
            apply_prompt_override(
                cfg, name, {"prompt_original": original, "override": dict(pd)}
            )
        except (cl.ConfigError, KeyError) as exc:
            errors.append(f"{name}/{original}: {exc}")


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
    # #15: the holder carries EVERY gateway-owned transform on this proxy (the
    # tool transform + the optional resource/prompt transform) — swap them all.
    for old in holders.get(backend) or []:
        if old is not None and old in proxy._transforms:
            proxy._transforms.remove(old)
    proxy.add_transform(new_transform)
    new_holder = [new_transform]
    rp_transform = cl.build_resource_prompt_transform(b)
    if rp_transform is not None:
        proxy.add_transform(rp_transform)
        new_holder.append(rp_transform)
    holders[backend] = new_holder
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
    max_age: float = 0.0,
) -> dict:
    """Re-capture one backend's baseline and, if it changed, hot-reload its
    live transforms + instructions so pins/enabled reconcile with the fresh
    tool list (#43). The shared tail of every auto-refresh trigger (post-mount,
    tools/list_changed, dashboard load, interval, manual Re-inspect). A
    non-zero *max_age* age-gates the capture (#157) — only the post-mount
    trigger passes one."""
    res = await refresh_defaults(
        b, log, force=force, throttle=throttle, max_age=max_age
    )
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

# #46: cache `claude mcp list` output in-process so repeated /admin page loads
# don't re-shell the CLI on every render; `?fresh=1` busts it after a
# register/deregister. Module-level (resets on restart, which is fine).
CC_REG_CACHE_TTL = 60.0
_cc_reg_cache: dict[str, Any] = {"ts": 0.0, "output": None}

# Codex uses one user-level config and exposes JSON for reliable status parsing.
# Keep its cache independent from Claude Code so either CLI can be absent or
# refreshed without affecting the other client integration.
CODEX_REG_CACHE_TTL = 60.0
_codex_reg_cache: dict[str, Any] = {"ts": 0.0, "output": None}


def parse_cc_registrations(output: str, backends: list[str]) -> dict[str, bool]:
    """Map each configured backend to whether it is registered in Claude Code,
    parsed from ``claude mcp list`` output (#46).

    A backend counts as registered iff the token ``gateway-<name>:`` appears in
    the output — the colon anchors the match so ``gateway-cc:`` can't be read
    off ``gateway-cc-docs:``, and the connection-status suffix
    (``✓ Connected`` / ``✘ Failed``) is deliberately ignored: this reports
    REGISTRATION, not liveness (that is #23's ``/admin/api/status``).
    """
    return {name: f"gateway-{name}:" in output for name in backends}


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


def parse_codex_registrations(output: str, backends: list[str]) -> dict[str, bool]:
    """Map configured backends to exact registrations from ``codex mcp list``.

    Codex provides a JSON mode, so avoid depending on human-readable output.
    Registration state is intentionally distinct from ``enabled`` and
    ``auth_status``: a disabled or unauthenticated entry still exists in Codex.
    """
    try:
        items = json.loads(output)
    except (json.JSONDecodeError, TypeError) as exc:
        raise cl.ConfigError("codex mcp list returned malformed JSON") from exc
    if not isinstance(items, list):
        raise cl.ConfigError("codex mcp list returned JSON other than a list")
    names = {
        item.get("name")
        for item in items
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    return {name: f"gateway-{name}" in names for name in backends}


def codex_bearer_env_var(bearer_token: str | None) -> str | None:
    """Return the environment variable Codex should use for gateway auth.

    ``codex mcp add`` deliberately accepts an environment-variable *name*, not
    a literal token. Requiring a single ``${ENV}`` reference keeps credentials
    out of argv, API responses, and ``~/.codex/config.toml``.
    """
    if bearer_token is None:
        return None
    match = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", bearer_token.strip())
    if match is None:
        raise cl.ConfigError(
            "Codex registration requires bearer_token to be a single ${ENV_VAR} "
            "reference; literal tokens are never written to Codex config"
        )
    return match.group(1)


def codex_mcp_command(
    action: str,
    name: str,
    url: str | None = None,
    bearer_env_var: str | None = None,
) -> list[str]:
    """Build the Codex CLI argv for one independent gateway backend."""
    registration = f"gateway-{name}"
    if action == "add":
        if not url:
            raise cl.ConfigError("register needs the backend's endpoint url")
        argv = ["codex", "mcp", "add", registration, "--url", url]
        if bearer_env_var:
            argv += ["--bearer-token-env-var", bearer_env_var]
        return argv
    if action == "remove":
        return ["codex", "mcp", "remove", registration]
    raise cl.ConfigError(f"unknown action {action!r} (use add or remove)")


def codex_cli_path() -> str | None:
    """Locate Codex for both interactive shells and the macOS login daemon."""
    found = shutil.which("codex")
    if found:
        return found
    candidates = [
        os.environ.get("CODEX_CLI_PATH"),
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


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


def _validate_active_virtual_references(cfg: GatewayConfig) -> None:
    """Reject a save that would strand a currently published Virtual Tool.

    This is deliberately structural and synchronous: admin override handlers
    call it before persistence and before their live transform is swapped. Live
    reachability remains the activation/test concern; here we prove the stable
    source tool/parameter still exists in the captured baseline and is enabled.
    """
    virtual_mod.ensure_backend_ids(cfg)
    by_id = {backend.id: backend for backend in cfg.backends}
    for tool in cfg.virtual_tools:
        if not tool.enabled:
            continue
        for member in tool.members:
            backend = by_id.get(member.backend_id)
            label = virtual_mod.member_label(member)
            if backend is None:
                raise cl.ConfigError(
                    f"active Virtual Tool {tool.name!r} member {label!r} "
                    "references a missing backend"
                )
            if not backend.enabled:
                raise cl.ConfigError(
                    f"cannot disable backend {backend.name!r}: active Virtual "
                    f"Tool {tool.name!r} references it"
                )
            defaults = load_defaults(backend.name) or {}
            source = next(
                (
                    item
                    for item in defaults.get("tools", [])
                    if item.get("original") == member.tool_original
                ),
                None,
            )
            if source is None:
                raise cl.ConfigError(
                    f"active Virtual Tool {tool.name!r} source "
                    f"{backend.name}/{member.tool_original} is not in the "
                    "captured catalog; disable or repair the Virtual Tool first"
                )
            override = next(
                (
                    item
                    for item in backend.tools
                    if item.original == member.tool_original
                ),
                None,
            )
            if override is not None and not override.enabled:
                raise cl.ConfigError(
                    f"cannot disable {backend.name}/{member.tool_original}: "
                    f"active Virtual Tool {tool.name!r} references it"
                )
            params = {item.get("original") for item in source.get("params", [])}
            referenced = set(member.args) | set(member.static_args)
            missing = sorted(referenced - params)
            if missing:
                raise cl.ConfigError(
                    f"active Virtual Tool {tool.name!r} source "
                    f"{backend.name}/{member.tool_original} is missing original "
                    f"parameter(s): {missing}"
                )
            hidden = {
                item.original
                for item in (override.params if override is not None else [])
                if item.hide
            }
            blocked = sorted(referenced & hidden)
            if blocked:
                raise cl.ConfigError(
                    f"cannot hide parameter(s) {blocked} on "
                    f"{backend.name}/{member.tool_original}: active Virtual Tool "
                    f"{tool.name!r} references them"
                )


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

    def commit(
        self,
        cfg: GatewayConfig,
        *backends: str,
        validate_virtual_refs: bool = True,
    ) -> None:
        """The shared tail of every mutating handler: backup, atomic save, then
        hot-reload each named backend that is currently mounted (hot_reload
        itself skips unmounted ones with a warning)."""
        GatewayConfig.model_validate(cfg.model_dump())
        if validate_virtual_refs:
            _validate_active_virtual_references(cfg)
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
                try:
                    return await handler(request)
                except cl.ConfigError as exc:
                    return _err(str(exc))

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


def _settings_routes(ctx: _AdminCtx) -> list[Route]:  # noqa: PLR0915 — statement count is the nested handlers; the group is cohesive
    """Text overrides: tool override, reset, instructions, import, and the #153
    dangling-override migrate/discard repairs."""

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

    async def put_resource_override(request: Request):
        """#15: upsert one resource/template override (keyed by uri)."""
        payload = await request.json()
        cfg = ctx.load()
        try:
            apply_resource_override(cfg, payload["backend"], payload)
        except (cl.ConfigError, KeyError) as exc:
            return _err(str(exc))
        ctx.commit(cfg, payload["backend"])
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def reset_resource(request: Request):
        """#15: clear all overrides for one resource (revert to default)."""
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == payload["backend"]), None)
        if b is None:
            return _err("unknown backend")
        b.resources = [r for r in b.resources if r.uri != payload["uri"]]
        ctx.commit(cfg, payload["backend"])
        return JSONResponse({"ok": True})

    async def put_prompt_override(request: Request):
        """#15: upsert one prompt's override (rename, text, args, enabled)."""
        payload = await request.json()
        cfg = ctx.load()
        try:
            apply_prompt_override(cfg, payload["backend"], payload)
        except (cl.ConfigError, KeyError) as exc:
            return _err(str(exc))
        ctx.commit(cfg, payload["backend"])
        return JSONResponse({"ok": True, "reloaded": "in-process"})

    async def reset_prompt(request: Request):
        """#15: clear all overrides for one prompt (revert to default)."""
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == payload["backend"]), None)
        if b is None:
            return _err("unknown backend")
        b.prompts = [p for p in b.prompts if p.original != payload["prompt_original"]]
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

    async def migrate_override_route(request: Request):
        """#153: carry a dangling override's tuned text onto the tool's new
        original, then drop the old entry. Hot-reloads that backend."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        try:
            res = migrate_override(cfg, name, payload.get("from"), payload.get("to"))
        except (cl.ConfigError, KeyError) as exc:
            return _err(str(exc))
        ctx.commit(cfg, name)
        return JSONResponse({"ok": True, "reloaded": "in-process", **res})

    async def discard_override_route(request: Request):
        """#153: drop a dangling override entry (its tuned text no longer
        applies). Same removal as /reset — the intent is different (clearing a
        stale entry, not reverting a live tool)."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return _err("unknown backend")
        original = payload.get("original")
        before = len(b.tools)
        b.tools = [t for t in b.tools if t.original != original]
        if len(b.tools) == before:
            return _err(f"no stored override for {original!r}")
        ctx.commit(cfg, name)
        return JSONResponse({"ok": True, "reloaded": "in-process"})

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
            "/admin/api/resource-override",
            _needs_json(ctx.locked(put_resource_override)),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/resource-reset",
            _needs_json(ctx.locked(reset_resource)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/prompt-override",
            _needs_json(ctx.locked(put_prompt_override)),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/prompt-reset",
            _needs_json(ctx.locked(reset_prompt)),
            methods=["POST"],
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
        Route(
            "/admin/api/backend/{name}/migrate-override",
            _needs_json(ctx.locked(migrate_override_route)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/discard-override",
            _needs_json(ctx.locked(discard_override_route)),
            methods=["POST"],
        ),
    ]


# ${ENV} reference guard for the bearer token (#155): a stored token must be a
# reference like ${MCP_GATEWAY_TOKEN}, never a pasted secret value.
_ENV_REF_RE = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}")


def _gateway_settings_routes(ctx: _AdminCtx) -> list[Route]:
    """#155: read/write the two gateway-wide, boot-time settings — the bearer
    token (${ENV} ref) and the introspect interval. Both are resolved/read only
    at startup (token via expand_env in _build_app; interval when the lifespan's
    #43 sweep is wired), so a change needs a daemon restart to take effect —
    the PUT returns restart semantics."""

    async def get_settings(_request: Request):
        cfg = ctx.load()
        return JSONResponse(
            {
                # the ${ENV} REF exactly as stored — never the resolved secret
                "bearer_token": cfg.bearer_token,
                "introspect_interval": cfg.introspect_interval,
            }
        )

    async def put_settings(request: Request):
        payload = await request.json()
        cfg = ctx.load()
        if "bearer_token" in payload:
            tok = payload.get("bearer_token")
            if tok is not None and not isinstance(tok, str):
                return _err("bearer_token must be a string or null")
            tok = (tok or "").strip()
            # Guard against pasting a raw secret: an empty token (no auth) is
            # fine, otherwise it MUST be a ${ENV} reference (#155/#26).
            if tok and not _ENV_REF_RE.search(tok):
                return _err(
                    "bearer_token must reference an environment variable like "
                    "${MCP_GATEWAY_TOKEN} — never paste the secret itself"
                )
            cfg.bearer_token = tok or None
        if "introspect_interval" in payload:
            iv = payload.get("introspect_interval")
            # bool is an int subclass — reject it explicitly so `true` isn't 1.
            if isinstance(iv, bool) or not isinstance(iv, int) or iv < 0:
                return _err("introspect_interval must be an integer >= 0")
            cfg.introspect_interval = iv
        ctx.commit(cfg)  # persist only — both settings are read at boot
        return ctx.restart_response({"changed": "gateway-settings"})

    return [
        Route("/admin/api/settings", get_settings, methods=["GET"]),
        Route(
            "/admin/api/settings",
            _needs_json(ctx.locked(put_settings)),
            methods=["PUT"],
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

    async def set_stateless(request: Request):
        """Toggle a backend's warm/stateless session strategy (#161).

        A warm session (``stateless=false``) reuses one backend connection across
        calls and auto-recycles if it dies; stateless spins up a fresh session per
        call. Flipping it is a per-backend topology change, but it needs NO daemon
        restart: save the new flag, then recycle the backend via the lifespan hook
        — the runner tears down and re-mounts, reading the fresh config (so the new
        ``stateless`` value takes effect on the re-mount). If the hook isn't
        registered (no lifespan, e.g. a unit test), the value is still persisted."""
        name = request.path_params["name"]
        payload = await request.json()
        cfg = ctx.load()
        b = next((x for x in cfg.backends if x.name == name), None)
        if b is None:
            return _err("unknown backend")
        b.stateless = bool(payload.get("value", False))
        ctx.commit(cfg)  # persist only — the recycle re-mounts with fresh config
        recycle = ctx.hooks.get("recycle")
        if recycle is not None:
            recycle(name)
        return JSONResponse(
            {"ok": True, "reloaded": "recycled", "stateless": b.stateless}
        )

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

    async def rename_backend(request: Request):  # noqa: PLR0911, PLR0912, PLR0915 - transactional validation/rollback exits
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
        if new_name == virtual_mod.VIRTUAL_ROUTE:
            return _err("backend name 'virtual' is reserved for Virtual Tools")
        cfg = ctx.load()
        old_backend = next((x for x in cfg.backends if x.name == name), None)
        if old_backend is None:
            return _err("unknown backend")
        if any(x.name == new_name for x in cfg.backends):
            return _err(
                f"backend name {new_name!r} already exists — pick a different one"
            )
        candidate = cfg.model_copy(deep=True)
        new_backend = next(x for x in candidate.backends if x.name == name)
        new_backend.name = new_name
        try:
            candidate = GatewayConfig.model_validate(candidate.model_dump())
        except Exception as exc:  # noqa: BLE001 - complete candidate validation
            return _err(str(exc))
        # Persist the complete valid candidate first, but keep the old live mount
        # until the new identity has mounted successfully. This makes the runtime
        # topology transactional even though two names briefly coexist.
        ctx.commit(candidate, validate_virtual_refs=False)
        # Migrate the captured defaults (the immutable baseline) old → new so
        # overrides keep diffing against it; tolerate a never-introspected
        # backend (no file — the restart re-captures under the new name).
        old_defaults = DEFAULTS_DIR / f"{name}.json"
        new_defaults = DEFAULTS_DIR / f"{new_name}.json"
        old_defaults_data = (
            json.loads(old_defaults.read_text(encoding="utf-8"))
            if old_defaults.is_file()
            else None
        )
        if old_defaults_data is not None:
            data = dict(old_defaults_data)
            data["backend"] = new_name
            save_defaults(data)
            old_defaults.unlink(missing_ok=True)
        base = f"http://{candidate.host}:{candidate.port}"
        response = {
            "backend": new_name,
            "old_endpoint": f"{base}/{name}/mcp",
            "new_endpoint": f"{base}/{new_name}/mcp",
            "old_registration": f"gateway-{name}",
            "new_registration": f"gateway-{new_name}",
        }
        # In the live daemon, mount the new identity before responding so stable
        # Virtual Tool references resolve immediately. Claude Code still needs
        # its external registration moved, which the response makes explicit.
        remove = ctx.hooks.get("remove")
        add = ctx.hooks.get("add")
        if remove is not None and add is not None:
            mount_error = None
            try:
                mounted = await add(new_backend)
            except Exception as exc:  # noqa: BLE001 - transaction rolls back below
                mounted = False
                mount_error = f": {type(exc).__name__}: {exc}"
            if mounted:
                remove(name)
                return JSONResponse({"ok": True, "reloaded": "hot-rename", **response})
            # The old runner was deliberately left alive. Remove any partial new
            # runner, restore config/defaults, and only re-add old if it somehow
            # disappeared independently while the new mount was attempted.
            remove(new_name)
            ctx.commit(cfg, validate_virtual_refs=False)
            new_defaults.unlink(missing_ok=True)
            if old_defaults_data is not None:
                save_defaults(old_defaults_data)
            recovery_error = None
            if name not in ctx.registry:
                try:
                    if not await add(old_backend):
                        recovery_error = "; old backend re-mount also failed"
                except Exception as exc:  # noqa: BLE001 - report safe rollback state
                    recovery_error = (
                        f"; old backend re-mount failed: {type(exc).__name__}: {exc}"
                    )
            return JSONResponse(
                {
                    "ok": False,
                    "reloaded": "mount-failed-rolled-back",
                    "error": "new backend mount failed; rename rolled back"
                    + (mount_error or "")
                    + (recovery_error or ""),
                    **response,
                },
                status_code=500,
            )
        return ctx.restart_response(response)

    async def add_backend(request: Request):  # noqa: PLR0911 — one early return per validation/probe/mount outcome
        """Import a new backend MCP. Validates + introspects, then restarts."""
        payload = await request.json()
        if payload.get("name") == virtual_mod.VIRTUAL_ROUTE:
            return _err("backend name 'virtual' is reserved for Virtual Tools")
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
        b.id = virtual_mod.stable_backend_id(b)
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
            candidate = cfg.model_copy(
                update={"backends": [*cfg.backends, b]}, deep=True
            )
            try:
                candidate = GatewayConfig.model_validate(candidate.model_dump())
            except Exception as exc:  # noqa: BLE001 - complete candidate validation
                return _err(str(exc))
            ctx.commit(candidate)
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
        backend = next((b for b in cfg.backends if b.name == name), None)
        if backend is None:
            return _err("unknown backend")
        backend_id = virtual_mod.stable_backend_id(backend)
        referenced_by = [
            tool.name
            for tool in cfg.virtual_tools
            if any(member.backend_id == backend_id for member in tool.members)
        ]
        if referenced_by:
            return _err(
                f"backend is referenced by Virtual Tool(s): {', '.join(referenced_by)}"
            )
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
            "/admin/api/backend/{name}/stateless",
            _needs_json(ctx.locked(set_stateless)),
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


def _claude_routes(ctx: _AdminCtx) -> list[Route]:  # noqa: PLR0915 — nested handlers; the group is cohesive
    """One-click Claude Code registration (#45): shell out to `claude mcp
    add/remove` for a backend's gateway endpoint (never edit Claude's config
    files by hand). The CLI runs in a thread so the event loop stays free.
    A CLI failure comes back as ``ok: false`` with its output at HTTP 200 (the
    HTTP call itself succeeded); missing binary / bad scope are 400."""

    async def _cli_raw(argv: list[str]) -> tuple[int, str, str]:
        """Run one `claude` CLI invocation off the event loop; never raises —
        a spawn failure comes back as ``(-1, "", "<error>")``."""
        try:
            r = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True,
                text=True,
                timeout=CLAUDE_CLI_TIMEOUT,
                check=False,  # a CLI failure is surfaced as ok:false, not raised
            )
            return r.returncode, r.stdout, r.stderr
        except (subprocess.SubprocessError, OSError) as exc:
            return -1, "", f"{type(exc).__name__}: {exc}"

    async def _run_cli(argv: list[str], redact: str | None = None) -> JSONResponse:
        rc, stdout, stderr = await _cli_raw(argv)

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

    async def cc_registrations(request: Request):
        """#46: which backends are registered in Claude Code. Runs
        ``claude mcp list`` ONCE (in a thread), caches the output in-process for
        ``CC_REG_CACHE_TTL`` so page reloads don't re-shell the CLI; ``?fresh=1``
        busts the cache (used right after a register/deregister). Returns
        ``{"available": false}`` when the CLI isn't on PATH, else
        ``{"available": true, "registered": {backend: bool}}``."""
        if shutil.which("claude") is None:
            return JSONResponse({"available": False})
        fresh = request.query_params.get("fresh") in ("1", "true")
        now = time.monotonic()
        output = _cc_reg_cache.get("output")
        if fresh or output is None or now - _cc_reg_cache["ts"] > CC_REG_CACHE_TTL:
            try:
                r = await asyncio.to_thread(
                    subprocess.run,
                    ["claude", "mcp", "list"],
                    capture_output=True,
                    text=True,
                    timeout=CLAUDE_CLI_TIMEOUT,
                    check=False,
                )
                output = (r.stdout or "") + (r.stderr or "")
            except (subprocess.SubprocessError, OSError):
                output = ""
            _cc_reg_cache["output"] = output
            _cc_reg_cache["ts"] = now
        names = [b.name for b in ctx.load().backends]
        return JSONResponse(
            {"available": True, "registered": parse_cc_registrations(output, names)}
        )

    async def reregister_all(request: Request):
        """#154: deregister + register EVERY enabled backend in Claude Code, so
        one click re-points them all at this gateway (e.g. after the bearer token
        changed — every registration must carry the new header). Sequential;
        each backend gets a fresh `remove` then `add`. A per-backend failure is
        recorded and does NOT abort the rest — the summary reports ok/fail each."""
        payload = await request.json()
        scope = payload.get("scope") or "local"
        cfg = ctx.load()
        missing = _missing_cli()
        if missing is not None:
            return missing
        try:
            # Resolved once for every registration; redacted from all output.
            token = cl.expand_env(cfg.bearer_token) if cfg.bearer_token else None
        except cl.ConfigError as exc:
            return _err(str(exc))

        def _hide(s: str) -> str:
            return s.replace(token, "***") if token else s

        results: list[dict] = []
        for b in cfg.backends:
            if not b.enabled:
                continue  # only enabled backends are broadcast, so only they register
            url = f"http://{cfg.host}:{cfg.port}/{b.name}/mcp"
            try:
                rm_argv = claude_mcp_command("remove", b.name, scope=scope)
                add_argv = claude_mcp_command(
                    "add", b.name, url=url, scope=scope, bearer_token=token
                )
            except cl.ConfigError as exc:
                results.append({"backend": b.name, "ok": False, "error": str(exc)})
                continue
            await _cli_raw(rm_argv)  # best-effort cleanup; a missing reg is fine
            rc, _out, err = await _cli_raw(add_argv)
            results.append(
                {
                    "backend": b.name,
                    "ok": rc == 0,
                    "exit": rc,
                    "stderr": _hide(err),
                }
            )
        ok_count = sum(1 for r in results if r["ok"])
        return JSONResponse(
            {
                "ok": all(r["ok"] for r in results),
                "count": len(results),
                "ok_count": ok_count,
                "backends": results,
                "note": "Claude Code may need a reload/restart to pick up the change",
            }
        )

    return [
        Route(
            "/admin/api/cc-reregister-all",
            _needs_json(reregister_all),
            methods=["POST"],
        ),
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
        Route("/admin/api/cc-registrations", cc_registrations, methods=["GET"]),
    ]


def _codex_routes(ctx: _AdminCtx) -> list[Route]:
    """One-click registration of each backend as an independent Codex MCP.

    Codex and Claude Code intentionally remain separate client integrations:
    this uses Codex's own CLI/config and never changes the backend mounts or
    combines their tool catalogs.
    """

    async def _cli_raw(argv: list[str]) -> tuple[int, str, str]:
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                argv,
                capture_output=True,
                text=True,
                timeout=CLAUDE_CLI_TIMEOUT,
                check=False,
            )
            return result.returncode, result.stdout, result.stderr
        except (subprocess.SubprocessError, OSError) as exc:
            return -1, "", f"{type(exc).__name__}: {exc}"

    def _binary() -> str | JSONResponse:
        binary = codex_cli_path()
        if binary is None:
            return _err(
                "Codex CLI not found — install Codex/ChatGPT desktop or expose "
                "the codex executable to the gateway daemon, then retry"
            )
        return binary

    async def register_backend(request: Request):
        name = request.path_params["name"]
        cfg = ctx.load()
        if not any(backend.name == name for backend in cfg.backends):
            return _err("unknown backend")
        binary = _binary()
        if isinstance(binary, JSONResponse):
            return binary
        url = f"http://{cfg.host}:{cfg.port}/{name}/mcp"
        try:
            bearer_env_var = codex_bearer_env_var(cfg.bearer_token)
            argv = codex_mcp_command(
                "add", name, url=url, bearer_env_var=bearer_env_var
            )
        except cl.ConfigError as exc:
            return _err(str(exc))
        argv[0] = binary
        rc, stdout, stderr = await _cli_raw(argv)
        return JSONResponse(
            {
                "ok": rc == 0,
                "exit": rc,
                "stdout": stdout,
                "stderr": stderr,
                "command": " ".join(argv),
                "note": "Restart Codex or open a new task to load the server",
            }
        )

    async def deregister_backend(request: Request):
        name = request.path_params["name"]
        binary = _binary()
        if isinstance(binary, JSONResponse):
            return binary
        argv = codex_mcp_command("remove", name)
        argv[0] = binary
        rc, stdout, stderr = await _cli_raw(argv)
        return JSONResponse(
            {
                "ok": rc == 0,
                "exit": rc,
                "stdout": stdout,
                "stderr": stderr,
                "command": " ".join(argv),
                "note": "Restart Codex or open a new task to unload the server",
            }
        )

    async def registrations(request: Request):
        binary = _binary()
        if isinstance(binary, JSONResponse):
            return JSONResponse({"available": False})
        fresh = request.query_params.get("fresh") in ("1", "true")
        now = time.monotonic()
        output = _codex_reg_cache.get("output")
        if (
            fresh
            or output is None
            or now - _codex_reg_cache["ts"] > CODEX_REG_CACHE_TTL
        ):
            rc, stdout, stderr = await _cli_raw([binary, "mcp", "list", "--json"])
            if rc != 0:
                return JSONResponse(
                    {
                        "available": True,
                        "ok": False,
                        "error": stderr or stdout or f"codex mcp list exited {rc}",
                    }
                )
            output = stdout
            _codex_reg_cache["output"] = output
            _codex_reg_cache["ts"] = now
        try:
            registered = parse_codex_registrations(
                output, [backend.name for backend in ctx.load().backends]
            )
        except cl.ConfigError as exc:
            return JSONResponse({"available": True, "ok": False, "error": str(exc)})
        return JSONResponse({"available": True, "ok": True, "registered": registered})

    return [
        Route(
            "/admin/api/backend/{name}/codex/register",
            _needs_json(register_backend),
            methods=["POST"],
        ),
        Route(
            "/admin/api/backend/{name}/codex/deregister",
            _needs_json(deregister_backend),
            methods=["POST"],
        ),
        Route("/admin/api/codex-registrations", registrations, methods=["GET"]),
    ]


def _ops_routes(ctx: _AdminCtx) -> list[Route]:  # noqa: PLR0915 — nested handlers; the group is cohesive
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
                # #161: a WARM backend that probes `error` may have a dead session
                # fastmcp won't heal — recycle it best-effort (cooldown-debounced
                # in the hook). Stateless backends spin a fresh session per probe,
                # so there's nothing to recycle.
                if not b.stateless:
                    recycle = ctx.hooks.get("recycle")
                    if recycle is not None:
                        recycle(b.name)
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


def _virtual_routes(ctx: _AdminCtx) -> list[Route]:  # noqa: PLR0915
    """First-class Virtual Tool catalog, draft lifecycle, testing and activation."""

    def _definition(tool: cl.VirtualTool) -> dict:
        return tool.model_dump(mode="json", exclude_none=True)

    def _find(cfg: GatewayConfig, name: str) -> cl.VirtualTool | None:
        return next((tool for tool in cfg.virtual_tools if tool.name == name), None)

    def _clean_virtual_payload(payload: dict) -> dict:
        """Discard client-authored consent receipts; only activation may mint one."""
        cleaned = dict(payload)
        cleaned.pop("consent_fingerprint", None)
        cleaned.pop("egress_consent_fingerprint", None)
        if isinstance(cleaned.get("router"), dict):
            cleaned["router"] = dict(cleaned["router"])
            cleaned["router"].pop("consent_fingerprint", None)
            cleaned["router"].pop("egress_consent_fingerprint", None)
        return cleaned

    def _consent_fingerprint(tool: cl.VirtualTool) -> str | None:
        """Fingerprint exactly the administrator-visible OpenRouter egress shape."""
        if tool.dispatch != "llm" or tool.router is None:
            return None
        return cl.llm_egress_consent_fingerprint(tool)

    async def _resolution(tool: cl.VirtualTool, cfg: GatewayConfig) -> dict:
        return await virtual_mod.resolve_tool(tool, cfg, ctx.registry)

    def _hot_replace(cfg: GatewayConfig) -> None:
        server = ctx.hooks.get("virtual_server")
        if server is None:
            raise cl.ConfigError("the shared /virtual/mcp endpoint is not mounted")
        virtual_mod.replace_tools(
            server,
            cfg,
            ctx.load,
            ctx.registry,
            ctx.log,
            ctx.hooks.setdefault("virtual_status", {}),
        )

    async def list_virtual_tools(_request: Request):
        cfg = ctx.load()
        resolutions = await asyncio.gather(
            *(_resolution(tool, cfg) for tool in cfg.virtual_tools)
        )
        statuses = ctx.hooks.setdefault("virtual_status", {})
        listed = []
        for tool, resolution in zip(cfg.virtual_tools, resolutions, strict=True):
            definition = _definition(tool)
            definition["members"] = [
                {**member, "resolution": resolved}
                for member, resolved in zip(
                    definition["members"], resolution["members"], strict=True
                )
            ]
            listed.append(
                {
                    **definition,
                    "resolution": resolution,
                    **statuses.get(tool.name, {}),
                }
            )
        return JSONResponse(
            {
                "mounted": "virtual_server" in ctx.hooks,
                "endpoint": "/virtual/mcp",
                "tools": listed,
            }
        )

    async def virtual_catalog(_request: Request):
        cfg = ctx.load()
        backends = []
        for backend in cfg.backends:
            defaults = load_defaults(backend.name) or {}
            tools = []
            for source in defaults.get("tools", []):
                override = _find_tool_override(backend, source["original"])
                effective = (
                    override.name if override and override.name else source["original"]
                )
                params = []
                overrides = {
                    item.original: item
                    for item in (override.params if override else [])
                }
                for param in source.get("params", []):
                    changed = overrides.get(param["original"])
                    params.append(
                        {
                            "original": param["original"],
                            "effective_name": (
                                changed.name
                                if changed is not None and changed.name
                                else param["original"]
                            ),
                            "description": param.get("description"),
                            "required": param.get("required", False),
                            "hidden": changed.hide if changed else False,
                        }
                    )
                tools.append(
                    {
                        "original": source["original"],
                        "effective_name": effective,
                        "description": (
                            override.description
                            if override and override.description is not None
                            else source.get("description")
                        ),
                        "enabled": backend.enabled
                        and (override.enabled if override else True),
                        "params": params,
                    }
                )
            backends.append(
                {
                    "id": virtual_mod.stable_backend_id(backend),
                    "name": backend.name,
                    "effective_name": backend.display_name or backend.name,
                    "enabled": backend.enabled,
                    "tools": tools,
                }
            )
        return JSONResponse({"backends": backends})

    async def create_virtual(request: Request):
        payload = _clean_virtual_payload(await request.json())
        payload = {**payload, "enabled": False}
        try:
            tool = cl.VirtualTool.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - Pydantic/config validation
            return _err(str(exc))
        cfg = ctx.load()
        if _find(cfg, tool.name) is not None:
            return _err("virtual tool name already exists")
        virtual_mod.ensure_backend_ids(cfg)
        try:
            candidate = cfg.model_copy(
                update={"virtual_tools": [*cfg.virtual_tools, tool]}, deep=True
            )
            candidate = GatewayConfig.model_validate(candidate.model_dump())
            virtual_mod.build_virtual_tool(tool, candidate, ctx.registry, ctx.log)
        except Exception as exc:  # noqa: BLE001 - dry build
            return _err(str(exc))
        ctx.commit(candidate)
        return JSONResponse(
            {"ok": True, "tool": _definition(tool), "lifecycle": "draft"},
            status_code=201,
        )

    async def update_virtual(request: Request):  # noqa: PLR0911 - validation/rollback exits
        name = request.path_params["name"]
        payload = _clean_virtual_payload(await request.json())
        cfg = ctx.load()
        previous = _find(cfg, name)
        if previous is None:
            return _err("unknown virtual tool", 404)
        # PUT always creates/replaces an inactive draft. An active revision is
        # removed from the shared endpoint after persistence; activation is the
        # only operation that can put the edited definition back into service.
        payload = {**payload, "enabled": False}
        try:
            tool = cl.VirtualTool.model_validate(payload)
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))
        if tool.name != name and _find(cfg, tool.name) is not None:
            return _err("virtual tool name already exists")
        virtual_mod.ensure_backend_ids(cfg)
        tools = [tool if item.name == name else item for item in cfg.virtual_tools]
        try:
            candidate = GatewayConfig.model_validate(
                cfg.model_copy(update={"virtual_tools": tools}, deep=True).model_dump()
            )
            virtual_mod.build_virtual_tool(tool, candidate, ctx.registry, ctx.log)
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))
        ctx.commit(candidate)
        if previous.enabled:
            try:
                _hot_replace(candidate)
            except Exception as exc:  # noqa: BLE001
                ctx.commit(cfg, validate_virtual_refs=False)
                _hot_replace(cfg)
                return _err(
                    f"hot reload failed; previous definition restored: {exc}", 500
                )
        return JSONResponse(
            {"ok": True, "tool": _definition(tool), "lifecycle": "draft"}
        )

    async def validate_virtual(request: Request):
        cfg = ctx.load()
        tool = _find(cfg, request.path_params["name"])
        if tool is None:
            return _err("unknown virtual tool", 404)
        resolution = await _resolution(tool, cfg)
        return JSONResponse(resolution, status_code=200 if resolution["ok"] else 400)

    async def test_virtual(request: Request):
        cfg = ctx.load()
        tool = _find(cfg, request.path_params["name"])
        if tool is None:
            return _err("unknown virtual tool", 404)
        payload = await request.json()
        resolution = await _resolution(tool, cfg)
        if not resolution["ok"]:
            return JSONResponse(resolution, status_code=400)
        started = time.perf_counter()
        try:
            result = await virtual_mod.run_virtual(
                tool, payload.get("arguments") or {}, cfg, ctx.registry, ctx.log
            )
            receipt = result.model_dump(mode="json", by_alias=True)
            status = {
                "last_test": {
                    "ok": not result.is_error,
                    "status": "passed" if not result.is_error else "failed",
                    "ms": round((time.perf_counter() - started) * 1000, 1),
                }
            }
            ctx.hooks.setdefault("virtual_status", {})[tool.name] = status
            return JSONResponse(
                {"ok": not result.is_error, "result": receipt, **status}
            )
        except Exception as exc:  # noqa: BLE001
            status = {"last_test": {"ok": False, "status": "failed", "error": str(exc)}}
            ctx.hooks.setdefault("virtual_status", {})[tool.name] = status
            return _err(str(exc))

    async def activate_virtual(request: Request):
        cfg = ctx.load()
        name = request.path_params["name"]
        tool = _find(cfg, name)
        if tool is None:
            return _err("unknown virtual tool", 404)
        fingerprint = _consent_fingerprint(tool)
        consent_store = ctx.hooks.setdefault("virtual_consent_fingerprints", {})
        if fingerprint is None:
            consent_store.pop(name, None)
        else:
            consent_store[name] = fingerprint
        ctx.hooks.setdefault("virtual_status", {}).setdefault(name, {})[
            "consent_fingerprint"
        ] = fingerprint
        resolution = await _resolution(tool, cfg)
        if not resolution["ok"]:
            return JSONResponse(resolution, status_code=400)
        candidate = cfg.model_copy(deep=True)
        active = _find(candidate, name)
        assert active is not None
        active.enabled = True
        if active.router is not None:
            active.router.egress_consent_fingerprint = fingerprint
        try:
            candidate = GatewayConfig.model_validate(candidate.model_dump())
            virtual_mod.build_virtual_tool(active, candidate, ctx.registry, ctx.log)
        except Exception as exc:  # noqa: BLE001
            return _err(str(exc))
        # Full live resolution immediately above is stronger than the captured
        # baseline guard used for synchronous legacy mutations.
        ctx.commit(candidate, validate_virtual_refs=False)
        try:
            _hot_replace(candidate)
        except Exception as exc:  # noqa: BLE001
            ctx.commit(cfg, validate_virtual_refs=False)
            _hot_replace(cfg)
            return _err(f"activation failed; draft restored: {exc}", 500)
        return JSONResponse({"ok": True, "enabled": True, "reloaded": "hot"})

    async def disable_virtual(request: Request):
        cfg = ctx.load()
        tool = _find(cfg, request.path_params["name"])
        if tool is None:
            return _err("unknown virtual tool", 404)
        candidate = cfg.model_copy(deep=True)
        disabled = _find(candidate, tool.name)
        assert disabled is not None
        disabled.enabled = False
        candidate = GatewayConfig.model_validate(candidate.model_dump())
        ctx.commit(candidate)
        try:
            _hot_replace(candidate)
        except Exception as exc:  # noqa: BLE001
            ctx.commit(cfg, validate_virtual_refs=False)
            _hot_replace(cfg)
            return _err(f"disable failed; active definition restored: {exc}", 500)
        return JSONResponse({"ok": True, "enabled": False, "reloaded": "hot"})

    async def delete_virtual(request: Request):
        cfg = ctx.load()
        name = request.path_params["name"]
        if _find(cfg, name) is None:
            return _err("unknown virtual tool", 404)
        candidate = cfg.model_copy(
            update={
                "virtual_tools": [
                    tool for tool in cfg.virtual_tools if tool.name != name
                ]
            },
            deep=True,
        )
        candidate = GatewayConfig.model_validate(candidate.model_dump())
        ctx.commit(candidate)
        try:
            _hot_replace(candidate)
        except Exception as exc:  # noqa: BLE001
            ctx.commit(cfg, validate_virtual_refs=False)
            _hot_replace(cfg)
            return _err(f"delete failed; definition restored: {exc}", 500)
        return JSONResponse({"ok": True})

    return [
        Route("/admin/api/virtual-tools", list_virtual_tools, methods=["GET"]),
        Route("/admin/api/virtual-catalog", virtual_catalog, methods=["GET"]),
        Route(
            "/admin/api/virtual-tools",
            _needs_json(ctx.locked(create_virtual)),
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}",
            _needs_json(ctx.locked(update_virtual)),
            methods=["PUT"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}",
            ctx.locked(delete_virtual),
            methods=["DELETE"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}/validate",
            validate_virtual,
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}/test",
            _needs_json(test_virtual),
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}/activate",
            ctx.locked(activate_virtual),
            methods=["POST"],
        ),
        Route(
            "/admin/api/virtual-tools/{name}/disable",
            ctx.locked(disable_virtual),
            methods=["POST"],
        ),
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
            *_gateway_settings_routes(ctx),
            *_backend_routes(ctx),
            *_claude_routes(ctx),
            *_codex_routes(ctx),
            *_virtual_routes(ctx),
            *_ops_routes(ctx),
        ]
    )
