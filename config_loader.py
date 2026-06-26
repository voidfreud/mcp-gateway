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


class ConfigError(RuntimeError):
    """Raised for any malformed or unresolvable configuration."""


def expand_env(value: str) -> str:
    """Replace every ``${VAR}`` in *value* with the environment variable.

    Raises :class:`ConfigError` if a referenced variable is unset, so a missing
    secret fails loudly at startup instead of sending an empty auth header.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        try:
            return os.environ[name]
        except KeyError:
            raise ConfigError(
                f"config references ${{{name}}} but environment variable "
                f"{name!r} is not set"
            ) from None

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
    transport: Literal["http", "stdio"]
    # http
    url: str | None = None
    auth_header: str | None = None
    auth_value: str | None = None
    # stdio
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    # efficiency knob (parsed; shared-session reuse is a tier-2 optimisation,
    # see README "Session strategy" — default per-request sessions for now)
    stateless: bool = False
    # Pin ALL of this backend's tools to load upfront (per-tool meta on each).
    always_load: bool = False
    tools: list[ToolOverride] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_transport(self) -> "Backend":
        if self.transport == "http":
            if not self.url:
                raise ConfigError(f"backend {self.name!r}: http transport needs a url")
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
    """The name FastMCP exposes a backend tool under.

    Single backend -> bare name; multiple backends -> ``<backend>_<original>``.
    This is the key a tool transform must use.
    """
    if len(cfg.backends) > 1:
        return f"{backend.name}_{original}"
    return original


def to_proxy_config(cfg: GatewayConfig) -> dict:
    """Build the FastMCP ``{"mcpServers": {...}}`` dict for ``create_proxy``."""
    servers: dict[str, dict] = {}
    for b in cfg.backends:
        if b.transport == "http":
            entry: dict = {"url": expand_env(b.url or ""), "transport": "http"}
            if b.auth_header and b.auth_value:
                entry["headers"] = {b.auth_header: expand_env(b.auth_value)}
        else:
            entry = {"command": b.command, "args": list(b.args), "transport": "stdio"}
            if b.env:
                entry["env"] = {k: expand_env(v) for k, v in b.env.items()}
        servers[b.name] = entry
    return {"mcpServers": servers}


# Tool `_meta` hint that exempts a tool from Claude Code's tool-search deferral
# (loads it upfront). See references/mcp.md (anthropic/alwaysLoad).
ALWAYS_LOAD_META = {"anthropic/alwaysLoad": True}


def build_transforms(
    cfg: GatewayConfig, all_tools: dict[str, list[str]] | None = None
) -> tuple[ToolTransform, dict[str, str]]:
    """Build the ``ToolTransform`` plus a ``{exposed_name: backend}`` index.

    The index lets the server reconcile configured tools against the live tool
    list at startup and warn on any name that does not match a real backend
    tool (a typo in ``original``).

    *all_tools* maps ``backend name -> [original tool names]`` (from captured
    defaults). It is needed only to apply a **per-backend** ``always_load`` to
    tools that have no other override; per-tool ``always_load`` works without it.
    """
    transforms: dict[str, ToolTransformConfig] = {}
    index: dict[str, str] = {}
    for b in cfg.backends:
        for tool in b.tools:
            key = exposed_name(cfg, b, tool.original)
            index[key] = b.name
            arguments: dict[str, ArgTransformConfig] = {}
            for param in tool.params:
                arg_kwargs: dict = {"hide": param.hide}
                if param.name is not None:
                    arg_kwargs["name"] = param.name
                if param.description is not None:
                    arg_kwargs["description"] = param.description
                arguments[param.original] = ArgTransformConfig(**arg_kwargs)

            tc_kwargs: dict = {"enabled": tool.enabled}
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

        # Per-backend always_load: also pin tools that have no override entry.
        if b.always_load and all_tools and b.name in all_tools:
            for original in all_tools[b.name]:
                key = exposed_name(cfg, b, original)
                if key not in transforms:
                    transforms[key] = ToolTransformConfig(
                        enabled=True, meta=dict(ALWAYS_LOAD_META)
                    )
                    index[key] = b.name
    return ToolTransform(transforms), index


def to_raw(cfg: GatewayConfig) -> dict:
    """Convert a GatewayConfig back to the plain dict shape of config.toml,
    omitting None/empty fields so the written file stays minimal and clean.
    """

    def _backend(b: Backend) -> dict:
        d: dict = {"name": b.name, "transport": b.transport}
        if b.transport == "http":
            d["url"] = b.url
            if b.auth_header and b.auth_value:
                d["auth_header"] = b.auth_header
                d["auth_value"] = b.auth_value
        else:
            d["command"] = b.command
            if b.args:
                d["args"] = list(b.args)
            if b.env:
                d["env"] = dict(b.env)
        d["stateless"] = b.stateless
        if b.always_load:
            d["always_load"] = True
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

    return {
        "host": cfg.host,
        "port": cfg.port,
        "log_file": cfg.log_file,
        "backends": [_backend(b) for b in cfg.backends],
    }


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
