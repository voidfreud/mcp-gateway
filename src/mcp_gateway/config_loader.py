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

import hashlib
import ipaddress
import json
import math
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
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from mcp_gateway import hooks as hooks_mod
from mcp_gateway import logging_setup

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_SECRETS_PATH = "~/.config/mcp-gateway/secrets.env"

# #157: default post-mount refresh age gate — skip re-capturing a baseline
# younger than 24h at mount time (see GatewayConfig.baseline_max_age).
DEFAULT_BASELINE_MAX_AGE = 86_400

# Bound each public ``tools/list`` response without changing the independent
# per-backend topology.  FastMCP's proxy client fully consumes an upstream
# catalog before applying gateway transforms; this value controls only the
# gateway-to-client response pages.
DOWNSTREAM_TOOLS_PAGE_SIZE = 50

# Names a backend can never take: each backend mounts at ``/<name>`` and the
# unmount path strips routes by path-string equality, so a backend named after
# a built-in route would shadow it — and removing that backend would strip the
# built-in route itself. "health"/"ready" are also BearerAuthMiddleware
# exemptions, so a same-named backend would serve without auth.
RESERVED_BACKEND_NAMES = frozenset({"virtual", "admin", "health", "ready"})


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


def expand_env_required(value: str, what: str) -> str:
    """Expand *value* like :func:`expand_env`, then reject an empty result.

    A configured auth value (e.g. ``bearer_token = "${VAR}"``) whose variable
    exists but is EMPTY must fail loudly: an empty expansion is otherwise
    indistinguishable from "no token configured" downstream and would silently
    disable authentication.
    """
    expanded = expand_env(value)
    if not expanded:
        raise ConfigError(
            f"{what} is configured but expands to an empty string — refusing "
            "to run with authentication silently disabled"
        )
    return expanded


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

    @field_validator("default")
    @classmethod
    def _default_must_be_finite(cls, value):
        if isinstance(value, float) and not math.isfinite(value):
            raise ConfigError("parameter override default must be finite")
        return value


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
    # #162: per-tool output budget, broadcast as
    # `_meta["anthropic/maxResultSizeChars"]`. Claude Code caps MCP tool output
    # at 25k tokens (MAX_MCP_OUTPUT_TOKENS) but honors this per-tool char cap
    # for text content — raise it for bulk readers, lower it for chatty tools.
    # None = no cap override (the client default applies).
    max_result_chars: int | None = None
    # #16: behavior hooks — "module:function" specs resolved in the hooks dir
    # (see hooks.py; NEVER evaluated as code). The TOML key is `validate`; the
    # field is aliased because `validate` shadows a pydantic BaseModel attr.
    # validate(args: dict) rejects a call by raising ValueError(msg);
    # post_process(result) reshapes the backend's answer before the caller
    # sees it. Hand-authored in config.toml, read-only in the admin UI.
    validate_: str | None = Field(default=None, alias="validate")
    post_process: str | None = None
    params: list[ParamOverride] = Field(default_factory=list)

    @field_validator("max_result_chars", mode="before")
    @classmethod
    def _check_max_result_chars(cls, v):
        # Positive integer or nothing — a zero/negative cap would blank every
        # result. mode="before" so pydantic's lax coercion can't launder a
        # bool/float/string into an int first (bool is an int subclass).
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise ConfigError(
                f"max_result_chars must be a positive integer (got {v!r})"
            )
        return v

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

    # Stable, non-display identity used by Virtual Tool bindings. Legacy configs
    # may omit it; the first Virtual Tool write assigns and persists IDs before
    # storing any reference. Backend rename therefore changes only ``name``.
    id: str | None = None
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


_VIRTUAL_NAME_RE = r"[A-Za-z0-9_-]{1,64}"
MAX_VIRTUAL_ROUTE_PATTERNS = 32
MAX_VIRTUAL_ROUTE_PATTERN_CHARS = 256
DEFAULT_VIRTUAL_ROUTING_INPUT_CHARS = 4_096
MAX_VIRTUAL_ROUTING_INPUT_CHARS = 32_768


def _route_quantifier_end(pattern: str, position: int) -> int | None:
    """Return the position after a regex quantifier beginning at *position*."""

    if pattern[position] in "*+?":
        return position + 1
    if pattern[position] != "{":
        return None
    end = pattern.find("}", position + 1)
    if end == -1:
        return None
    if re.fullmatch(r"\d+(?:,\d*)?", pattern[position + 1 : end]):
        return end + 1
    return None


def _validate_route_pattern(  # noqa: PLR0912, PLR0915 - small regex parser state machine
    pattern: str, tool_original: str
) -> None:
    """Accept a bounded, non-recursive subset of Python's regex syntax.

    Keyword routes run for every virtual invocation, so valid Python regexes are
    not automatically safe enough.  In particular, lookarounds, backreferences,
    and a quantified group containing another quantifier make catastrophic
    backtracking practical on caller-controlled input.
    """

    if not pattern.strip():
        raise ConfigError(
            f"virtual member {tool_original!r}: route pattern cannot be empty"
        )
    if len(pattern) > MAX_VIRTUAL_ROUTE_PATTERN_CHARS:
        raise ConfigError(
            f"virtual member {tool_original!r}: route pattern exceeds "
            f"{MAX_VIRTUAL_ROUTE_PATTERN_CHARS} characters"
        )

    groups: list[bool] = []
    in_class = False
    position = 0
    while position < len(pattern):
        char = pattern[position]
        if char == "\\":
            if position + 1 < len(pattern):
                escaped = pattern[position + 1]
                if escaped.isdigit() or (
                    escaped in {"g", "k"}
                    and position + 2 < len(pattern)
                    and pattern[position + 2] == "<"
                ):
                    raise ConfigError(
                        f"virtual member {tool_original!r}: route patterns "
                        "cannot use backreferences"
                    )
            position += 2
            continue
        if in_class:
            if char == "]":
                in_class = False
            position += 1
            continue
        if char == "[":
            in_class = True
            position += 1
            continue
        if char == "(":
            prefix = pattern[position : position + 4]
            if (
                prefix.startswith("(?=")
                or prefix.startswith("(?!")
                or prefix.startswith("(?<=")
                or prefix.startswith("(?<!")
                or prefix.startswith("(?P=")
                or prefix.startswith("(?(")
            ):
                raise ConfigError(
                    f"virtual member {tool_original!r}: route patterns cannot "
                    "use lookarounds, conditionals, or backreferences"
                )
            groups.append(False)
            position += 1
            continue
        if char == ")":
            # Python's own compiler reports unmatched parentheses below.
            contains_quantifier = groups.pop() if groups else False
            quantifier_end = (
                _route_quantifier_end(pattern, position + 1)
                if (position + 1 < len(pattern))
                else None
            )
            if quantifier_end is not None and contains_quantifier:
                raise ConfigError(
                    f"virtual member {tool_original!r}: route patterns cannot "
                    "use nested quantifiers"
                )
            if groups:
                groups[-1] = (
                    groups[-1] or contains_quantifier or (quantifier_end is not None)
                )
            position += 1
            continue
        quantifier_end = _route_quantifier_end(pattern, position)
        if quantifier_end is not None:
            # The '?' in a non-capturing or named-group prefix is not a
            # quantifier.  It cannot create a backtracking repetition.
            if not (char == "?" and position > 0 and pattern[position - 1] == "("):
                if groups:
                    groups[-1] = True
            position = quantifier_end
            continue
        position += 1

    try:
        re.compile(pattern)
    except re.error as exc:
        raise ConfigError(
            f"virtual member {tool_original!r}: invalid route pattern "
            f"{pattern!r}: {exc}"
        ) from None


def routing_input_text(arguments: dict[str, object], max_chars: int) -> str:
    """Return bounded routing text for keyword/LLM selection.

    Callers must apply this before regex matching or sending arguments to an
    external router.  The limit is character-based because route regexes work
    on Python strings, and it excludes schema defaults that are not part of a
    call.
    """

    text = " ".join(str(value) for value in arguments.values())
    if len(text) > max_chars:
        raise ValueError(
            f"virtual routing input exceeds configured {max_chars}-character limit"
        )
    return text


class VirtualInput(BaseModel, extra="forbid"):
    """One public JSON-Schema input of a gateway-owned Virtual Tool."""

    name: str
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str | None = None
    required: bool = True
    default: str | int | float | bool | None = None

    @model_validator(mode="after")
    def _check(self) -> VirtualInput:
        if not re.fullmatch(_VIRTUAL_NAME_RE, self.name):
            raise ConfigError(
                f"invalid virtual input name {self.name!r}: use only letters, "
                f"digits, '_' or '-' (max 64 chars)"
            )
        if self.required and self.default is not None:
            raise ConfigError(
                f"virtual input {self.name!r}: a required input cannot have a default"
            )
        if self.default is not None:
            if isinstance(self.default, float) and not math.isfinite(self.default):
                raise ConfigError(
                    f"virtual input {self.name!r}: default must be finite"
                )
            if isinstance(self.default, bool):
                valid = self.type == "boolean"
            elif self.type == "integer":
                valid = isinstance(self.default, int)
            elif self.type == "number":
                valid = isinstance(self.default, int | float)
            elif self.type == "string":
                valid = isinstance(self.default, str)
            else:
                valid = False
            if not valid:
                raise ConfigError(
                    f"virtual input {self.name!r}: default {self.default!r} does "
                    f"not match type {self.type!r}"
                )
        return self


class VirtualMember(BaseModel, extra="forbid"):
    """Stable binding from a Virtual Tool to one original backend tool."""

    backend_id: str
    tool_original: str
    label: str | None = None
    # Original member parameter -> Virtual Tool input name.
    args: dict[str, str] = Field(default_factory=dict)
    # Original member parameter -> injected scalar.
    static_args: dict[str, str | int | float | bool] = Field(default_factory=dict)
    timeout: float = Field(default=30.0, gt=0, le=300)
    route_patterns: list[str] = Field(default_factory=list)
    route_description: str | None = None

    @field_validator("static_args")
    @classmethod
    def _static_args_must_be_finite(cls, values):
        invalid = sorted(
            key
            for key, value in values.items()
            if isinstance(value, float) and not math.isfinite(value)
        )
        if invalid:
            raise ConfigError(
                f"virtual member static_args must be finite for {invalid}"
            )
        return values

    @model_validator(mode="after")
    def _check(self) -> VirtualMember:
        if not self.backend_id.strip() or not self.tool_original.strip():
            raise ConfigError(
                "virtual member backend_id and tool_original are required"
            )
        overlap = set(self.args) & set(self.static_args)
        if overlap:
            raise ConfigError(
                f"virtual member {self.tool_original!r}: parameter(s) "
                f"{sorted(overlap)} appear in both args and static_args"
            )
        if len(self.route_patterns) > MAX_VIRTUAL_ROUTE_PATTERNS:
            raise ConfigError(
                f"virtual member {self.tool_original!r}: no more than "
                f"{MAX_VIRTUAL_ROUTE_PATTERNS} route patterns are allowed"
            )
        for pattern in self.route_patterns:
            _validate_route_pattern(pattern, self.tool_original)
        return self


class VirtualRouter(BaseModel, extra="forbid"):
    """Keyword/LLM member selection and explicit local fallback."""

    model: str = "openai/gpt-4o-mini"
    api_key: str | None = None
    conditions: str | None = None
    timeout: float = Field(default=3.0, gt=0, le=30)
    fallback: str = "all"
    egress_acknowledged: bool = False
    # SHA-256 consent receipt for the exact external-routing payload contract.
    # It is intentionally stored alongside the acknowledgement, never derived
    # during serialization: an operator must explicitly reconfirm a changed
    # model, API reference, prompt context, or public input contract.
    egress_consent_fingerprint: str | None = None

    @field_validator("api_key")
    @classmethod
    def _key_must_be_env_ref(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(
            r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", value
        ):
            raise ConfigError("virtual router api_key must be a ${ENV_VAR} reference")
        return value

    @field_validator("egress_consent_fingerprint")
    @classmethod
    def _fingerprint_must_be_sha256(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
            raise ConfigError(
                "virtual router egress_consent_fingerprint must be a sha256 digest"
            )
        return value


class VirtualTool(BaseModel, extra="forbid"):
    """A gateway-owned tool served from the shared ``/virtual/mcp`` endpoint."""

    name: str
    description: str
    enabled: bool = False
    always_load: bool = False
    dispatch: Literal["all", "keyword", "llm"] = "all"
    inputs: list[VirtualInput] = Field(default_factory=list)
    members: list[VirtualMember]
    router: VirtualRouter | None = None
    routing_input_max_chars: int = Field(
        default=DEFAULT_VIRTUAL_ROUTING_INPUT_CHARS,
        ge=64,
        le=MAX_VIRTUAL_ROUTING_INPUT_CHARS,
    )
    max_result_bytes: int = Field(default=262_144, ge=1_024, le=16_777_216)
    failure_policy: Literal["partial", "strict"] = "partial"

    @model_validator(mode="after")
    def _check(self) -> VirtualTool:  # noqa: PLR0912 - cohesive model invariants
        if not re.fullmatch(_VIRTUAL_NAME_RE, self.name):
            raise ConfigError(
                f"invalid virtual tool name {self.name!r}: use only letters, "
                f"digits, '_' or '-' (max 64 chars)"
            )
        if not self.description.strip():
            raise ConfigError(f"virtual tool {self.name!r}: description is required")
        if not self.members:
            raise ConfigError(
                f"virtual tool {self.name!r}: at least one member is required"
            )
        names = [item.name for item in self.inputs]
        dupes = {name for name in names if names.count(name) > 1}
        if dupes:
            raise ConfigError(
                f"virtual tool {self.name!r}: duplicate input name(s): {sorted(dupes)}"
            )
        declared = set(names)
        for member in self.members:
            unknown = set(member.args.values()) - declared
            if unknown:
                raise ConfigError(
                    f"virtual tool {self.name!r}: member maps undeclared input(s): "
                    f"{sorted(unknown)}"
                )
        if self.dispatch == "keyword" and not any(
            member.route_patterns for member in self.members
        ):
            raise ConfigError(
                f"virtual tool {self.name!r}: keyword dispatch needs route_patterns"
            )
        if self.dispatch == "llm":
            if self.router is None or not self.router.api_key:
                raise ConfigError(
                    f"virtual tool {self.name!r}: llm dispatch needs router.api_key"
                )
            if not self.router.egress_acknowledged:
                raise ConfigError(
                    f"virtual tool {self.name!r}: llm dispatch requires explicit "
                    f"egress acknowledgement"
                )
            if self.enabled:
                expected_fingerprint = llm_egress_consent_fingerprint(self)
                if self.router.egress_consent_fingerprint != expected_fingerprint:
                    raise ConfigError(
                        f"virtual tool {self.name!r}: active llm dispatch requires "
                        "an egress consent fingerprint matching its routing "
                        "configuration"
                    )
        if self.router is not None and self.router.fallback != "all":
            labels = {
                member.label or f"{member.backend_id}/{member.tool_original}"
                for member in self.members
            }
            if self.router.fallback not in labels:
                raise ConfigError(
                    f"virtual tool {self.name!r}: router fallback "
                    f"{self.router.fallback!r} matches no member label"
                )
        return self


def llm_egress_consent_fingerprint(tool: VirtualTool) -> str:
    """Return the consent fingerprint for an LLM's externally-visible contract.

    This deliberately mirrors every configuration field that changes the
    router destination, selection prompt, or public call surface.  It is a
    fingerprint of configuration only: caller-provided argument *values* stay
    out of durable configuration and are instead bounded at invocation time by
    :func:`routing_input_text`.
    """

    router = tool.router
    if router is None:
        raise ConfigError("llm egress fingerprint needs a virtual router")
    payload = {
        "version": 1,
        "tool": {
            "name": tool.name,
            "description": tool.description,
            "inputs": [
                {
                    "name": item.name,
                    "type": item.type,
                    "description": item.description,
                    "required": item.required,
                    "default": item.default,
                }
                for item in tool.inputs
            ],
            "routing_input_max_chars": tool.routing_input_max_chars,
        },
        "routing": {
            "model": router.model,
            "api_key": router.api_key,
            "conditions": router.conditions,
            "timeout": router.timeout,
            "fallback": router.fallback,
        },
        "members": [
            {
                "backend_id": member.backend_id,
                "tool_original": member.tool_original,
                "label": member.label,
                "route_patterns": member.route_patterns,
                "route_description": member.route_description,
            }
            for member in tool.members
        ],
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode()
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


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


def _is_loopback_url(url: AnyHttpUrl) -> bool:
    """Return whether an HTTP URL is explicitly local.

    OAuth discovery may use plain HTTP for a loopback development issuer, but
    production authorization-server and JWKS URLs must be HTTPS.  We never
    resolve DNS here: hostnames that are not obvious loopback literals are
    treated as remote and therefore require HTTPS.
    """
    host = url.host.strip("[]")
    return _is_loopback_host(host)


class OAuthConfig(BaseModel, extra="forbid"):
    """JWT resource-server settings for the independent MCP endpoints.

    The gateway is deliberately only a resource server.  Login, consent,
    client registration, and token issuance remain the responsibility of the
    configured authorization server; the gateway validates its access tokens
    and publishes RFC 9728 protected-resource metadata.
    """

    public_base_url: AnyHttpUrl
    authorization_servers: list[AnyHttpUrl] = Field(min_length=1)
    issuer: str = Field(min_length=1)
    jwks_uri: AnyHttpUrl
    algorithm: Literal[
        "RS256",
        "RS384",
        "RS512",
        "ES256",
        "ES384",
        "ES512",
        "PS256",
        "PS384",
        "PS512",
    ] = "RS256"
    required_scopes: list[str] = Field(
        default_factory=lambda: ["mcp:access"], min_length=1
    )
    # OAuth protects MCP resources; the Admin API has a separate static token
    # so an mcp:access token can never mutate gateway configuration.
    admin_bearer_token: str | None = None

    @model_validator(mode="after")
    def _check_oauth_urls(self) -> OAuthConfig:
        base = self.public_base_url
        if base.path not in ("", "/") or base.query or base.fragment:
            raise ConfigError(
                "oauth.public_base_url must be an origin without a path, query, "
                "or fragment"
            )
        try:
            issuer_url = TypeAdapter(AnyHttpUrl).validate_python(self.issuer)
        except ValueError as exc:
            raise ConfigError("oauth.issuer must be a valid HTTP URL") from exc
        urls = [*self.authorization_servers, self.jwks_uri, issuer_url]
        for url in urls:
            if url.scheme != "https" and not _is_loopback_url(url):
                raise ConfigError(
                    f"OAuth URL {url} must use https outside loopback development"
                )
        if base.scheme != "https" and not _is_loopback_url(base):
            raise ConfigError(
                "oauth.public_base_url must use https outside loopback development"
            )
        if issuer_url.scheme != "https" and not _is_loopback_url(issuer_url):
            raise ConfigError(
                "oauth.issuer must use https outside loopback development"
            )
        if len(set(self.required_scopes)) != len(self.required_scopes):
            raise ConfigError("oauth.required_scopes must not contain duplicates")
        if any(
            not scope or any(char.isspace() for char in scope)
            for scope in self.required_scopes
        ):
            raise ConfigError(
                "oauth.required_scopes entries must be non-empty and contain no "
                "whitespace"
            )
        if self.admin_bearer_token == "":
            raise ConfigError("oauth.admin_bearer_token must not be empty")
        if self.admin_bearer_token is not None and not _ENV_PATTERN.fullmatch(
            self.admin_bearer_token.strip()
        ):
            raise ConfigError(
                "oauth.admin_bearer_token must be a single ${ENV_VAR} reference"
            )
        return self


class GatewayConfig(BaseModel, extra="forbid"):
    """Top-level gateway configuration."""

    host: str = "127.0.0.1"
    port: int = 9100
    log_file: str = "~/.local/state/mcp-gateway/gateway.log"
    # Structured event verbosity. INFO preserves the gateway's lifecycle and
    # tool-call events; DEBUG also enables routine framework/library chatter.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = (
        logging_setup.DEFAULT_LOG_LEVEL
    )
    # Disk retention cap: one active file plus this many rotated backups.
    log_max_bytes: int = Field(
        default=logging_setup.DEFAULT_LOG_MAX_BYTES, ge=64 * 1024, le=1024 * 1024 * 1024
    )
    log_backup_count: int = Field(
        default=logging_setup.DEFAULT_LOG_BACKUP_COUNT, ge=1, le=100
    )
    # #43: scheduled re-introspection interval in seconds. OFF by default (0) —
    # the event-driven triggers (post-mount refresh, tools/list_changed, admin
    # page load) cover everything but a long-lived remote backend that hot-swaps
    # tools silently; set an interval only for that rare case.
    introspect_interval: int = Field(default=0, ge=0)
    # #157: age-gate the POST-MOUNT baseline refresh (#43 trigger 1). A boot
    # (or remount) skips re-capturing a backend whose stored baseline is
    # younger than this many seconds — sparing slow stdio backends a second
    # cold start per boot, at the cost of up to this much
    # staleness after an upgrade. 0 disables the gate (refresh on every
    # mount, the pre-#157 behavior). Event-driven triggers — tools/
    # list_changed, admin page load, manual Re-inspect — are NEVER gated.
    baseline_max_age: int = Field(default=DEFAULT_BASELINE_MAX_AGE, ge=0)
    # Optional bearer token required on every backend MCP endpoint (#26) —
    # defense-in-depth on the loopback bind. Store a ${ENV} ref, never the raw
    # value; the server resolves it ONCE at startup via expand_env (a missing
    # var fails loudly), not per request. /admin, /health and /ready stay open
    # (see server.BearerAuthMiddleware).
    bearer_token: str | None = None
    # Optional OAuth 2.1 resource-server profile. Each independent backend
    # endpoint and /virtual/mcp gets its own audience and RFC 9728 metadata.
    # The legacy bearer_token and OAuth profile are mutually exclusive.
    oauth: OAuthConfig | None = None
    backends: list[Backend] = Field(default_factory=list)
    virtual_tools: list[VirtualTool] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_backends(self) -> GatewayConfig:
        # An empty gateway is valid: the permanent Virtual Tools endpoint still
        # mounts with an empty catalog, and the Admin UI can import the first
        # backend later. Virtual member validation below still rejects dangling
        # backend IDs.
        names = [b.name for b in self.backends]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ConfigError(f"duplicate backend name(s): {sorted(dupes)}")
        reserved = sorted(RESERVED_BACKEND_NAMES.intersection(names))
        if reserved:
            raise ConfigError(
                f"backend name(s) {reserved} are reserved (built-in routes: "
                "/virtual, /admin, /health, /ready)"
            )
        backend_ids = [b.id for b in self.backends if b.id is not None]
        duplicate_ids = {value for value in backend_ids if backend_ids.count(value) > 1}
        if duplicate_ids:
            raise ConfigError(f"duplicate backend id(s): {sorted(duplicate_ids)}")
        if self.bearer_token == "":
            raise ConfigError("bearer_token must not be empty")
        if self.bearer_token is not None and self.oauth is not None:
            raise ConfigError(
                "bearer_token and oauth are mutually exclusive; choose one "
                "gateway authentication profile"
            )
        virtual_names = [tool.name for tool in self.virtual_tools]
        duplicate_virtual = {
            name for name in virtual_names if virtual_names.count(name) > 1
        }
        if duplicate_virtual:
            raise ConfigError(
                f"duplicate virtual tool name(s): {sorted(duplicate_virtual)}"
            )
        known_ids = set(backend_ids)
        for tool in self.virtual_tools:
            for member in tool.members:
                if member.backend_id not in known_ids:
                    raise ConfigError(
                        f"virtual tool {tool.name!r}: member references unknown "
                        f"backend id {member.backend_id!r}"
                    )
        # #18: a non-loopback bind exposes config writes and tool execution to
        # the network, so it is refused without static bearer or OAuth auth.
        if (
            self.bearer_token is None
            and self.oauth is None
            and not _is_loopback_host(self.host)
        ):
            raise ConfigError(
                f"host {self.host!r} is not loopback: binding beyond this "
                f"machine requires bearer_token or oauth (see docs/security.md)"
            )
        if (
            self.oauth is not None
            and not _is_loopback_host(self.host)
            and not self.oauth.admin_bearer_token
        ):
            raise ConfigError(
                "a non-loopback OAuth deployment requires "
                "oauth.admin_bearer_token to protect the Admin API"
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

# Tool `_meta` hint Claude Code honors as a per-tool output budget (chars of
# text content), overriding its global 25k-token MAX_MCP_OUTPUT_TOKENS cap
# (#162). See .agents/skills/mcp-tool-design/references/clients/claude-code.md.
MAX_RESULT_CHARS_META_KEY = "anthropic/maxResultSizeChars"


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
        # per-backend pin below. The output cap (#162) rides the same merged
        # meta: FastMCP's ToolTransformConfig.meta REPLACES the tool's meta, so
        # both flags must land in ONE dict on top of the captured original.
        meta_flags: dict = {}
        if b.enabled and (tool.always_load or b.always_load):
            meta_flags.update(ALWAYS_LOAD_META)
        if b.enabled and tool.max_result_chars is not None:
            meta_flags[MAX_RESULT_CHARS_META_KEY] = tool.max_result_chars
        if meta_flags:
            tc_kwargs["meta"] = {**(bmeta.get(key) or {}), **meta_flags}
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
        broadcast: dict[str, str] = {}
        for p in prompts:
            original = p.name
            if original is None:
                raise ValueError("backend returned a prompt without a name")
            ov = self._prompts.get(original)
            transformed = p if ov is None else self._apply_prompt(p, ov)
            if ov is not None and not ov.enabled:
                continue
            target = transformed.name
            if target is None:
                raise ValueError("prompt transform produced a prompt without a name")
            previous = broadcast.get(target)
            if previous is not None and previous != original:
                raise ValueError(
                    f"prompt broadcast name {target!r} is produced by "
                    f"both {previous!r} and {original!r}"
                )
            broadcast[target] = original
            out.append(transformed)
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
        if b.id is not None:
            d["id"] = b.id
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
        if t.max_result_chars is not None:  # #162: per-tool output cap
            d["max_result_chars"] = t.max_result_chars
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

    def _virtual(tool: VirtualTool) -> dict:  # noqa: PLR0912
        item: dict = {
            "name": tool.name,
            "description": tool.description,
            "enabled": tool.enabled,
            "dispatch": tool.dispatch,
            "routing_input_max_chars": tool.routing_input_max_chars,
            "max_result_bytes": tool.max_result_bytes,
            "failure_policy": tool.failure_policy,
        }
        if tool.always_load:
            item["always_load"] = True
        if tool.inputs:
            item["inputs"] = [
                {
                    key: value
                    for key, value in {
                        "name": inp.name,
                        "type": inp.type,
                        "description": inp.description,
                        "required": inp.required,
                        "default": inp.default,
                    }.items()
                    if value is not None
                }
                for inp in tool.inputs
            ]
        item["members"] = [
            {
                key: value
                for key, value in {
                    "backend_id": member.backend_id,
                    "tool_original": member.tool_original,
                    "label": member.label,
                    "args": dict(member.args) or None,
                    "static_args": dict(member.static_args) or None,
                    "timeout": member.timeout,
                    "route_patterns": list(member.route_patterns) or None,
                    "route_description": member.route_description,
                }.items()
                if value is not None
            }
            for member in tool.members
        ]
        if tool.router is not None:
            item["router"] = {
                key: value
                for key, value in tool.router.model_dump().items()
                if value is not None
            }
        return item

    out: dict = {
        "host": cfg.host,
        "port": cfg.port,
        "log_file": cfg.log_file,
    }
    if cfg.log_level != logging_setup.DEFAULT_LOG_LEVEL:
        out["log_level"] = cfg.log_level
    if cfg.log_max_bytes != logging_setup.DEFAULT_LOG_MAX_BYTES:
        out["log_max_bytes"] = cfg.log_max_bytes
    if cfg.log_backup_count != logging_setup.DEFAULT_LOG_BACKUP_COUNT:
        out["log_backup_count"] = cfg.log_backup_count
    if cfg.introspect_interval:  # default 0 (off) — only persist when set (#43)
        out["introspect_interval"] = cfg.introspect_interval
    if cfg.baseline_max_age != DEFAULT_BASELINE_MAX_AGE:  # persist non-default (#157)
        out["baseline_max_age"] = cfg.baseline_max_age
    if cfg.bearer_token is not None:  # default None — only persist when set (#26)
        out["bearer_token"] = cfg.bearer_token
    if cfg.oauth is not None:
        oauth = cfg.oauth
        oauth_raw: dict[str, object] = {
            "public_base_url": str(oauth.public_base_url).rstrip("/"),
            "authorization_servers": [
                str(url).rstrip("/") for url in oauth.authorization_servers
            ],
            "issuer": oauth.issuer,
            "jwks_uri": str(oauth.jwks_uri),
            "algorithm": oauth.algorithm,
            "required_scopes": list(oauth.required_scopes),
        }
        if oauth.admin_bearer_token is not None:
            oauth_raw["admin_bearer_token"] = oauth.admin_bearer_token
        out["oauth"] = oauth_raw
    out["backends"] = [_backend(b) for b in cfg.backends]
    if cfg.virtual_tools:
        out["virtual_tools"] = [_virtual(tool) for tool in cfg.virtual_tools]
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
