# Installation

mcp-gateway runs as a single background service on your Mac. There are two ways
to install it:

- **From a clone, as a login service (recommended, macOS).** You clone the
  repository and run `./install.sh`. The gateway then starts automatically every
  time you log in and stays running in the background. This is the intended setup.
- **As a standalone tool.** You install the `mcp-gateway` command with `uv` and
  start it yourself when you want it. No login service is set up; on Linux or
  Windows this is the only option.

Both paths are described below.

## Prerequisites

You need [`uv`](https://docs.astral.sh/uv/), Astral's Python tool manager. It
handles Python and every dependency for you — you do not need to install Python
separately. To install `uv`:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

To register backends from inside the admin UI, install the client CLI you want
the gateway to manage: `claude` for the Claude Code controls and/or `codex` for
the Codex controls. On macOS the gateway also detects the Codex executable
bundled with ChatGPT desktop. Both integrations are optional—you can always
register each `/<backend>/mcp` endpoint by hand.

## Path A — clone and install as a login service (macOS)

This is the recommended setup on a Mac.

```bash
git clone https://github.com/voidfreud/mcp-gateway
cd mcp-gateway
./install.sh
```

That is the whole install. To preview exactly what the script will do without
changing anything, run `./install.sh --dry-run` first.

### What `install.sh` does

The script is safe to run repeatedly. Each run:

1. **Creates the environment.** If the project's `.venv` does not exist yet, it
   runs `uv sync` to build it and install every dependency. This produces the
   `mcp-gateway` program the service runs.
2. **Wires a stable symlink.** It points `~/.local/opt/mcp-gateway` at wherever
   your clone actually lives. The login service references only this symlink,
   never the clone's real path — so you can move the clone later and fix
   everything by re-running the script (see [Moving the repository](#moving-the-repository)).
3. **Installs the login service.** It fills in your home directory in
   `deploy/com.void.mcp-gateway.plist.template` and writes the result to
   `~/Library/LaunchAgents/com.void.mcp-gateway.plist`. (The template in the repo
   carries no personal paths, so it is safe to share.)
4. **Starts the service.** It unloads any previous copy, loads the fresh one, and
   starts the daemon immediately.

When it finishes it prints a `curl` command to confirm the service is up:

```bash
curl -s http://127.0.0.1:9100/health
# -> ok mcp-gateway <version> @ /path/to/your/clone
```

The path in that reply is the directory the running service was actually started
from. It should match where your clone lives. If it does not, see
[operations.md](operations.md#the-daemon-is-running-from-an-old-clone-a-ghost-process).

### Machine-specific paths

The login service starts under macOS's `launchd`, which does not understand `~`,
so the installed service file contains your home directory as a full path.
`install.sh` handles that for you. If you need optional `newsyslog` rotation for
the launchd capture logs, create its account-specific configuration locally from
`newsyslog.conf(5)`; it must use your absolute paths and account ownership and is
not a tracked project file (see [operations.md](operations.md#logs)).

## Path B — install as a standalone tool (any platform)

This installs the `mcp-gateway` command globally with `uv`, straight from GitHub.
It does **not** set up a login service — you start the gateway yourself.

```bash
uv tool install git+https://github.com/voidfreud/mcp-gateway
mcp-gateway
```

The first time it runs, the gateway creates a starter config at
`~/.config/mcp-gateway/config.toml` (a copy of the shipped default, which
proxies three sample backends). Edit that file, or use the admin UI, to add your
own backends. The gateway runs in the foreground and stops when you close it or
press Ctrl-C — start it again whenever you need it.

To confirm the installed version:

```bash
mcp-gateway --version
```

> **Why install from GitHub and not PyPI?** Distribution is uv-from-GitHub by
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
   (Path A) sets this to the `config.toml` inside your clone.
2. A `config.toml` in the current working directory.
3. `~/.config/mcp-gateway/config.toml` — used by Path B, and auto-seeded on first
   run.

In every case the file is created from a working default if it does not exist
yet, so the gateway always starts. See [configuration.md](configuration.md) for
the full reference.

## Upgrading (Path A)

From a **clean checkout on `main`**, run:

```bash
cd /path/to/mcp-gateway
just update
```

The guarded recipe fast-forwards from `origin/main`, runs `uv sync --locked`,
reloads the LaunchAgent through `./install.sh`, and verifies both `/health` and
`/ready`. It refuses to deploy a feature branch or a dirty checkout. Your
`config.toml`, captured defaults, backups, and runtime state are not part of the
Git update and are preserved.

If you prefer to run the steps separately:

```bash
git pull --ff-only origin main
uv sync --locked
./install.sh
```

Confirm the new version with the `/health` check above. A restart briefly drops
active MCP sessions; clients reconnect to the freshly loaded daemon.

For Path B, upgrade with:

```bash
uv tool upgrade mcp-gateway
```

## Moving the repository

If you move or rename your clone (Path A), the login service would otherwise
point at the old location. Fix it in one step — from the **new** location, run:

```bash
./install.sh
```

That repoints the stable symlink, refreshes the installed service file, and
restarts the daemon. Nothing else is needed. Confirm with the `/health` check —
the path it reports should now be the new location.

## Uninstalling

**Path A (login service)** — one command, symmetric with the install:

```bash
./install.sh --uninstall
```

That boots out the LaunchAgent, removes the installed service file
(`~/Library/LaunchAgents/com.void.mcp-gateway.plist`) and the stable symlink
(`~/.local/opt/mcp-gateway`), and prints exactly what was removed and what was
deliberately kept. It is idempotent — running it twice, or without an install
present, exits cleanly saying so. Preview with `--dry-run`.

Your data is **kept** by default: config (`./config.toml` in the clone and/or
`~/.config/mcp-gateway/`) and runtime state — logs and config backups — under
`~/.local/state/mcp-gateway/`. To delete the config and state directories too,
add `--purge` (asks for confirmation; irreversible — the backups live there):

```bash
./install.sh --uninstall --purge
```

If you registered backends in Claude Code, remove those yourself — the script
cannot do it safely and will remind you:

```bash
claude mcp remove gateway-<name>   # for each registered backend
```

(or use the admin UI's remove buttons *before* uninstalling). Then delete the
clone if you want.

The manual recipe, for reference (what `--uninstall` does):

```bash
launchctl bootout gui/$(id -u)/com.void.mcp-gateway
rm ~/Library/LaunchAgents/com.void.mcp-gateway.plist
rm ~/.local/opt/mcp-gateway
```

**Path B (standalone tool):**

```bash
uv tool uninstall mcp-gateway
```

This removes only the `mcp-gateway` binary; the same data notes apply —
`~/.config/mcp-gateway/`, `~/.local/state/mcp-gateway/`, and any Claude Code
registrations stay until you remove them by hand.

## Next steps

- [admin-guide.md](admin-guide.md) — a tour of the web admin UI.
- [configuration.md](configuration.md) — the full `config.toml` reference.
- [operations.md](operations.md) — running, logs, health checks, troubleshooting.
- [security.md](security.md) — what the gateway protects and what it does not.
