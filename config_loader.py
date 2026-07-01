"""Config loading for mcp-gateway.

Reads ``config.toml`` into validated Pydantic models, then maps those models
into the two things ``server.py`` needs:

1. A FastMCP proxy config dict (``{"mcpServers": {...}}``) for ``create_proxy``.
2. A ``ToolTransform`` that rewrites every broadcast text (tool name/title/
   description + each parameter name/description, hide params, disable tools).

Key behaviour learned from the installed FastMCP (3.4.2): a proxy over a
*single* backend exposes tools under their bare name (``ask_question``), but a
proxy over *two or more* backends prefixes them with the server name
(``deepwiki_ask_question``). Tool transforms are keyed by that exposed name, so
:func:`exposed_name` computes the right key from the backend count.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path
from typing import Literal

import tomli_w
from pydantic import BaseModel, Field, model_validator

from fastmcp.server.transforms import ToolTransform
from fastmcp.tools.tool_transform import ArgTransformConfig, ToolTransformConfig

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

DEFAULT_SECRETS_PATH = "~/.config/mcp-gateway/secrets.env"


class ConfigError(RuntimeError):
    """Raised for any malformed or unresolvable configuration."""


def secrets_path() -> Path:
    """The gateway-scoped secrets file (``MCP_GATEWAY_SECRETS`` overrides)."""
    return Path(
        os.environ.get("MCP_GATEWAY_SECRETS", DEFAULT_SECRETS_PATH)
    ).expanduser()


def load_secrets() -> dict[str, str]:
    """Parse the gateway-scoped secrets file into a dict.

    KEY=VALUE lines; blank lines, ``#`` comments, and an ``export `` prefix are
    tolerated; surrounding single/double quotes on the value are stripped.
    Values stay OUT of ``os.environ`` on purpose: stdio backend subprocesses
    inherit the daemon's environment, and one backend must not be able to read
    secrets meant for another. Re-read on every call (the file is tiny) so a
    newly added secret works without a daemon restart.
    """
    path = secrets_path()
    secrets: dict[str, str] = {}
    if not path.is_file():
        return secrets
    for line in path.read_text().splitlines():
        line = line.strip()
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


class ToolOverride(BaseModel, extra="forbid"):
    """Rewrite (or disable) one backend tool and its parameters."""

    original: str
    name: str | None = None
    title: str | None = None
    description: str | None = None
    enabled: bool = True
    # Pin this tool to load UPFRONT (exempt from Claude Code tool-search deferral)
    # via the tool's `_meta["anthropic/alwaysLoad"]`.
    always_load: bool = False
    params: list[ParamOverride] = Field(default_factory=list)


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
    # A command that prints a JSON object of headers to stdout (#6) — for
    # short-lived tokens / SSO. Runs when the backend's client config is built
    # (mount / introspect), NOT per request. Same trust level as a stdio
    # backend's `command`: the config file is local-admin-owned.
    headers_helper: str | None = None
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

    @model_validator(mode="after")
    def _check_transport(self) -> "Backend":
        if self.transport != "stdio":  # http / streamable-http / sse — url-based
            if not self.url:
                raise ConfigError(
                    f"backend {self.name!r}: {self.transport} transport needs a url"
                )
        else:  # stdio
            if not self.command:
                raise ConfigError(
                    f"backend {self.name!r}: stdio transport needs a command"
                )
        if (self.auth_header is None) != (self.auth_value is None):
            raise ConfigError(
                f"backend {self.name!r}: set both auth_header and auth_value, or neither"
            )
        return self


class GatewayConfig(BaseModel, extra="forbid"):
    """Top-level gateway configuration."""

    host: str = "127.0.0.1"
    port: int = 9100
    log_file: str = "~/.local/state/mcp-gateway/gateway.log"
    backends: list[Backend] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_backends(self) -> "GatewayConfig":
        if not self.backends:
            raise ConfigError("config has no [[backends]]")
        names = [b.name for b in self.backends]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ConfigError(f"duplicate backend name(s): {sorted(dupes)}")
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


def exposed_name(cfg: GatewayConfig, backend: Backend, original: str) -> str:
    """The name a backend tool is exposed under on its own endpoint.

    Each backend is proxied alone on its own path (``/<backend>/mcp``) and
    registered as its own MCP server in Claude Code, so tools keep their BARE
    original name. The old ``<backend>_`` prefix only existed to disambiguate
    tools inside one shared multi-backend proxy; with per-backend endpoints the
    endpoint/server registration provides the namespace, so the prefix is gone.

    (``cfg``/``backend`` are kept in the signature so the admin's call sites stay
    stable while the prefix rule is centralised here.)
    """
    return original


def _run_headers_helper(b: Backend) -> dict[str, str]:
    """Run ``headers_helper`` and parse its stdout as a JSON object of headers.

    Raises ConfigError on a non-zero exit, timeout, or non-object output — a
    misconfigured helper must fail loudly, not silently connect unauthenticated.
    """
    import subprocess

    try:
        out = subprocess.run(
            b.headers_helper,
            shell=True,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
    except subprocess.SubprocessError as exc:
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


def build_transforms(
    cfg: GatewayConfig,
    backend: Backend,
    all_tools: dict[str, list[str]] | None = None,
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
    """
    transforms: dict[str, ToolTransformConfig] = {}
    index: dict[str, str] = {}
    b = backend
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
        if tool.always_load or b.always_load:
            tc_kwargs["meta"] = dict(ALWAYS_LOAD_META)
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
                    enabled=True, meta=dict(ALWAYS_LOAD_META)
                )
                index[original] = b.name
    return ToolTransform(transforms), index


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


def to_raw(cfg: GatewayConfig) -> dict:
    """Convert a GatewayConfig back to the plain dict shape of config.toml,
    omitting None/empty fields so the written file stays minimal and clean.
    """

    def _backend(b: Backend) -> dict:
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
        params = [_param(p) for p in t.params]
        if params:
            d["params"] = params
        return d

    def _param(p: ParamOverride) -> dict:
        d: dict = {"original": p.original}
        if p.name is not None:
            d["name"] = p.name
        if p.description is not None:
            d["description"] = p.description
        d["hide"] = p.hide
        return d

    out: dict = {
        "host": cfg.host,
        "port": cfg.port,
        "log_file": cfg.log_file,
    }
    out["backends"] = [_backend(b) for b in cfg.backends]
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
    # Quick self-check: `uv run config_loader.py [config.toml]`
    path = sys.argv[1] if len(sys.argv) > 1 else "config.toml"
    cfg = load(path)
    transforms, index = build_transforms(cfg)
    print(f"loaded {len(cfg.backends)} backend(s); {len(index)} tool override(s)")
    for key, backend in index.items():
        print(f"  transform key: {key:40s} <- backend {backend}")
    print("proxy config:")
    import json

    print(json.dumps(to_proxy_config(cfg), indent=2))
