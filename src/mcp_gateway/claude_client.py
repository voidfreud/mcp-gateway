"""Claude Code CLI policy for registering independent gateway MCPs.

The Admin route group owns HTTP behavior and subprocess execution.  This
module owns only the Claude-specific contract: CLI discovery, registration
status parsing, and the exact ``claude mcp`` argv for one endpoint.  Keeping
that policy here lets other gateway surfaces reuse it without importing the
Admin composition root.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable

from mcp_gateway import config_loader as cl

# ``claude mcp add/remove`` scopes (see ``claude mcp add --help``).
CLAUDE_SCOPES = ("local", "user", "project")

# A local CLI bookkeeping operation should not hang the daemon indefinitely.
CLAUDE_CLI_TIMEOUT = 30

# Registration status is cached by the Admin composition root.  The policy
# module owns the TTL so any future client surface can use the same contract.
CC_REG_CACHE_TTL = 60.0


def claude_cli_path(*, which: Callable[[str], str | None] | None = None) -> str | None:
    """Locate the Claude Code executable on the daemon's current ``PATH``."""
    lookup = which or shutil.which
    return lookup("claude")


def parse_cc_registrations(output: str, backends: list[str]) -> dict[str, bool]:
    """Map configured backend names to Claude registration state.

    The colon anchors each match so ``gateway-cc:`` cannot be inferred from
    ``gateway-cc-docs:``.  Connection liveness is intentionally ignored: this
    endpoint reports registration, not whether the MCP is reachable.
    """
    return {name: f"gateway-{name}:" in output for name in backends}


def claude_mcp_command(
    action: str,
    name: str,
    url: str | None = None,
    scope: str = "local",
    bearer_token: str | None = None,
) -> list[str]:
    """Build the Claude CLI argv for one gateway MCP registration.

    ``bearer_token`` is rendered as a Claude ``Authorization`` header only for
    ``add``.  The route layer redacts it from responses; callers should never
    log the returned argv without applying the same redaction.
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
        # ``--header`` is variadic.  It must follow the positional name and
        # URL or Claude consumes them as header values (#123).
        if bearer_token:
            argv += ["--header", f"Authorization: Bearer {bearer_token}"]
        return argv
    if action == "remove":
        return ["claude", "mcp", "remove", "--scope", scope, registration]
    raise cl.ConfigError(f"unknown action {action!r} (use add or remove)")
