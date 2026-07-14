# Operations

Running the gateway day to day: the login service, logs, health checks, backups,
recovery, and a troubleshooting table.

## The daemon

On macOS (Path A install) the gateway runs as a `launchd` LaunchAgent with the
label **`com.void.mcp-gateway`**. `RunAtLoad` starts it when you log in;
`KeepAlive` restarts it if it ever dies. It runs the `mcp-gateway` program from
your clone's environment directly — one resident process, no supervisor.

Common commands (run them as-is; `$(id -u)` fills in your user id):

```bash
# status
launchctl print gui/$(id -u)/com.void.mcp-gateway

# restart
launchctl kickstart -k gui/$(id -u)/com.void.mcp-gateway

# stop and unload
launchctl bootout gui/$(id -u)/com.void.mcp-gateway
```

You can also restart it from the admin UI's **⚙ Gateway → Restart** button.

If you installed as a standalone tool (Path B) there is no login service — you
started `mcp-gateway` yourself, and you stop it the same way (close it, or
Ctrl-C).

## Health and readiness

Two HTTP endpoints report status. Both are always reachable (no bearer token
needed).

**`/health` — is it alive, and which build?**

```bash
curl -s http://127.0.0.1:9100/health
# -> ok mcp-gateway <version> @ /path/the/daemon/runs/from
```

The reply names the directory the running daemon was actually started from —
useful for catching a stale process (see below).

**`/ready` — is it up *and* are all enabled backends mounted?**

```bash
curl -s http://127.0.0.1:9100/ready
```

Returns JSON listing which backends are `mounted`, which are `enabled`, and which
are `missing`. HTTP status is `200` when everything is ready and `503` when an
enabled backend has not mounted — so a monitor can tell "up" from "up but
degraded".

### The daemon is running from an old clone (a ghost process)

The login service references your clone through a stable symlink, so a repo move
is handled by re-running `./install.sh`. But if a leftover process from an old
clone is still running, it can keep `/health` green while the installed service
points elsewhere. To catch it, compare the path in `/health` against where your
clone actually lives:

```bash
curl -s http://127.0.0.1:9100/health
```

If the path is not your current clone, you are talking to a ghost. Re-run
`./install.sh` from the correct location (it unloads the old job and starts the
right one).

## Logs

The gateway writes structured JSON logs (one event per line) to
**`~/.local/state/mcp-gateway/gateway.log`** — connection events, tool calls with
latency, and errors. This file **rotates automatically**: 5 MB per file, up to 5
files kept. Library logging (from the web server and the MCP framework) at WARNING
and above is folded into the same file; routine informational chatter from those
libraries is dropped.

Two other files live alongside it under `~/.local/state/mcp-gateway/`:

- **`out.log`** and **`err.log`** — the login service's own capture of anything
  the process prints outside the structured logger. Because nearly all logging
  goes to `gateway.log`, these only catch rare pre-startup or hard-crash text and
  stay small.

For a hard cap on `out.log`/`err.log`, the repo ships an optional `newsyslog`
config at `deploy/newsyslog-mcp-gateway.conf`. It is optional and needs its
absolute paths edited for your account; follow the comments in the file to
install it.

## Backups and captured defaults

Everything the gateway persists lives under `~/.local/state/mcp-gateway/`:

- **`backups/`** — every time you save config, the previous `config.toml` is
  snapshotted here as `config-<timestamp>.toml`. The last 30 snapshots are kept.
  This is your undo history.
- **`defaults/`** — one `<backend>.json` per backend, holding the backend's
  **original** captured tool list, parameters, and server instructions. This is
  the immutable baseline the UI diffs your overrides against, and the source for
  "reset to default." It is re-captured on connect, on a backend's own change
  notification, on admin page load (throttled), and on **Re-inspect**.
  The connect-time re-capture is **age-gated**: a baseline younger than
  `baseline_max_age` (default 24 h) is kept as-is, so a routine restart does
  not cold-start every slow stdio backend twice — the log line for a skip is
  `baseline_fresh_skipped`. Set `baseline_max_age = 0` to re-capture on every
  mount, or press **Re-inspect** for an immediate refresh (never gated). See
  [configuration.md](configuration.md#gateway-settings-top-level).
  A boot also sweeps `defaults/` files for backends no longer in the config —
  but refuses (`orphan_sweep_refused` in the log) when more than half the
  files would go, which means the running config isn't the one that captured
  them (e.g. a scratch daemon pointed at a test config while sharing the real
  state dir). Nothing is deleted in that case.

## Recovering from a bad config

If `config.toml` ever becomes invalid, the daemon does **not** crash-loop. On
startup it validates the config; if it fails to load, it falls back to the most
recent good snapshot from `backups/` and logs the problem, so the gateway stays
up on the last known-good settings. To recover deliberately:

1. Look at the error in `gateway.log`.
2. Fix `config.toml` by hand, or copy a good snapshot from `backups/` over it.
3. Restart the daemon (`launchctl kickstart -k …` or the UI's Restart button).

The starter/default config ships inside the package and seeds a fresh
`config.toml` automatically if the file is missing entirely — so deleting
`config.toml` and restarting gives you a clean, working baseline.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---------|--------------|------------|
| `/health` doesn't respond at all | Daemon not running | `launchctl print gui/$(id -u)/com.void.mcp-gateway` for status; `launchctl kickstart -k …` to (re)start. Check `gateway.log` and `err.log`. |
| `/health` green but from the wrong path | Ghost process from an old/moved clone | Re-run `./install.sh` from the current clone location. |
| `/ready` returns 503; a backend shows red in the UI | An enabled backend failed to connect or mount | Check that backend's URL/command and its secret; use **Re-inspect** in the UI, or read the error on its status dot. Other backends keep working. |
| Every call to the gateway returns 401 | A bearer token is set but the caller isn't sending it | Register the backend in Claude Code with the `Authorization` header (the UI's **Register** button in the Claude Code cluster does this automatically), or paste the token when the admin UI prompts. See [security.md](security.md). |
| Claude Code still shows the old tool name/description after an edit | The session hasn't re-listed the backend's tools yet | Reconnect the MCP server, start a new session, or trigger a tool use. Text edits are live in the gateway immediately, but a connected session caches the old broadcast. |
| A tool you renamed can't be saved | Its name (or a deliberately identical description) collides with another tool | Pick a unique name, or turn on **auto-uniquify** in the ⚙ Gateway header for bulk renames. |
| Claude sees a backend's tools twice | The backend is registered *both* directly and through the gateway | Remove the direct registration so Claude sees only the gateway's rewritten version. |
| A benign-looking `reusing existing session … context mixing` line in the log | Framework informational message | Expected and harmless here; it is quieted to WARNING and does not indicate a problem. |

## Related

- [installation.md](installation.md) — install, upgrade, move, uninstall.
- [security.md](security.md) — the bearer token and what is (not) protected.
- [configuration.md](configuration.md) — the config file the UI writes.
