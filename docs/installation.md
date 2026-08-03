# Installation

mcp-gateway runs in the foreground on every supported platform and can install
itself as a resident macOS login service. The normal distribution is the public
`mcp-local-gateway` package on PyPI; it installs the unchanged `mcp-gateway`
command and `mcp_gateway` import package. Verified private GitHub Release assets
and checkout installs remain fallback/contributor paths.

## Prerequisites

You need [`uv`](https://docs.astral.sh/uv/), Astral's Python tool manager. It
handles Python and every dependency; you do not need to install Python
separately:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

GitHub authentication is not required for the normal PyPI install. You need the
[GitHub CLI](https://cli.github.com/) and repository access only for private
release artifacts or a checkout. Install [`just`](https://just.systems/) only
for contributor/repository recipes.

Claude Code and Codex CLIs are optional. When present, the Admin UI can register
independent backend endpoints in those clients; manual registration always
remains available.

## Recommended — public PyPI package

```bash
uv tool install mcp-local-gateway
mcp-gateway
```

The distribution name differs because another project already owns
`mcp-gateway` on PyPI. Package installation itself never changes services or
user state. On the first interactive no-argument launch on macOS, the program
offers to install its resident LaunchAgent once; accepting completes setup.
Declining records the choice and starts in the foreground. Use
`mcp-gateway --foreground` to bypass the offer explicitly.

The macOS application-owned lifecycle creates config/state directories, an
atomic versioned LaunchAgent plist, and a stable wrapper under
`~/.local/libexec/mcp-gateway/`. It captures the installing shell's `PATH`,
starts the service, and requires `/health` and `/ready`. A failed replacement
restores the prior plist/wrapper and service. Inspect gateway plus backend-child
resources on demand:

```bash
mcp-gateway --service-status
```

Resident mode keeps one gateway process alive. Warm `stdio` backends can remain
its children; disabled backends consume no session resources, and stateless mode
remains available when a backend need not stay warm.

A fresh run normally seeds `~/.config/mcp-gateway/config.toml`. The bundled
DeepWiki and Context7 examples contact those services for initial catalog
capture and tool calls. With `update_check = true` (the default), the daemon also
makes one lightweight PyPI version request at startup and daily; failures are
offline-tolerant and updates are never auto-applied. Remove the sample backends
and set `update_check = false` before launch for a network-silent configuration.
### Upgrading from v1.1.0 or earlier

Private releases through v1.1.0 used `mcp-gateway` as the distribution name.
Because `uv` correctly treats the public `mcp-local-gateway` distribution as a
different tool, migrate once rather than forcing two tool environments to own
the same command:

```bash
uv tool uninstall mcp-gateway
uv tool install mcp-local-gateway
```

This removes only the old package environment; gateway config, captured state,
logs, and backups are unchanged. On macOS, complete the cutover and verify the
resident service with:

```bash
mcp-gateway --install-service
```

On Linux and Windows, start `mcp-gateway` in the foreground as usual.


## Authenticated fallback and checkout install

Every release continues to carry checksummed wheel/source artifacts in the
private GitHub repository. Use this only when PyPI is unavailable or when
reviewing a specific private artifact:

```bash
gh auth login
gh release download vX.Y.Z --repo voidfreud/mcp-gateway --dir mcp-gateway-vX.Y.Z
cd mcp-gateway-vX.Y.Z
shasum -a 256 -c SHA256SUMS
uv tool install --reinstall ./*.whl
mcp-gateway
```

The checksum file verifies all downloaded release assets. On systems without
`shasum`, use an equivalent SHA-256 checker. The SBOM is evidence, not an install
input. Do not use mutable `main` as a stable package source.

Contributors who intentionally deploy a checkout on macOS can use:

```bash
gh auth login
gh auth setup-git
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

## Updating and rollback

The normal update path requires no GitHub authentication:

```bash
mcp-gateway update
```

It resolves the latest published `mcp-local-gateway` version, asks `uv` to
install that exact version, verifies the new command, and—when the resident
service exists—uses the newly installed code to refresh service files, restart,
and require `/health` plus `/ready`. Config, captured defaults, logs, backups,
and runtime state are never part of the package swap.

For provenance, the updater ignores ambient package-index, find-links,
constraint, and override settings and resolves from public PyPI only. Network
proxy and certificate environment settings still apply. Use the verified
private-release fallback when that fixed source is unavailable.


Use the same command with an exact published version to roll back:

```bash
mcp-gateway update --version X.Y.Z
```

If activation fails after a package swap, the command automatically attempts to
reinstall and restart the old exact version and reports whether rollback
succeeded. Versions predating the first `mcp-local-gateway` PyPI release remain
available only through the authenticated GitHub Release fallback above.

For a contributor checkout deployment, `just update` remains the guarded path:

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
mcp-gateway --uninstall-service
```

An interactive uninstall explains that the plist, wrapper, prompt marker, and
legacy checkout artifacts will be removed, then asks whether to keep config,
logs, backups, and state. Non-interactive callers must make retention explicit:

```bash
mcp-gateway --uninstall-service --keep-data
mcp-gateway --uninstall-service --purge-data
```

Removal boots out the service before deleting its files and is idempotent. The
checkout compatibility forms `./install.sh --uninstall` and
`./install.sh --uninstall --purge` delegate to the same application command;
add `--dry-run` to preview the delegation.

If you registered backends in an MCP client, remove those registrations
separately (or use the admin UI's remove buttons before uninstalling). For
Claude Code:

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
- [releases.md](releases.md) — release automation, PyPI publishing, and fallback artifacts.
