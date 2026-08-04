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


Running `mcp-gateway` with no arguments starts the gateway in the current
terminal on every platform — there is no first-run install prompt. On macOS the
resident login service is optional and explicitly installed:

```bash
mcp-gateway service install     # install, start, and verify the resident service
mcp-gateway service status      # loaded state plus process resources
mcp-gateway service uninstall --yes   # remove the service (keeps config and state)
```

The legacy `--install-service`, `--service-status`, and `--uninstall-service`
flags remain as compatibility aliases.

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

## Control from the terminal

Every dashboard action has a scriptable command. The CLI is a client of the
same [admin API](https://github.com/voidfreud/mcp-gateway/blob/main/docs/api.md)
the UI uses, so what you can click you can automate. Commands are grouped by
domain; `--help` works at every level:

```bash
mcp-gateway --help            # every top-level command
mcp-gateway backend --help    # backend subcommands
mcp-gateway tool --help       # tool and parameter overrides
```

```bash
mcp-gateway backend list                     # backends, state, tool counts
mcp-gateway backend add exa --transport http \
  --backend-url https://your-exa-endpoint/mcp
mcp-gateway tool list --backend exa          # effective broadcast of every tool
mcp-gateway tool set exa web_search --description "…"
mcp-gateway virtual list                     # gateway-owned Virtual Tools
mcp-gateway settings show                    # bearer ref, update check, log settings
mcp-gateway logs follow                      # live structured log
```

- `--json` prints exactly one JSON value for result-producing commands, for
  pipelines; the default human output is concise. Exceptions: streaming
  `logs follow --json` emits one JSON event per line, and `run`/`--help`
  produce no JSON.
- The admin base URL defaults to the resolved config's `host`/`port` (without
  creating the file), else `http://127.0.0.1:9100`; override with `--url`.
- If the gateway uses a bearer token, the CLI resolves it from the environment
  for commands that talk to the admin API — `--token-env NAME`, else
  `MCP_GATEWAY_ADMIN_TOKEN`, else the configured `${ENV}` reference. An
  explicit `--url` disables those implicit sources, so authenticate an
  explicit endpoint with `--token-env NAME` (over HTTPS). Tokens are never
  accepted as command-line arguments and never printed; see
  [security.md](https://github.com/voidfreud/mcp-gateway/blob/main/docs/security.md#the-cli-and-the-bearer-token).
- Destructive commands require `--yes`; scripts are never prompted.
- Complex inputs (a full Virtual Tool definition, a tool-run argument object)
  are read from a JSON file or `-` (stdin).

The full reference lives in
[operations.md](https://github.com/voidfreud/mcp-gateway/blob/main/docs/operations.md#command-line-reference).

## Running and updating

`/health` answers whether the gateway process is alive and identifies the code
path it is running. `/ready` answers whether the gateway and every enabled
backend are mounted; it returns `503` while any enabled backend is unavailable.

```bash
curl -s http://127.0.0.1:9100/health
curl -s http://127.0.0.1:9100/ready
```

The same probes are exposed as commands: `mcp-gateway check` exits nonzero when
the gateway is alive but degraded, `mcp-gateway status` reports per-backend
liveness, and `mcp-gateway restart` restarts the daemon (an honest no-op in
foreground/development mode).

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
swap. `just` is a repository/contributor tool, not the user control interface:
contributors deploying a checkout can continue to use the guarded
`just update` recipe from a clean `main` branch.

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
- [Operations](https://github.com/voidfreud/mcp-gateway/blob/main/docs/operations.md) — the `mcp-gateway` command-line reference, service lifecycle, logs, and recovery.
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
