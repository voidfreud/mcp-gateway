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
repair, and inline tool editing (follows your system's light/dark theme)](docs/img/admin-backend-dark.png)

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

Prerequisites:

- [`uv`](https://docs.astral.sh/uv/) to install and run the foreground package.
- [`just`](https://just.systems/) only for the repository's check and macOS
  deployment recipes, including `just update`.
- The [GitHub CLI](https://cli.github.com/) authenticated as an account that can
  read this private repository, to download a release asset.

For a portable, stable foreground run on any platform, download a tagged GitHub
Release and verify it before installing its wheel:

```bash
gh auth login
gh release download vX.Y.Z --repo voidfreud/mcp-gateway --dir mcp-gateway-vX.Y.Z
cd mcp-gateway-vX.Y.Z
shasum -a 256 -c SHA256SUMS
uv tool install --reinstall ./mcp_gateway-*.whl
mcp-gateway
```

Replace `vX.Y.Z` with the release tag you chose. The [installation guide](docs/installation.md)
has the complete private-release procedure, including verification and the
separate checkout workflow for development. The gateway selects its
configuration in this order: `MCP_GATEWAY_CONFIG`, an existing `./config.toml`,
then `~/.config/mcp-gateway/config.toml`. It seeds the selected missing path
from the packaged default, so a fresh installation normally creates the
home-path file. The bundled DeepWiki and Context7 examples are stateless proxies
once running, but a freshly seeded default configuration with no
captured-default state is not network-silent: before the app mounts its
endpoints, startup connects to both public services to capture each backend's
baseline metadata and tool list. Complete captured defaults are normally reused
on later starts. That initial capture is separate from ordinary proxy use; tool
calls can also make backend requests. Replace or remove those entries before
starting the gateway if those outbound connections are not appropriate for your
environment. Stop the foreground process with Ctrl-C.

For a macOS login service, clone the repository and install it:

```bash
gh auth login
gh auth setup-git
git clone https://github.com/voidfreud/mcp-gateway
cd mcp-gateway
./install.sh
```

The compatibility script installs a stable `uv` tool, then the application
atomically installs and verifies its own LaunchAgent. It does not leave the
service tied to the checkout path. Preview with `./install.sh --dry-run`; use
`mcp-gateway --foreground` to run without the service and
`mcp-gateway --service-status` to inspect resident gateway/backend resources.
For exact install, migration, update, and removal behavior, read
[the installation guide](docs/installation.md).

Open <http://127.0.0.1:9100/admin> to import or edit backends. If the relevant
client CLI is installed, the admin UI provides verified registration controls for
both Claude Code and Codex; otherwise register the backend endpoint manually in
your MCP client. See [the admin guide](docs/admin-guide.md).

## Running and updating

`/health` answers whether the gateway process is alive and identifies the code
path it is running. `/ready` answers whether the gateway and every enabled
backend are mounted; it returns `503` while any enabled backend is unavailable.

```bash
curl -s http://127.0.0.1:9100/health
curl -s http://127.0.0.1:9100/ready
```

For a macOS checkout installation only, `just update` is a guarded, stateful,
readiness-dependent deployment command. Run it from a clean `main` checkout
after changes have merged: it fast-forwards `origin/main`, synchronizes the
locked environment, reinstalls/reloads the LaunchAgent, and waits for both
endpoints. It preserves the configuration and runtime state, but briefly
interrupts MCP sessions.

```bash
just update
```

It is not a general upgrade command for a release-asset installation; install a
new verified wheel from the next GitHub Release instead.

## What you can change

- Tool, parameter, resource, prompt, and server-instruction text.
- Visibility, injected defaults, output budgets, and per-tool behavior hooks.
- Backend configuration and independent client registrations.
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

- [Installation](docs/installation.md) — foreground and macOS service paths,
  upgrades, moves, and uninstalling.
- [Releases](docs/releases.md) — versioning, automation, and private release
  installation.
- [Admin guide](docs/admin-guide.md) — editing, registration, and Virtual Tools.
- [Configuration reference](docs/configuration.md) — `config.toml`, backends,
  secrets, and behavior hooks.
- [Operations](docs/operations.md) — readiness, logs, recovery, and local
  verification boundaries.
- [Security](docs/security.md) — network exposure, bearer tokens, OAuth, and
  local trust boundaries.
- [Admin API](docs/api.md) — scripting interface and API contracts.

## Contributing

Work through pull requests, with CI as the shared baseline. Start with
[CONTRIBUTING.md](CONTRIBUTING.md) and the repository's
[agent instructions](AGENTS.md); they define the development workflow,
validation expectations, and where to record deferred work.

## License

MIT. See [LICENSE](LICENSE).
