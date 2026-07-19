"""Codex CLI policy for registering independent gateway MCPs.

The Admin route group owns HTTP behavior and subprocess execution.  This
module owns Codex-specific discovery, auth-safe argument construction, and
JSON registration parsing so every gateway surface can reuse one policy.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from mcp_gateway import config_loader as cl

# Codex has one user-level MCP registry and exposes JSON for reliable status
# parsing.  Keep its timeout/cache policy independent from Claude Code.
CODEX_CLI_TIMEOUT = 30
CODEX_REG_CACHE_TTL = 60.0


def codex_cli_path(
    *,
    which: Callable[[str], str | None] | None = None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    """Locate Codex for both interactive shells and the macOS login daemon."""
    lookup = which or shutil.which
    found = lookup("codex")
    if found:
        return found
    environment = os.environ if environ is None else environ
    candidates = [
        environment.get("CODEX_CLI_PATH"),
        "/Applications/ChatGPT.app/Contents/Resources/codex",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def parse_codex_registrations(output: str, backends: list[str]) -> dict[str, bool]:
    """Map configured backend names to exact entries in ``codex mcp list``."""
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
    """Return the environment variable name Codex should use for gateway auth.

    Codex accepts an environment-variable name rather than a literal token.
    Requiring one ``${ENV_VAR}`` reference keeps credentials out of argv,
    responses, and the Codex config file.
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
    """Build the Codex CLI argv for one independent gateway MCP."""
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
