# mcp-gateway

[![check](https://github.com/voidfreud/mcp-gateway/actions/workflows/check.yml/badge.svg)](https://github.com/voidfreud/mcp-gateway/actions/workflows/check.yml)

**Make MCP servers easier for every MCP client to use.** mcp-gateway is a local
MCP proxy that lets you improve a backend's tool names, descriptions,
parameters, prompts, resources, and server instructions without forking that
backend. Calls still reach the original backend; the gateway improves the
surface your client receives.

It runs one local HTTP service with an admin UI at
<http://127.0.0.1:9100/admin>. Claude Code and Codex are supported equally as
independent clients: each backend keeps its own `/<backend>/mcp` endpoint.

![The admin UI's backend view — live status, grouped controls, stale-override
repair, and inline tool editing (follows your system's light/dark theme)](https://raw.githubusercontent.com/voidfreud/mcp-gateway/main/docs/img/admin-backend-dark.png)

## At a glance

```text
MCP client (Claude Code, Codex, or another MCP client)
                        │
                        ▼
          mcp-gateway — localhost:9100/<backend>/mcp
                        │
                        ▼
              remote or local MCP backend
```

The gateway is deliberately a proxy and editor, not a replacement MCP client or
an identity provider. It can rewrite what a backend advertises and optionally
validate or post-process calls; it does not silently change a backend's core
behavior.

## Start here

Install the public PyPI distribution with [`uv`](https://docs.astral.sh/uv/):

```bash
uv tool install mcp-local-gateway
mcp-gateway
```

The distribution is named `mcp-local-gateway` because the unrelated
`mcp-gateway` name was already occupied on PyPI. The command and Python package
remain `mcp-gateway` and `mcp_gateway`.
Existing `uv` tool installs of v1.1.0 or earlier need the one-time
[renamed-distribution migration](https://github.com/voidfreud/mcp-gateway/blob/main/docs/installation.md#upgrading-from-v110-or-earlier).


On macOS, the first interactive run offers to install the resident LaunchAgent;
accepting is the entire service setup. On Linux and Windows, or with
`mcp-gateway --foreground`, it runs in the current terminal. The application
owns macOS service install, upgrade, status, and removal:

```bash
mcp-gateway --service-status
mcp-gateway --uninstall-service
```

Open <http://127.0.0.1:9100/admin> to import or edit backends. Each backend is
served at its own `/<backend>/mcp` endpoint; register that endpoint using the
MCP client's supported configuration or CLI.

A fresh run normally creates `~/.config/mcp-gateway/config.toml`. The bundled
DeepWiki and Context7 examples make outbound requests while capturing their
initial catalogs and when tools are called. The gateway also makes one
lightweight PyPI version request at startup and daily; it never auto-applies an
update, tolerates offline failure, and exposes an `update_check` toggle in
Gateway settings. Remove the sample backends and disable that toggle before
starting if the environment must be network-silent.

The [installation guide](https://github.com/voidfreud/mcp-gateway/blob/main/docs/installation.md)
covers the verified GitHub Release fallback, checkout development,
configuration selection, and complete service lifecycle. See the
[Admin guide](https://github.com/voidfreud/mcp-gateway/blob/main/docs/admin-guide.md)
for registering endpoints in MCP clients.

## Running and updating

`/health` answers whether the gateway process is alive and identifies the code
path it is running. `/ready` answers whether the gateway and every enabled
backend are mounted; it returns `503` while any enabled backend is unavailable.

```bash
curl -s http://127.0.0.1:9100/health
curl -s http://127.0.0.1:9100/ready
```

For a normal installation, one command checks PyPI, installs the exact published
version, restarts the resident service when present, and requires `/health` plus
`/ready` before reporting success:

```bash
mcp-gateway update
```

Use the same path with an exact prior version for deterministic rollback:

```bash
mcp-gateway update --version X.Y.Z
```

An activation failure automatically attempts to reinstall and restart the old
version. Config, logs, backups, and captured state are never part of the package
swap. Contributors deploying a checkout can continue to use guarded
`just update` from a clean `main` branch.

## What you can change

- Tool, parameter, resource, prompt, and server-instruction text.
- Visibility, injected defaults, output budgets, and per-tool behavior hooks.
- Backend configuration and the independent per-backend `/<backend>/mcp`
  endpoints clients register.
- Gateway-owned Virtual Tools that compose or route backend tools at
  `/virtual/mcp`.

The detailed configuration and security contracts live in the linked manuals;
this README intentionally does not duplicate them.

## Validation boundaries

`just check` is the repeatable local quality gate. CI runs that gate and a
hermetic MCP conformance job using disposable fixtures; it does not contact your
personal backends or exercise your installed daemon. Those stateful, local
integration checks remain your responsibility. `just verify` is opt-in: it may
call the public DeepWiki service, sends no bearer or OAuth credentials, and is
only suitable for an equivalent unprotected test instance.

## Documentation

- [Installation](https://github.com/voidfreud/mcp-gateway/blob/main/docs/installation.md) — foreground and macOS service paths, upgrades, moves, and uninstalling.
- [Releases](https://github.com/voidfreud/mcp-gateway/blob/main/docs/releases.md) — versioning, PyPI publishing, and verified fallback artifacts.
- [Admin guide](https://github.com/voidfreud/mcp-gateway/blob/main/docs/admin-guide.md) — editing, registering endpoints in MCP clients, and Virtual Tools.
- [Configuration reference](https://github.com/voidfreud/mcp-gateway/blob/main/docs/configuration.md) — `config.toml`, backends, secrets, and behavior hooks.
- [Operations](https://github.com/voidfreud/mcp-gateway/blob/main/docs/operations.md) — readiness, logs, recovery, and local verification boundaries.
- [Security](https://github.com/voidfreud/mcp-gateway/blob/main/docs/security.md) — network exposure, bearer tokens, OAuth, and local trust boundaries.
- [Security policy](https://github.com/voidfreud/mcp-gateway/blob/main/.github/SECURITY.md) — private vulnerability reporting and supported versions.
- [Admin API](https://github.com/voidfreud/mcp-gateway/blob/main/docs/api.md) — scripting interface and API contracts.

## Contributing

Work through pull requests, with CI as the shared baseline. Start with
[CONTRIBUTING.md](https://github.com/voidfreud/mcp-gateway/blob/main/CONTRIBUTING.md)
and the repository's
[agent instructions](https://github.com/voidfreud/mcp-gateway/blob/main/AGENTS.md);
they define the development workflow, validation expectations, and where to
record deferred work.

## License

MIT. See [LICENSE](https://github.com/voidfreud/mcp-gateway/blob/main/LICENSE).
