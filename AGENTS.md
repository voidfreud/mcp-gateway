# mcp-gateway contributor entrypoint

Read [the project policy](docs/project-policy.md) before planning, changing, or
shipping work. It is the authoritative workflow and release policy. This file
is a concise session entrypoint; it does not replace the policy, an Issue, or
focused documentation.

## Product and architecture

mcp-gateway is a local MCP gateway. It exposes each configured backend at its
own MCP endpoint, rewrites the metadata that backend broadcasts, forwards tool
calls, and provides gateway-owned Virtual Tools. It has a local admin UI and
supports Claude Code, Codex, and other compatible MCP clients; do not describe
a client-specific behavior as generic MCP behavior.

Key boundaries:

- `src/mcp_gateway/config_loader.py` owns configuration models, validation,
  transforms, and persistence.
- `src/mcp_gateway/server.py` owns the application, endpoint mounting, and
  backend lifecycle.
- `src/mcp_gateway/admin.py` is the admin composition root; client registration
  routes live in `admin_routes_claude.py` and `admin_routes_codex.py`.
- `src/mcp_gateway/runtime.py` owns mounted-backend runtime state.
- `src/mcp_gateway/virtual_tools.py` owns gateway-managed composite tools.
- `tests/` is the behavioral contract; `docs/` is the maintained user and
  operator manual.

## Canonical commands

- `just` lists supported development and operational commands.
- `just check` runs the local hermetic quality gate.
- `uv run mcp-gateway` starts a development gateway.
- `just verify [url]` exercises a running gateway; use it only when its live
  scenario is applicable.
- `just install`, `just update`, and `just uninstall` manage the macOS service.
  Read [installation](docs/installation.md) and [operations](docs/operations.md)
  before using them.

## Before changing code or documentation

1. Start or link a GitHub Issue; capture deferred work, risks, and decisions
   there rather than in repository instructions.
2. Read the affected code, tests, and user documentation. Preserve public
   configuration, endpoint, command, and client-workflow compatibility unless
   the linked Issue declares a migration or breaking release.
3. Choose the smallest safe change and update the relevant tests and docs.
4. Follow the policy's validation tiers. CI does not prove a contributor's
   daemon, credentials, clients, or real backend integrations.

## Definition of done

Before requesting merge, confirm that the linked Issue's acceptance criteria,
required CI, applicable local/live receipt, documentation, security review,
and follow-ups meet [the policy's definition of done](docs/project-policy.md#definition-of-done).
Use [CONTRIBUTING.md](CONTRIBUTING.md) for the operating procedure and
[security guidance](docs/security.md) for safe reporting and handling.

## User documentation

- [Installation](docs/installation.md)
- [Admin UI guide](docs/admin-guide.md)
- [Configuration reference](docs/configuration.md)
- [Operations](docs/operations.md)
- [Security](docs/security.md)
- [Admin API](docs/api.md)
