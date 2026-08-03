# Installation

mcp-gateway can run in the foreground on any platform or as a macOS login
service. There are two installation paths:

- **A verified GitHub Release wheel (stable, any platform).** Download a tagged
  private release with the GitHub CLI, verify its checksum, install the wheel
  with `uv`, and start it in the foreground. No login service is set up; this is
  the stable installation path.
- **A checkout as a macOS login service.** Contributors and operators who need
  the repository deployment workflow can clone it and run `./install.sh`. The
  gateway then starts automatically every time they log in and stays in the
  background.

Both paths are described below.

## Prerequisites

You need [`uv`](https://docs.astral.sh/uv/), Astral's Python tool manager. It
handles Python and every dependency for you — you do not need to install Python
separately. To install `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

The repository is private. Authenticate the [GitHub CLI](https://cli.github.com/)
with an account authorized to read it before downloading a release. Before an
HTTPS clone, run `gh auth setup-git` after `gh auth login` so Git can use that
authorized account. Install [`just`](https://just.systems/) only when you need
the repository or macOS recipes, including the guarded `just update` command.

To register backends from inside the admin UI, install the client CLI you want
the gateway to manage: `claude` for Claude Code and/or `codex` for Codex. On
macOS the gateway also detects the Codex executable bundled with ChatGPT desktop.
Both integrations are optional—you can always register each `/<backend>/mcp`
endpoint by hand.

## Path A — checkout and install as a login service (macOS)

Use this stateful deployment path when you intentionally operate from a
repository checkout. It is not the stable release-asset channel.

```bash
gh auth login
gh auth setup-git
git clone https://github.com/voidfreud/mcp-gateway
cd mcp-gateway
./install.sh
```

The compatibility script installs the checkout as a `uv` tool, then delegates
service ownership to the installed `mcp-gateway` command. Preview both actions
without changing the machine with `./install.sh --dry-run`.

The application creates the state and config directories, an atomic versioned
LaunchAgent plist, and a stable wrapper under
`~/.local/libexec/mcp-gateway/`. It captures the installing shell's `PATH`,
starts the service, and requires both `/health` and `/ready` to pass. Repeating
the command is idempotent: unchanged files are not rewritten or double-loaded.
A failed changed install restores the previous plist and wrapper.

The resident service is one gateway process. Warm `stdio` backends can remain
its child processes; disabled backends consume no session resources, and
stateless mode remains available for backends that do not need a warm session.
Inspect the whole resident process tree on demand:

```bash
mcp-gateway --service-status
```

To start without installing a login service, use
`mcp-gateway --foreground`. On the first interactive no-argument launch on
macOS, the command offers service installation once. Declining records that
choice and starts in the foreground; it does not prompt again on every run.

### Machine-specific paths

The installed plist contains absolute paths because `launchd` does not
understand `~`. The application renders those paths locally; no personal path
is stored in the repository. The service uses
`~/.config/mcp-gateway/config.toml` and writes launch capture logs plus gateway
state under `~/.local/state/mcp-gateway/`. If an older checkout installation
exists, installation copies its `config.toml` to the home config path before
removing the legacy `~/.local/opt/mcp-gateway` symlink.

If you need optional `newsyslog` rotation for launch capture logs, create its
account-specific configuration locally from `newsyslog.conf(5)`; it must use
your absolute paths and account ownership and is not a tracked project file
(see [operations.md](operations.md#logs)).

## Path B — install a stable release (any platform)

This installs the `mcp-gateway` command globally from a verified GitHub Release
wheel. It does not install a login service during package installation. In the
commands below, replace `vX.Y.Z` with a release tag after reviewing its notes.

```bash
gh auth login
gh release download vX.Y.Z --repo voidfreud/mcp-gateway --dir mcp-gateway-vX.Y.Z
cd mcp-gateway-vX.Y.Z
shasum -a 256 -c SHA256SUMS
uv tool install --reinstall ./mcp_gateway-*.whl
mcp-gateway
```

`gh release download` uses the authenticated account for this private
repository. On systems without `shasum`, use an equivalent SHA-256 checker.
The release may also contain an SBOM; it is not an install input. Do not use a
mutable `main` Git URL as stable-install guidance. A tag-pinned Git install is
only a developer convenience; see [releases.md](releases.md#installing-a-private-release).

The first time it runs, a fresh standalone install normally creates a starter
config at `~/.config/mcp-gateway/config.toml` (see [Where the config
lives](#where-the-config-lives) for the exact selection rule). On macOS, an
interactive `mcp-gateway` launch first offers to install the resident login
service; use `mcp-gateway --foreground` to bypass that offer. Other platforms
start in the foreground.

The shipped default contains two public sample backends, DeepWiki and Context7.
They are stateless proxies once running, but a freshly seeded default
configuration with no captured-default state is not network-silent: before the
app mounts its endpoints, startup connects to both public services to capture
each backend's baseline metadata and tool list. Complete captured defaults are
normally reused on later starts. That initial capture is separate from ordinary
proxy use; tool calls can also make backend requests. Edit or remove those
entries before starting the gateway if those outbound connections are not
appropriate for your environment.

To confirm the installed version:

```bash
mcp-gateway --version
```

> **Why a GitHub Release and not PyPI?** Releases are private GitHub assets by
> choice. The `mcp-gateway` name is already taken on PyPI, and the project's
> package metadata deliberately blocks an accidental upload there.

### On Linux and Windows

There is no login service outside macOS (the login integration uses macOS's
`launchd`). Use Path B and start `mcp-gateway` yourself — for example from a
terminal you keep open, or from your own service manager (systemd, a startup
script, and so on). Everything else — the admin UI, config, backends — works the
same.

## Where the config lives

The gateway looks for its config file in this order:

1. The path in the `MCP_GATEWAY_CONFIG` environment variable — the login service
   (Path A) sets this to `~/.config/mcp-gateway/config.toml`.
2. A `config.toml` in the current working directory.
3. `~/.config/mcp-gateway/config.toml`.

The current-directory candidate only wins when it already exists. Whichever
path wins is seeded from the packaged default when it is missing: this can be
the explicit environment path or, when neither of the first two candidates
applies, the home path. The gateway therefore does not always create a home
configuration file. See [configuration.md](configuration.md) for the full
reference.

## Upgrading (Path A)

From a **clean checkout on `main`**, run:

```bash
cd /path/to/mcp-gateway
just update
```

The guarded recipe fast-forwards from `origin/main`, runs `uv sync --locked`,
reloads the LaunchAgent through `./install.sh`, and waits for both `/health` and
`/ready`. It is a stateful deployment operation, not a generic package upgrade:
it refuses a feature branch or dirty checkout and can briefly interrupt active
MCP sessions. Your `config.toml`, captured defaults, backups, and runtime state
are not part of the Git update and are preserved.

If you prefer to run the steps separately:

```bash
git pull --ff-only origin main
uv sync --locked
./install.sh
```

Confirm the new version with the `/health` check above. A restart briefly drops
active MCP sessions; clients reconnect to the freshly loaded daemon.

For Path B, download and verify the next release wheel, then reinstall it:

```bash
gh release download vX.Y.Z --repo voidfreud/mcp-gateway --dir mcp-gateway-vX.Y.Z
cd mcp-gateway-vX.Y.Z
shasum -a 256 -c SHA256SUMS
uv tool install --reinstall ./mcp_gateway-*.whl
```

For foreground development instead of a stable installation, clone the
repository, run `uv sync --locked`, then start `uv run mcp-gateway`. Do not
treat an unpinned `main` checkout as a release.

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
uv tool uninstall mcp-gateway
```

Removing only the application first leaves the LaunchAgent inert rather than in
a crash loop: the stable wrapper exits successfully when its executable is
absent. Still, service-first removal is the supported order.

## Next steps

- [admin-guide.md](admin-guide.md) — a tour of the web admin UI.
- [configuration.md](configuration.md) — the full `config.toml` reference.
- [operations.md](operations.md) — running, logs, health checks, troubleshooting.
- [security.md](security.md) — what the gateway protects and what it does not.
- [releases.md](releases.md) — release automation and private release use.
