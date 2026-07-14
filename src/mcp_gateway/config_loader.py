"""Config loading for mcp-gateway.

Reads ``config.toml`` into validated Pydantic models, then maps those models
into the two things ``server.py`` needs:

1. A FastMCP proxy config dict (``{"mcpServers": {...}}``) for ``create_proxy``.
2. A ``ToolTransform`` that rewrites every broadcast text (tool name/title/
   description + each parameter name/description, hide params, disable tools).

Each backend is proxied ALONE on its own endpoint (``/<backend>/mcp``) and
registered as its own MCP server in Claude Code, so tools keep their BARE
original name — the old ``<backend>_`` prefix is gone (ADR-0002). Tool
transforms are keyed by that bare original name.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Literal

import structlog
import tomli_w
from fastmcp.server.transforms import ToolTransform, Transform
from fastmcp.tools.tool_transform import ArgTransformConfig, ToolTransformConfig
from pydantic import BaseModel, Field, field_validator, model_validator

from mcp_gateway import hooks as hooks_mod

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_SECRETS_PATH = "~/.config/mcp-gateway/secrets.env"


class ConfigError(RuntimeError):
    """Raised for any malformed or unresolvable configuration."""


def secrets_path() -> Path:
    """The gateway-scoped secrets file (``MCP_GATEWAY_SECRETS`` overrides)."""
    return Path(
        os.environ.get("MCP_GATEWAY_SECRETS", DEFAULT_SECRETS_PATH)
    ).expanduser()


_secrets_cache: dict[str, tuple[float, dict[str, str]]] = {}


def load_secrets() -> dict[str, str]:
    """Parse the gateway-scoped secrets file into a dict.

    KEY=VALUE lines; blank lines, ``#`` comments, and an ``export `` prefix are
    tolerated; surrounding single/double quotes on the value are stripped.
    Values stay OUT of ``os.environ`` on purpose: stdio backend subprocesses
    inherit the daemon's environment, and one backend must not be able to read
    secrets meant for another.

    Cached by (path, mtime) (#105): a config build expands many ``${VAR}`` values
    but only reads the file once; a freshly-edited secrets file is still picked up
    because its mtime changes (so a new secret works without a daemon restart).
    """
    path = secrets_path()
    if not path.is_file():
        return {}
    ckey = str(path)
    mtime = path.stat().st_mtime
    cached = _secrets_cache.get(ckey)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    secrets: dict[str, str] = {}
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :]
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        secrets[key.strip()] = value
    _secrets_cache[ckey] = (mtime, secrets)
    return secrets


def expand_env(value: str) -> str:
    """Replace every ``${VAR}`` in *value* from the environment, falling back
    to the gateway-scoped secrets file (:func:`secrets_path`).

    Raises :class:`ConfigError` if a referenced variable is found in neither,
    so a missing secret fails loudly at startup instead of sending an empty
    auth header.
    """

    secrets = load_secrets() if _ENV_PATTERN.search(value) else {}

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in os.environ:
            return os.environ[name]
        if name in secrets:
            return secrets[name]
        raise ConfigError(
            f"config references ${{{name}}} but {name!r} is set neither in the "
            f"environment nor in the gateway secrets file ({secrets_path()})"
        )

    return _ENV_PATTERN.sub(_sub, value)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class ParamOverride(BaseModel, extra="forbid"):
    """Rewrite (or hide) one parameter of a backend tool."""

    original: str
    name: str | None = None
    description: str | None = None
    hide: bool = False
    # #35: a fixed value the gateway injects on every call (FastMCP
    # ArgTransform default). Scalars only — mirrors ArgTransformConfig.default.
    # With a default set, hiding is safe even for a required param: Claude
    # never sees it, the backend always receives this value.
    default: str | int | float | bool | None = None


class ToolOverride(BaseModel, extra="forbid", populate_by_name=True):
    """Rewrite (or disable) one backend tool and its parameters."""

    original: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    enabled: bool = True
    # Pin this tool to load UPFRONT (exempt from Claude Code tool-search deferral)
    # via the tool's `_meta["anthropic/alwaysLoad"]`.
    always_load: bool = False
    # #16: behavior hooks — "module:function" specs resolved in the hooks dir
    # (see hooks.py; NEVER evaluated as code). The TOML key is `validate`; the
    # field is aliased because `validate` shadows a pydantic BaseModel attr.
    # validate(args: dict) rejects a call by raising ValueError(msg);
    # post_process(result) reshapes the backend's answer before the caller
    # sees it. Hand-authored in config.toml, read-only in the admin UI.
    validate_: str | None = Field(default=None, alias="validate")
    post_process: str | None = None
    params: list[ParamOverride] = Field(default_factory=list)

    @field_validator("validate_", "post_process")
    @classmethod
    def _check_hook_spec(cls, v: str | None) -> str | None:
        # Format-only check at load time (cheap, catches typos in a hand-edited
        # config); existence/importability is checked at transform-build time so
        # a missing hook FILE can't crash config load / boot.
        if v is not None and not hooks_mod.valid_spec(v):
            raise ConfigError(
                f"invalid hook spec {v!r}: use 'module:function' (a function in "
                f"a .py file inside the hooks directory)"
            )
        return v


class ResourceOverride(BaseModel, extra="forbid"):
    """Rewrite (or hide) one backend resource OR resource template (#15).

    Keyed by ``uri`` — the resource's URI (or a template's ``uriTemplate``),
    its wire IDENTITY. The URI is never rewritten (clients read by it); only
    the display text is editable. ``enabled=False`` hides the entry from the
    listing AND blocks reads through the gateway.
    """

    uri: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    enabled: bool = True


class PromptArgOverride(BaseModel, extra="forbid"):
    """Rewrite one prompt argument's description.

    Argument NAMES are deliberately not renameable: ``prompts/get`` carries an
    arguments dict the proxy forwards to the backend verbatim, so a renamed
    argument would never reach it.
    """

    original: str
    description: str | None = None


class PromptOverride(BaseModel, extra="forbid"):
    """Rewrite (or hide) one backend prompt and its argument descriptions (#15).

    Renames are real: the broadcast name changes and ``prompts/get`` for the
    new name reverse-maps to the backend's original (FastMCP's ``ProxyPrompt``
    preserves the backend name across a ``model_copy`` rename).
    """

    original: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    enabled: bool = True
    args: list[PromptArgOverride] = Field(default_factory=list)


class Backend(BaseModel, extra="forbid"):
    """One backend MCP server and its per-tool text overrides."""

    name: str
    # Optional display-only label for the admin UI (#42). Cosmetic ONLY: it does
    # NOT change routing, the endpoint URL, config keys, or the Claude Code
    # registration — all of those keep using `name`. None/empty -> show `name`.
    display_name: str | None = None
    # http == FastMCP streamable-http; "streamable-http" is the explicit alias,
    # "sse" is legacy SSE. http/streamable-http/sse are all url-based remote
    # transports; stdio spawns a local command. (issue #5)
    transport: Literal["http", "streamable-http", "sse", "stdio"]
    # http
    url: str | None = None
    auth_header: str | None = None
    auth_value: str | None = None
    # Extra static headers (#6) — values may reference ${ENV}. Merged with the
    # legacy auth_header/auth_value pair (the pair wins on a same-name clash).
    headers: dict[str, str] = Field(default_factory=dict)
    # OAuth-protected remote MCP (#6): "oauth" is passed straight through to
    # FastMCP's client config (RemoteMCPServer.auth), which runs the OAuth flow
    # (browser consent on first connect, cached tokens after).
    auth: Literal["oauth"] | None = None
    # A helper that prints a JSON object of headers to stdout (#6) — for tokens
    # resolved at connect time. It runs ONCE when the backend's client config is
    # built (mount / introspect), NOT per request and NOT on a timer, so its
    # output is fixed for the daemon's lifetime (#82). Suitable for a token valid
    # across the daemon's uptime (e.g. `gh auth token`); a truly short-lived token
    # that must rotate mid-session is NOT refreshed until the next restart. Two
    # forms (#81):
    #   - list[str] -> argv, run WITHOUT a shell (safe; no injection surface)
    #   - str       -> run via the shell (needed for $()/pipes), so it carries
    #                  FULL shell privilege — same trust as a stdio `command`.
    # Either way the config file is local-admin-owned, so this is not a new trust
    # boundary; the list form just removes the shell footgun for simple helpers.
    headers_helper: str | list[str] | None = None
    # stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # efficiency knob (parsed; shared-session reuse is a tier-2 optimisation,
    # see README "Session strategy" — default per-request sessions for now)
    stateless: bool = False
    # Pin ALL of this backend's tools to load upfront (per-tool meta on each).
    always_load: bool = False
    # Backend-level broadcast switch (#38). False -> NONE of this backend's tools
    # are broadcast to Claude (every tool disabled via transforms). Hot-reloadable:
    # the endpoint stays mounted and the backend process is not torn down.
    enabled: bool = True
    # Override this backend's server-level `instructions` (the always-loaded
    # blurb the backend sends at `initialize`). None -> use the captured original
    # (see admin defaults); a string replaces it in the gateway's composed
    # instructions. Set even when the backend sends none, to add your own.
    instructions: str | None = None
    tools: list[ToolOverride] = Field(default_factory=list)
    # #15: broadcast-text overrides for resources/templates (keyed by uri) and
    # prompts (keyed by original name) — the same diff-vs-default model as tools.
    resources: list[ResourceOverride] = Field(default_factory=list)
    prompts: list[PromptOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_transport(self) -> Backend:
        if self.transport != "stdio":  # http / streamable-http / sse — url-based
            if not self.url:
                raise ConfigError(
                    f"backend {self.name!r}: {self.transport} transport needs a url"
                )
        elif not self.command:
            raise ConfigError(f"backend {self.name!r}: stdio transport needs a command")
        if (self.auth_header is None) != (self.auth_value is None):
            raise ConfigError(
                f"backend {self.name!r}: set both auth_header and auth_value, "
                f"or neither"
            )
        return self

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        # #81: the name lands in a route mount (/<name>/mcp) and a defaults-file
        # path (<name>.json), so restrict it to the same MCP-safe identifier set
        # used for tool/param names — blocks '/', '..', and other traversal.
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", v):
            raise ConfigError(
                f"invalid backend name {v!r}: use only letters, digits, '_' or "
                f"'-' (max 64 chars)"
            )
        return v


def _is_loopback_host(host: str) -> bool:
    """True when ``host`` can only be reached from this machine.

    ``localhost`` and any loopback IP (127.0.0.0/8, ``::1``) qualify; every
    other hostname or address — including ones that HAPPEN to resolve to
    loopback — is treated as exposed, because we can't verify resolution at
    config-load time and the failure mode is an open admin API.
    """
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


# The reserved mount path for the composite endpoint (#14): all composite
# tools are served together at /<COMPOSITE_ROUTE>/mcp, so no backend may claim
# that name while composites are configured.
COMPOSITE_ROUTE = "composite"

_NAME_RE = r"[A-Za-z0-9_-]{1,64}"


class CompositeParam(BaseModel, extra="forbid"):
    """One exposed parameter of a composite tool (#14).

    This is authored surface, not a rewrite: the composite tool's schema is
    built entirely from these entries (there is no backend schema to diff
    against). Members map their own params onto these names via
    ``CompositeMember.args``.
    """

    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str | None = None
    required: bool = True
    # Optional params only: the schema default Claude sees when it omits the
    # param (a defaulted required param is a contradiction — rejected below).
    default: str | int | float | bool | None = None

    @model_validator(mode="after")
    def _check(self) -> CompositeParam:
        if not re.fullmatch(_NAME_RE, self.name):
            raise ConfigError(
                f"invalid composite param name {self.name!r}: use only letters, "
                f"digits, '_' or '-' (max 64 chars)"
            )
        if self.required and self.default is not None:
            raise ConfigError(
                f"composite param {self.name!r}: a required param cannot take a "
                f"default — set required = false"
            )
        if self.default is not None:
            # The default must satisfy the declared type or the emitted JSON
            # Schema contradicts itself. bool is special-cased first because
            # isinstance(True, int) is True in Python but not in JSON Schema.
            if isinstance(self.default, bool):
                ok = self.type == "boolean"
            elif self.type == "integer":
                ok = isinstance(self.default, int)
            elif self.type == "number":
                ok = isinstance(self.default, (int, float))
            elif self.type == "string":
                ok = isinstance(self.default, str)
            else:  # boolean, and the default wasn't a bool
                ok = False
            if not ok:
                raise ConfigError(
                    f"composite param {self.name!r}: default {self.default!r} "
                    f"does not match type {self.type!r}"
                )
        return self


class CompositeMember(BaseModel, extra="forbid"):
    """One member tool a composite fans out to (#14).

    ``tool`` is the EXPOSED tool name on the backend's gateway endpoint (i.e.
    post-rename, exactly what Claude sees) — member calls go through the
    gateway's own live proxy, so every override applies.
    """

    backend: str
    tool: str
    # Label used in the merged output ("## <label> — ok"); default backend/tool.
    label: str | None = None
    # #21 "keyword" strategy: this member is selected when ANY of these regexes
    # matches the call's arg text (case-insensitive search). Ignored by "all".
    route_patterns: list[str] = Field(default_factory=list)
    # #21 "llm" strategy: the routing condition the router model reads for this
    # member ("use for code questions", …). Ignored by "all" and "keyword".
    route_description: str | None = None
    # member param name -> composite param name (the value Claude supplied for
    # that composite param is forwarded under the member's own param name).
    args: dict[str, str] = Field(default_factory=dict)
    # member param name -> fixed value injected on every call (scalars only,
    # mirroring ParamOverride.default).
    static_args: dict[str, str | int | float | bool] = Field(default_factory=dict)
    # Per-member deadline in seconds; a member that misses it reports itself as
    # timed out in the merged output and never sinks the composite call.
    timeout: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> CompositeMember:
        # backend is cross-checked against the configured backend names at the
        # GatewayConfig level; tool has no such registry (it's the backend's
        # own exposed name), so at least fail fast on a blank one.
        if not self.tool.strip():
            raise ConfigError(
                f"composite member on backend {self.backend!r}: tool name "
                f"must not be empty"
            )
        overlap = set(self.args) & set(self.static_args)
        if overlap:
            raise ConfigError(
                f"composite member {self.backend}/{self.tool}: param(s) "
                f"{sorted(overlap)} appear in both args and static_args"
            )
        for pat in self.route_patterns:
            try:
                re.compile(pat)
            except re.error as exc:
                raise ConfigError(
                    f"composite member {self.backend}/{self.tool}: invalid "
                    f"route_pattern {pat!r}: {exc}"
                ) from None
        return self


class CompositeRouter(BaseModel, extra="forbid"):
    """Per-composite routing settings (#21) — the ``[composites.router]`` table.

    Used by the ``"keyword"`` strategy (``fallback`` only) and the ``"llm"``
    strategy (all fields). The ``api_key`` is a ``${ENV}`` reference resolved
    ONCE at boot via :func:`expand_env` — like ``bearer_token``, the raw value
    never sits in the config file and stays out of ``os.environ``.
    """

    # OpenRouter model slug for the "llm" strategy — pick something cheap/fast;
    # the router only ever emits a tiny JSON array.
    model: str = "openai/gpt-4o-mini"
    # ${ENV} reference to the OpenRouter API key. Required for strategy="llm".
    api_key: str | None = None
    # Extra routing policy text appended to the router prompt (llm only).
    conditions: str | None = None
    # Router deadline in seconds (llm only). Kept SHORT on purpose: a router
    # outage falls back, it never stalls the composite call.
    timeout: float = Field(default=3.0, gt=0)
    # Where a call goes when routing decides nothing: "all" (every member) or
    # one member's label. Applies to keyword no-match AND every llm failure.
    fallback: str = "all"


class Composite(BaseModel, extra="forbid"):
    """One synthetic multi-backend tool (#14, spec §17.4).

    Exposed as a single tool on the shared ``/composite/mcp`` endpoint; a call
    fans out to every selected member concurrently, gathers per-member results
    (status, latency, text — a failed member reports itself instead of sinking
    the call), and returns one labeled merge.
    """

    name: str
    description: str
    enabled: bool = True
    # Pin the composite tool to load upfront (same lever as ToolOverride).
    always_load: bool = False
    # Member-selection strategy (#21). "all" = fan out to every member;
    # "keyword" = per-member route_patterns regexes against the call's arg
    # text; "llm" = an OpenRouter-backed router picks a subset per call.
    strategy: Literal["all", "keyword", "llm"] = "all"
    # Router settings for "keyword"/"llm" (see CompositeRouter).
    router: CompositeRouter | None = None
    params: list[CompositeParam] = Field(default_factory=list)
    members: list[CompositeMember]

    @model_validator(mode="after")
    def _check(self) -> Composite:
        if not re.fullmatch(_NAME_RE, self.name):
            raise ConfigError(
                f"invalid composite name {self.name!r}: use only letters, "
                f"digits, '_' or '-' (max 64 chars)"
            )
        if not self.members:
            raise ConfigError(f"composite {self.name!r} has no members")
        pnames = [p.name for p in self.params]
        dupes = {n for n in pnames if pnames.count(n) > 1}
        if dupes:
            raise ConfigError(
                f"composite {self.name!r}: duplicate param name(s): {sorted(dupes)}"
            )
        declared = set(pnames)
        for m in self.members:
            unknown = [cp for cp in m.args.values() if cp not in declared]
            if unknown:
                raise ConfigError(
                    f"composite {self.name!r}: member {m.backend}/{m.tool} maps "
                    f"undeclared composite param(s): {sorted(set(unknown))}"
                )
        # #21 strategy plumbing: fail at LOAD, never at call time.
        if self.strategy == "llm" and (self.router is None or not self.router.api_key):
            raise ConfigError(
                f"composite {self.name!r}: strategy 'llm' needs a "
                f"[composites.router] table with an api_key (a ${{ENV}} reference)"
            )
        if self.strategy == "keyword" and not any(
            m.route_patterns for m in self.members
        ):
            raise ConfigError(
                f"composite {self.name!r}: strategy 'keyword' needs "
                f"route_patterns on at least one member"
            )
        if self.router is not None and self.router.fallback != "all":
            labels = {m.label or f"{m.backend}/{m.tool}" for m in self.members}
            if self.router.fallback not in labels:
                raise ConfigError(
                    f"composite {self.name!r}: router fallback "
                    f"{self.router.fallback!r} matches no member label "
                    f'(use "all" or one of {sorted(labels)})'
                )
        return self


class GatewayConfig(BaseModel, extra="forbid"):
    """Top-level gateway configuration."""

    host: str = "127.0.0.1"
    port: int = 9100
    log_file: str = "~/.local/state/mcp-gateway/gateway.log"
    # #43: scheduled re-introspection interval in seconds. OFF by default (0) —
    # the event-driven triggers (post-mount refresh, tools/list_changed, admin
    # page load) cover everything but a long-lived remote backend that hot-swaps
    # tools silently; set an interval only for that rare case.
    introspect_interval: int = Field(default=0, ge=0)
    # Optional bearer token required on every backend MCP endpoint (#26) —
    # defense-in-depth on the loopback bind. Store a ${ENV} ref, never the raw
    # value; the server resolves it ONCE at startup via expand_env (a missing
    # var fails loudly), not per request. /admin, /health and /ready stay open
    # (see server.BearerAuthMiddleware).
    bearer_token: str | None = None
    backends: list[Backend] = Field(default_factory=list)
    # #14: synthetic multi-backend tools, all served at /composite/mcp.
    composites: list[Composite] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_backends(self) -> GatewayConfig:
        if not self.backends:
            raise ConfigError("config has no [[backends]]")
        names = [b.name for b in self.backends]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ConfigError(f"duplicate backend name(s): {sorted(dupes)}")
        cnames = [c.name for c in self.composites]
        cdupes = {n for n in cnames if cnames.count(n) > 1}
        if cdupes:
            raise ConfigError(f"duplicate composite name(s): {sorted(cdupes)}")
        if self.composites:
            # The shared composite endpoint mounts at /composite/mcp — a backend
            # of that name would collide with the route. Gated on composites
            # actually existing so a legacy config without them keeps loading.
            if COMPOSITE_ROUTE in names:
                raise ConfigError(
                    f"backend name {COMPOSITE_ROUTE!r} is reserved for the "
                    f"composite endpoint while [[composites]] are configured"
                )
            known = set(names)
            for c in self.composites:
                for m in c.members:
                    if m.backend not in known:
                        raise ConfigError(
                            f"composite {c.name!r}: member references unknown "
                            f"backend {m.backend!r}"
                        )
        # #18: a non-loopback bind exposes config writes and tool execution to
        # the network, so it is refused outright without the bearer token.
        if self.bearer_token is None and not _is_loopback_host(self.host):
            raise ConfigError(
                f"host {self.host!r} is not loopback: binding beyond this "
                f"machine requires bearer_token (see docs/security.md)"
            )
        return self


# ---------------------------------------------------------------------------
# Loading + mapping
# ---------------------------------------------------------------------------


def ensure_config(path: str | Path) -> None:
    """Seed *path* from ``config.default.toml`` if it doesn't exist yet.

    ``config.toml`` is the live, admin-managed file (gitignored — it gets
    regenerated on every UI save). ``config.default.toml`` is the committed
    runnable seed (both backends passthrough). On a fresh clone the live file is
    missing, so copy the default into place once.
    """
    p = Path(path).expanduser()
    if p.is_file():
        return
    default = Path(__file__).resolve().parent / "config.default.toml"
    if not default.is_file():
        raise ConfigError(
            f"no {p} and no config.default.toml to seed it from ({default})"
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(default.read_text(encoding="utf-8"), encoding="utf-8")


def load(path: str | Path) -> GatewayConfig:
    """Read and validate *path* (a ``config.toml``), seeding it if absent."""
    ensure_config(path)
    p = Path(path).expanduser()
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")
    with p.open("rb") as fh:
        raw = tomllib.load(fh)
    return GatewayConfig.model_validate(raw)


def _run_headers_helper(b: Backend) -> dict[str, str]:
    """Run ``headers_helper`` and parse its stdout as a JSON object of headers.

    Raises ConfigError on a non-zero exit, timeout, or non-object output — a
    misconfigured helper must fail loudly, not silently connect unauthenticated.
    """
    helper = b.headers_helper
    # A str runs via the shell (needed for $()/pipes) and carries full shell
    # privilege; a list is argv run WITHOUT a shell (no injection surface). Both
    # are local-admin-owned config — same trust as a stdio `command` (#81).
    is_shell = isinstance(helper, str)
    try:
        out = subprocess.run(
            helper,
            shell=is_shell,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        # OSError covers a missing executable in the list (no-shell) form.
        raise ConfigError(f"backend {b.name!r}: headers_helper failed: {exc}") from None
    try:
        data = json.loads(out)
    except ValueError:
        raise ConfigError(
            f"backend {b.name!r}: headers_helper must print a JSON object"
        ) from None
    if not isinstance(data, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in data.items()
    ):
        raise ConfigError(
            f"backend {b.name!r}: headers_helper must print a JSON object of "
            f"string headers"
        )
    return data


def backend_entry(b: Backend) -> dict:
    """The FastMCP client-config entry for one backend (url/headers or command/env).

    The transport string is passed through verbatim: FastMCP's
    ``RemoteMCPServer.transport`` accepts ``http``/``streamable-http`` (both ->
    StreamableHttpTransport) and ``sse`` (-> SSETransport) — see
    ``fastmcp/mcp_config.py``. All three are url-based and share the url/auth shape;
    only ``stdio`` takes a command.
    """
    if b.transport != "stdio":  # http / streamable-http / sse — url-based
        entry: dict = {"url": expand_env(b.url or ""), "transport": b.transport}
        headers: dict[str, str] = {}
        if b.headers_helper:  # lowest precedence: refreshed on each config build
            headers.update(_run_headers_helper(b))
        headers.update({k: expand_env(v) for k, v in b.headers.items()})
        if b.auth_header and b.auth_value:  # legacy single pair wins on clash
            headers[b.auth_header] = expand_env(b.auth_value)
        if headers:
            entry["headers"] = headers
        if b.auth:
            entry["auth"] = b.auth
    else:
        entry = {"command": b.command, "args": list(b.args), "transport": "stdio"}
        if b.env:
            entry["env"] = {k: expand_env(v) for k, v in b.env.items()}
    return entry


def to_proxy_config_one(b: Backend) -> dict:
    """Single-backend proxy config (``{"mcpServers": {b.name: ...}}``).

    The live server builds ONE proxy per backend from this, so each backend is
    its own MCP endpoint and its tools come back un-prefixed (bare names).
    """
    return {"mcpServers": {b.name: backend_entry(b)}}


def to_proxy_config(cfg: GatewayConfig) -> dict:
    """All-backends proxy config. Kept for tooling/tests; the live server now
    builds one single-backend proxy per backend via :func:`to_proxy_config_one`."""
    return {"mcpServers": {b.name: backend_entry(b) for b in cfg.backends}}


# Tool `_meta` hint that exempts a tool from Claude Code's tool-search deferral
# (loads it upfront). See references/mcp.md (anthropic/alwaysLoad).
ALWAYS_LOAD_META = {"anthropic/alwaysLoad": True}


def tool_hook_fn(backend_name: str, tool: ToolOverride):
    """Resolve one override's hook specs (#16) into ``(transform_fn, error)``.

    ``(None, None)`` when the tool has no hooks. A load failure (missing
    module/function, import crash) must be loud but PER TOOL: it never raises
    — the mount and every other tool stay up — and it never fails open. The
    returned stand-in errors every call to THIS tool with the load failure
    (see hooks.make_failing_hook_fn), the structured log gets a
    ``hook_load_error`` line, and the admin state shows the same error.
    """
    if tool.validate_ is None and tool.post_process is None:
        return None, None
    try:
        vfn = hooks_mod.load_hook(tool.validate_) if tool.validate_ else None
        pfn = hooks_mod.load_hook(tool.post_process) if tool.post_process else None
    except hooks_mod.HookError as exc:
        structlog.get_logger("mcp-gateway").error(
            "hook_load_error",
            backend=backend_name,
            tool=tool.original,
            error=str(exc),
        )
        return hooks_mod.make_failing_hook_fn(tool.original, str(exc)), str(exc)
    return hooks_mod.make_hook_fn(vfn, pfn), None


def build_transforms(  # noqa: PLR0912 — one branch per override field; splitting would scatter the transform assembly
    cfg: GatewayConfig,
    backend: Backend,
    all_tools: dict[str, list[str]] | None = None,
    captured_meta: dict[str, dict[str, dict]] | None = None,
) -> tuple[ToolTransform, dict[str, str]]:
    """Build the ``ToolTransform`` for ONE *backend*'s endpoint, plus a
    ``{tool_name: backend}`` index for the startup reconcile.

    Each backend is proxied alone, so transform keys are the BARE tool names
    (no ``<backend>_`` prefix). The index lets the server reconcile configured
    tools against the live tool list at startup and warn on any name that does
    not match a real backend tool (a typo in ``original``).

    *all_tools* maps ``backend name -> [original tool names]`` (from captured
    defaults). It is needed only to apply a **per-backend** ``always_load`` to
    tools that have no other override; per-tool ``always_load`` works without it.

    *captured_meta* maps ``backend name -> {original tool name -> its captured
    _meta}``. Used so a pin (``always_load``) MERGES the alwaysLoad flag into the
    backend's original ``_meta`` instead of replacing it (#91); omit it and a pin
    just sets the flag alone (the pre-fix behaviour).
    """
    transforms: dict[str, ToolTransformConfig] = {}
    index: dict[str, str] = {}
    b = backend
    bmeta = (captured_meta or {}).get(b.name, {})

    def pin_meta(original: str) -> dict:
        # #91: pinning must MERGE the alwaysLoad flag into the backend's captured
        # _meta, not replace it (FastMCP's ToolTransformConfig.meta REPLACES the
        # tool's meta). A bare dict here would silently drop reserved keys such as
        # io.modelcontextprotocol/related-task. Our flag wins on any key clash.
        return {**(bmeta.get(original) or {}), **ALWAYS_LOAD_META}

    for tool in b.tools:
        key = tool.original
        index[key] = b.name
        arguments: dict[str, ArgTransformConfig] = {}
        for param in tool.params:
            arg_kwargs: dict = {"hide": param.hide}
            if param.name is not None:
                arg_kwargs["name"] = param.name
            if param.description is not None:
                arg_kwargs["description"] = param.description
            if param.default is not None:  # #35: injected on every call
                arg_kwargs["default"] = param.default
            arguments[param.original] = ArgTransformConfig(**arg_kwargs)

        # Backend disabled (#38) forces every tool off, whatever its own state.
        tc_kwargs: dict = {"enabled": tool.enabled and b.enabled}
        if tool.name is not None:
            tc_kwargs["name"] = tool.name
        if tool.title is not None:
            tc_kwargs["title"] = tool.title
        if tool.description is not None:
            tc_kwargs["description"] = tool.description
        if arguments:
            tc_kwargs["arguments"] = arguments
        # A disabled backend has no eager tools: gate the pin on b.enabled so
        # "disabled wins over pin" holds at the per-tool level too (#116) — else a
        # disabled backend's overridden tool would emit {enabled: False, meta:
        # alwaysLoad} (off, yet flagged eager). Mirrors the b.enabled gate on the
        # per-backend pin below.
        if b.enabled and (tool.always_load or b.always_load):
            tc_kwargs["meta"] = pin_meta(key)
        # #16: behavior hooks ride the same transform (FastMCP's config model
        # has no transform_fn field, so a hooked tool uses the gateway's
        # subclass). Hooks are loaded here — every transform (re)build picks up
        # an edited hook file — and a load failure fails closed per tool.
        hook_fn, _hook_error = tool_hook_fn(b.name, tool)
        if hook_fn is not None:
            transforms[key] = hooks_mod.HookedToolTransformConfig(
                **tc_kwargs, transform_fn=hook_fn
            )
        else:
            transforms[key] = ToolTransformConfig(**tc_kwargs)

    # Backend disabled (#38): force EVERY live tool off, including ones with no
    # override entry. Runs before the always_load pin so "disabled" wins over a
    # pin, and per-tool overrides above already got `enabled and b.enabled`.
    if not b.enabled and all_tools and b.name in all_tools:
        for original in all_tools[b.name]:
            if original not in transforms:
                transforms[original] = ToolTransformConfig(enabled=False)
                index[original] = b.name

    # Per-backend always_load: also pin tools that have no override entry. Skipped
    # when the backend is disabled (nothing to pin — every tool is off).
    if b.always_load and b.enabled and all_tools and b.name in all_tools:
        for original in all_tools[b.name]:
            if original not in transforms:
                transforms[original] = ToolTransformConfig(
                    enabled=True, meta=pin_meta(original)
                )
                index[original] = b.name
    return ToolTransform(transforms), index


class ResourcePromptTransform(Transform):
    """Rewrites resource / resource-template / prompt broadcast text for ONE
    backend's endpoint (#15).

    FastMCP 3.4.4 has no config-driven analog of ``ToolTransform`` for
    resources and prompts, but its ``Transform`` base class exposes the same
    list/get hooks — so the gateway supplies its own:

    - **resources + templates** (keyed by ``uri`` / ``uriTemplate``): name,
      title, description rewrites; ``enabled=False`` hides the entry from the
      listing and blocks reads through the gateway. URIs are never rewritten
      (they are the identity clients read by).
    - **prompts** (keyed by original name): name/title/description and
      per-argument description rewrites; renames reverse-map on ``prompts/get``
      (``ProxyPrompt.model_copy`` preserves ``_backend_name``, so the render
      still reaches the backend under its real name). ``enabled=False`` hides
      and blocks.

    Duplicate prompt TARGET names raise ``ValueError`` at build time, exactly
    like FastMCP's ``ToolTransform`` — the admin dry-build turns that into a
    400 instead of a persisted config that fails every mount.
    """

    # Resource URIs pass through pydantic's AnyUrl, which normalizes some forms
    # (e.g. ``file://a.txt`` -> ``file://a.txt/``). Key overrides on a
    # trailing-slash-insensitive form so a hand-written config URI still matches
    # the live object.
    @staticmethod
    def _norm_uri(uri) -> str:
        return str(uri).rstrip("/")

    def __init__(self, backend: Backend) -> None:
        self._backend_enabled = backend.enabled
        self._resources: dict[str, ResourceOverride] = {
            self._norm_uri(r.uri): r for r in backend.resources
        }
        self._prompts: dict[str, PromptOverride] = {
            p.original: p for p in backend.prompts
        }
        # broadcast prompt name -> original, for prompts/get reverse-mapping
        self._prompt_reverse: dict[str, str] = {}
        for original, ov in self._prompts.items():
            target = ov.name or original
            if target in self._prompt_reverse:
                raise ValueError(
                    f"prompt overrides have duplicate target name {target!r}: "
                    f"both {self._prompt_reverse[target]!r} and {original!r} "
                    f"map to it"
                )
            self._prompt_reverse[target] = original

    def __repr__(self) -> str:
        return (
            f"ResourcePromptTransform(resources={list(self._resources)!r}, "
            f"prompts={list(self._prompts)!r})"
        )

    @staticmethod
    def _text_update(ov) -> dict:
        update: dict = {}
        if ov.name is not None:
            update["name"] = ov.name
        if ov.title is not None:
            update["title"] = ov.title
        if ov.description is not None:
            update["description"] = ov.description
        return update

    def _apply_resource(self, resource, ov: ResourceOverride):
        update = self._text_update(ov)
        return resource.model_copy(update=update) if update else resource

    def _apply_prompt(self, prompt, ov: PromptOverride):
        # ONE model_copy so ProxyPrompt's rename bookkeeping (_backend_name)
        # sees the name change on the first (only) copy.
        update = self._text_update(ov)
        arg_desc = {a.original: a.description for a in ov.args if a.description}
        if arg_desc and prompt.arguments:
            update["arguments"] = [
                a.model_copy(update={"description": arg_desc[a.name]})
                if a.name in arg_desc
                else a
                for a in prompt.arguments
            ]
        return prompt.model_copy(update=update) if update else prompt

    # --- resources ---------------------------------------------------------

    async def list_resources(self, resources):
        if not self._backend_enabled:
            return []
        out = []
        for r in resources:
            ov = self._resources.get(self._norm_uri(r.uri))
            if ov is None:
                out.append(r)
            elif ov.enabled:
                out.append(self._apply_resource(r, ov))
        return out

    async def get_resource(self, uri, call_next, *, version=None):
        # URIs are never rewritten, so the lookup key passes through unchanged.
        resource = await call_next(uri, version=version)
        if resource is None:
            return None
        ov = self._resources.get(self._norm_uri(resource.uri))
        if ov is None:
            return resource
        if not self._backend_enabled or not ov.enabled:
            return None  # hidden -> not just unlisted, unreadable too
        return self._apply_resource(resource, ov)

    # --- resource templates (same override list, keyed by uriTemplate) ------

    async def list_resource_templates(self, templates):
        if not self._backend_enabled:
            return []
        out = []
        for t in templates:
            ov = self._resources.get(self._norm_uri(t.uri_template))
            if ov is None:
                out.append(t)
            elif ov.enabled:
                out.append(self._apply_resource(t, ov))
        return out

    async def get_resource_template(self, uri, call_next, *, version=None):
        template = await call_next(uri, version=version)
        if template is None:
            return None
        ov = self._resources.get(self._norm_uri(template.uri_template))
        if ov is None:
            return template
        if not self._backend_enabled or not ov.enabled:
            return None
        return self._apply_resource(template, ov)

    # --- prompts -------------------------------------------------------------

    async def list_prompts(self, prompts):
        if not self._backend_enabled:
            return []
        out = []
        for p in prompts:
            ov = self._prompts.get(p.name)
            if ov is None:
                out.append(p)
            elif ov.enabled:
                out.append(self._apply_prompt(p, ov))
        return out

    async def get_prompt(self, name, call_next, *, version=None):
        original = self._prompt_reverse.get(name, name)
        prompt = await call_next(original, version=version)
        if prompt is None:
            return None
        ov = self._prompts.get(original)
        if ov is None:
            return prompt if prompt.name == name else None
        if not self._backend_enabled or not ov.enabled:
            return None
        transformed = self._apply_prompt(prompt, ov)
        # a renamed prompt only answers to its broadcast name (mirrors
        # ToolTransform: the original name of a renamed tool is a miss)
        return transformed if transformed.name == name else None


def build_resource_prompt_transform(
    backend: Backend,
) -> ResourcePromptTransform | None:
    """The resource/prompt rewrite layer for ONE backend (#15), or ``None``
    when there is nothing to rewrite (passthrough costs nothing). A DISABLED
    backend always gets one — it hides every resource/prompt, mirroring how
    #38 forces every tool off (defense in depth; a disabled backend is
    normally unmounted anyway)."""
    if backend.enabled and not backend.resources and not backend.prompts:
        return None
    return ResourcePromptTransform(backend)


def backend_instructions(
    backend: Backend, captured: dict[str, str | None]
) -> str | None:
    """The server-level ``instructions`` for ONE backend's endpoint.

    Its override (``Backend.instructions``) if set, else the captured original;
    ``None`` if neither (the endpoint stays silent). Because each backend is now
    its own MCP endpoint, it carries only its own blurb — so each gets Claude
    Code's full per-server ~2KB instructions budget instead of all backends
    sharing one (issue #29).

    A DISABLED backend broadcasts nothing at all (#72): its tools are already
    all disabled via transforms (#38), and its instructions must go too — nil,
    not just tool-less. Both serving paths (mount + hot-reload) call this, and
    the enable toggle hot-reloads, so the blurb comes and goes live.
    """
    if not backend.enabled:
        return None
    eff = (
        backend.instructions
        if backend.instructions is not None
        else captured.get(backend.name)
    )
    return eff.strip() if (eff and eff.strip()) else None


def to_raw(cfg: GatewayConfig) -> dict:  # noqa: PLR0915 — field-by-field TOML serialization; nested helpers already factored
    """Convert a GatewayConfig back to the plain dict shape of config.toml,
    omitting None/empty fields so the written file stays minimal and clean.
    """

    def _backend(b: Backend) -> dict:  # noqa: PLR0912 — one branch per optional config field
        d: dict = {"name": b.name, "transport": b.transport}
        if b.display_name:
            d["display_name"] = b.display_name
        if b.transport != "stdio":  # http / streamable-http / sse — url-based
            d["url"] = b.url
            if b.auth_header and b.auth_value:
                d["auth_header"] = b.auth_header
                d["auth_value"] = b.auth_value
            if b.headers:
                d["headers"] = dict(b.headers)
            if b.auth:
                d["auth"] = b.auth
            if b.headers_helper:
                d["headers_helper"] = b.headers_helper
        else:
            d["command"] = b.command
            if b.args:
                d["args"] = list(b.args)
            if b.env:
                d["env"] = dict(b.env)
        d["stateless"] = b.stateless
        if b.always_load:
            d["always_load"] = True
        if not b.enabled:  # default True — only persist the off state (#38)
            d["enabled"] = False
        if b.instructions is not None:
            d["instructions"] = b.instructions
        tools = [_tool(t) for t in b.tools]
        if tools:
            d["tools"] = tools
        resources = [_resource(r) for r in b.resources]
        if resources:
            d["resources"] = resources
        prompts = [_prompt(p) for p in b.prompts]
        if prompts:
            d["prompts"] = prompts
        return d

    def _tool(t: ToolOverride) -> dict:
        d: dict = {"original": t.original}
        if t.name is not None:
            d["name"] = t.name
        if t.title is not None:
            d["title"] = t.title
        if t.description is not None:
            d["description"] = t.description
        d["enabled"] = t.enabled
        if t.always_load:
            d["always_load"] = True
        if t.validate_ is not None:  # #16: TOML key is `validate` (field aliased)
            d["validate"] = t.validate_
        if t.post_process is not None:
            d["post_process"] = t.post_process
        params = [_param(p) for p in t.params]
        if params:
            d["params"] = params
        return d

    def _resource(r: ResourceOverride) -> dict:
        d: dict = {"uri": r.uri}
        if r.name is not None:
            d["name"] = r.name
        if r.title is not None:
            d["title"] = r.title
        if r.description is not None:
            d["description"] = r.description
        d["enabled"] = r.enabled
        return d

    def _prompt(p: PromptOverride) -> dict:
        d: dict = {"original": p.original}
        if p.name is not None:
            d["name"] = p.name
        if p.title is not None:
            d["title"] = p.title
        if p.description is not None:
            d["description"] = p.description
        d["enabled"] = p.enabled
        args = [
            {"original": a.original, "description": a.description}
            for a in p.args
            if a.description is not None
        ]
        if args:
            d["args"] = args
        return d

    def _param(p: ParamOverride) -> dict:
        d: dict = {"original": p.original}
        if p.name is not None:
            d["name"] = p.name
        if p.description is not None:
            d["description"] = p.description
        d["hide"] = p.hide
        if p.default is not None:
            d["default"] = p.default
        return d

    def _composite(c: Composite) -> dict:  # noqa: PLR0912 — one branch per optional config field
        d: dict = {"name": c.name, "description": c.description}
        if not c.enabled:  # default True — only persist the off state
            d["enabled"] = False
        if c.always_load:
            d["always_load"] = True
        if c.strategy != "all":
            d["strategy"] = c.strategy
        if c.router is not None:  # #21 — [composites.router]
            rd: dict = {"model": c.router.model}
            if c.router.api_key is not None:
                rd["api_key"] = c.router.api_key
            if c.router.conditions is not None:
                rd["conditions"] = c.router.conditions
            if c.router.timeout != 3.0:  # noqa: PLR2004 — the field default above
                rd["timeout"] = c.router.timeout
            if c.router.fallback != "all":
                rd["fallback"] = c.router.fallback
            d["router"] = rd
        params = []
        for p in c.params:
            pd: dict = {"name": p.name, "type": p.type}
            if p.description is not None:
                pd["description"] = p.description
            if not p.required:
                pd["required"] = False
            if p.default is not None:
                pd["default"] = p.default
            params.append(pd)
        if params:
            d["params"] = params
        members = []
        for m in c.members:
            md: dict = {"backend": m.backend, "tool": m.tool}
            if m.label:
                md["label"] = m.label
            if m.route_patterns:  # #21 keyword strategy
                md["route_patterns"] = list(m.route_patterns)
            if m.route_description is not None:  # #21 llm strategy
                md["route_description"] = m.route_description
            if m.args:
                md["args"] = dict(m.args)
            if m.static_args:
                md["static_args"] = dict(m.static_args)
            if m.timeout != 30.0:  # noqa: PLR2004 — the field default above
                md["timeout"] = m.timeout
            members.append(md)
        d["members"] = members
        return d

    out: dict = {
        "host": cfg.host,
        "port": cfg.port,
        "log_file": cfg.log_file,
    }
    if cfg.introspect_interval:  # default 0 (off) — only persist when set (#43)
        out["introspect_interval"] = cfg.introspect_interval
    if cfg.bearer_token is not None:  # default None — only persist when set (#26)
        out["bearer_token"] = cfg.bearer_token
    out["backends"] = [_backend(b) for b in cfg.backends]
    if cfg.composites:  # #14 — only persist when configured
        out["composites"] = [_composite(c) for c in cfg.composites]
    return out


_GENERATED_HEADER = (
    "# mcp-gateway config. Managed by the admin UI at http://127.0.0.1:9100/admin.\n"
    "# Hand-edits are fine but comments are not preserved on UI save.\n"
    "# Secrets: use ${ENV_VAR} refs only; values come from the environment.\n\n"
)


def dump_toml(cfg: GatewayConfig) -> str:
    """Serialize a GatewayConfig to a config.toml string."""
    return _GENERATED_HEADER + tomli_w.dumps(to_raw(cfg))


def save(cfg: GatewayConfig, path: str | Path) -> None:
    """Write *cfg* to *path* as TOML, atomically and durably.

    Writes a temp file, fsyncs it, atomically renames over the target, then
    fsyncs the directory — so an unexpected crash/power-loss after this returns
    leaves the config intact (never a partial file).
    """
    p = Path(path).expanduser()
    tmp = p.with_suffix(p.suffix + ".tmp")
    data = dump_toml(cfg)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)
    dir_fd = os.open(str(p.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


if __name__ == "__main__":
    # Quick self-check: `uv run python -m mcp_gateway.config_loader [config.toml]`
    path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = load(path)
    # build_transforms is per-backend now (#29); accumulate every backend's index.
    all_index: dict[str, str] = {}
    for _b in cfg.backends:
        _transforms, index = build_transforms(cfg, _b)
        all_index.update(index)
    print(f"loaded {len(cfg.backends)} backend(s); {len(all_index)} tool override(s)")
    for key, backend in all_index.items():
        print(f"  transform key: {key:40s} <- backend {backend}")
    print("proxy config:")
    print(json.dumps(to_proxy_config(cfg), indent=2))
