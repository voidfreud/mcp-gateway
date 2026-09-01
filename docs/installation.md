# Installation

mcp-gateway runs in the foreground on every supported platform and can install
itself as a resident macOS login service. It is installed from this repository
with `uv`; the package is named `mcp-local-gateway` and installs the
`mcp-gateway` command and `mcp_gateway` import package. Checkout installs
remain a contributor path.

## Prerequisites

You need [`uv`](https://docs.astral.sh/uv/), Astral's Python tool manager. It
handles Python and every dependency; you do not need to install Python
separately:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

GitHub authentication is not required: the repository is public. Install
[`just`](https://just.systems/) only for contributor/repository recipes.

The MCP client CLIs you use are optional. Each `/<backend>/mcp` endpoint is
registered in the client itself using that client's supported configuration or
CLI; the gateway never registers endpoints for you.

## Recommended — from the repository

```bash
uv tool install git+https://github.com/voidfreud/mcp-gateway
mcp-gateway
```

Package installation itself never changes services or user state: the first `mcp-gateway` run starts the gateway in the foreground
and does not prompt to install anything. The macOS resident service is opt-in
and explicit:

```bash
mcp-gateway service install
```

The macOS application-owned lifecycle creates config/state directories, an
atomic versioned LaunchAgent plist, and a stable wrapper under
`~/.local/libexec/mcp-gateway/`. It captures the installing shell's `PATH`,
starts the service, and requires `/health` and `/ready`. A failed replacement
restores the prior plist/wrapper and service. Inspect gateway plus
backend-child resources on demand:

```bash
mcp-gateway service status
```

Resident mode keeps one gateway process alive. Warm `stdio` backends can remain
its children; disabled backends consume no session resources, and stateless mode
remains available when a backend need not stay warm.

A fresh run normally seeds `~/.config/mcp-gateway/config.toml`. The bundled
DeepWiki and Context7 examples contact those services for initial catalog
capture and tool calls. Remove the sample backends and set
`update_check = false` before launch for a network-silent configuration; that
daily version request is obsolete now that the package is not published, and
it is being removed.
### Upgrading from v1.1.0 or earlier

Private releases through v1.1.0 used `mcp-gateway` as the distribution name.
Because `uv` correctly treats the public `mcp-local-gateway` distribution as a
different tool, migrate once rather than forcing two tool environments to own
the same command:

```bash
uv tool uninstall mcp-gateway
uv tool install git+https://github.com/voidfreud/mcp-gateway
```

This removes only the old package environment; gateway config, captured state,
logs, and backups are unchanged. On macOS, complete the cutover and verify the
resident service with:

```bash
mcp-gateway service install
```

On Linux and Windows, start `mcp-gateway` in the foreground as usual.


## Checkout install

Contributors who intentionally deploy a checkout on macOS can use:

```bash
git clone https://github.com/voidfreud/mcp-gateway
cd mcp-gateway
./install.sh
```

`install.sh` installs the checkout as a stable `uv` tool and delegates to the
same application-owned service lifecycle. Preview with `./install.sh --dry-run`.
The installed service does not retain a path into the checkout. Existing
checkout-era config is copied to `~/.config/mcp-gateway/config.toml` before the
legacy symlink is removed.

There is no bundled login-service integration outside macOS. On Linux and
Windows, run `mcp-gateway` in a terminal or connect it to the service manager of
your choice; Admin, config, backends, and updates otherwise work the same.

## Where the config lives

The gateway looks for its config file in this order:

1. The path in the `MCP_GATEWAY_CONFIG` environment variable — the macOS login
   service sets this to `~/.config/mcp-gateway/config.toml`.
2. A `config.toml` in the current working directory.
3. `~/.config/mcp-gateway/config.toml`.

The current-directory candidate only wins when it already exists. Whichever
path wins is seeded from the packaged default when it is missing: this can be
the explicit environment path or, when neither of the first two candidates
applies, the home path. The gateway therefore does not always create a home
configuration file. See [configuration.md](configuration.md) for the full
reference.

### Credential references on upgrade

Current releases reject literal values in `bearer_token`,
`oauth.admin_bearer_token`, backend `auth_value`, and any backend
`headers`/`env` value under a credential-like key (names matching
`authorization`, `token`, `secret`, `password`, `api-key`, …). Before
updating an older config that stored such a value directly, move the value to
`~/.config/mcp-gateway/secrets.env` and leave only a reference in
`config.toml`, for example:

```toml
bearer_token = "${MCP_GATEWAY_TOKEN}"
auth_value = "Bearer ${EXA_TOKEN}"
headers = { "X-API-Key" = "${API_KEY}" }
env = { DB_PASSWORD = "${DB_PASSWORD}" }
```

An `env` key ending in `-file`, `-path`, `-dir`, or `-directory` (metadata
paths such as `PASSWORD_FILE`) may stay literal; headers never get that
exemption.

On POSIX systems, the gateway creates the config at `0600` and repairs
existing config and secrets files to that mode when it reads them.

## Updating and rollback

Reinstall from the repository, then restart the service so the new code is
the one running:

```bash
uv tool upgrade mcp-local-gateway
mcp-gateway restart
```

To roll back or pin, install a tag instead:

```bash
uv tool install --reinstall "git+https://github.com/voidfreud/mcp-gateway@vX.Y.Z"
mcp-gateway restart
```

Config, captured defaults, logs, backups, and runtime state are never part of
the package swap.

For a contributor checkout deployment, the `just update` recipe remains the
guarded path (contributor tooling, not the user control interface):

```bash
cd /path/to/mcp-gateway
just update
```

It requires clean `main`, fast-forwards `origin/main`, installs the checkout,
performs one controlled service restart, and verifies health/readiness.

## Moving the repository

The resident service no longer points into the checkout: `./install.sh` installs
the checkout as a `uv` tool and the plist invokes an application-owned stable
wrapper. Moving or deleting the clone therefore does not strand the installed
LaunchAgent. Run `./install.sh` from the new checkout only when you want to
install that checkout's code as the current tool version.

## Uninstalling

Remove the login service through the installed application:

```bash
mcp-gateway service uninstall --yes
```

Uninstall explains what will be removed (plist, wrapper, and legacy checkout
artifacts), boots out the service before deleting its files, and is idempotent.
`--yes` confirms removal — the CLI never prompts. Config, logs, backups, and
state are **retained** by default; delete them too with:

```bash
mcp-gateway service uninstall --yes --purge-data
```

The checkout compatibility forms `./install.sh --uninstall` and
`./install.sh --uninstall --purge` delegate to the same application command;
add `--dry-run` to preview the delegation.

If you registered backends in an MCP client, remove those registrations
separately before uninstalling. For Claude Code:

```bash
claude mcp remove gateway-<name>
```

To remove the separately installed application after removing its service:

```bash
uv tool uninstall mcp-local-gateway
```

Removing only the application first leaves the LaunchAgent inert rather than in
a crash loop: the stable wrapper exits successfully when its executable is
absent. Still, service-first removal is the supported order.

## Next steps

- [admin-guide.md](admin-guide.md) — a tour of the web admin UI.
- [configuration.md](configuration.md) — the full `config.toml` reference.
- [operations.md](operations.md) — running, logs, health checks, troubleshooting.
- [security.md](security.md) — what the gateway protects and what it does not.
- [releases.md](releases.md) — tags and installing a pinned release.
