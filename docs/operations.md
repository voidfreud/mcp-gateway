# Operations

Running the gateway day to day: the login service, logs, health checks, backups,
recovery, and a troubleshooting table.

## The daemon

On macOS the optional resident mode is a `launchd` LaunchAgent with label
**`com.void.mcp-gateway`**. `RunAtLoad` starts it at login and `KeepAlive`
restarts non-zero failures. The plist invokes an application-owned stable
wrapper rather than a checkout path, so moving or deleting a source clone does
not strand the job.

Application-owned lifecycle commands:

```bash
# install or repair, then require /health and /ready
mcp-gateway --install-service

# loaded state plus gateway and backend-child RSS
mcp-gateway --service-status

# remove service files and explicitly retain user data
mcp-gateway --uninstall-service --keep-data
```

An interactive no-argument launch offers resident setup once on macOS.
`mcp-gateway --foreground` always bypasses that offer. Uninstall without a data
flag asks whether to keep config and state; non-interactive callers must pass
`--keep-data` or `--purge-data`.

You can restart the loaded job from the admin UI's **Gateway → Restart** button,
or directly with:

```bash
launchctl kickstart -k gui/$(id -u)/com.void.mcp-gateway
```

### Resource footprint

Resident mode keeps one gateway process alive. Warm `stdio` backends can remain
child processes so repeated calls avoid session startup; that configurable
latency tradeoff, not service-management overhead, will usually dominate memory.
Disabled backends consume no session resources, and backends that do not need a
warm session can use stateless mode.

`mcp-gateway --service-status` measures the gateway and its complete descendant
process tree once. It does not add polling, workers, or a daemon-side memory
limit. From a checkout, the disposable receipt below starts isolated foreground
instances, samples settled CPU/RSS across restart cycles, and never touches the
installed service or live backends:

```bash
uv run python tools/measure_service_resources.py --cycles 3 --settle 5
```

Review first-to-last RSS and sample spread for growth rather than treating one
machine's absolute footprint as a universal ceiling.

### Updating the login service

After a change is merged to GitHub, use `just update` from a clean checkout on
`main`. This guarded checkout deployment fast-forwards source, installs the
checkout as the stable tool, performs one controlled service restart, and
requires `/health` plus `/ready` before reporting success. It does not overwrite
config or runtime state. See [installation.md](installation.md#upgrading-path-a).

The update command refuses dirty worktrees and feature branches. It is stateful
and may briefly drop active MCP sessions, after which clients can reconnect. Do
not use it for a foreground release-wheel installation.

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
**`~/.local/state/mcp-gateway/gateway.log`** — connection events, HTTP requests,
tool calls with latency, configuration saves, refreshes/recycles, and errors.
Logging is event-based: a line is emitted when an operation or request produces
an event, not on a fixed heartbeat or timer. Every event starts with an ISO
timestamp field, followed by level, logger, event name, and call-site data;
timing events also carry `ms`. The file handler runs on a listener thread, so
request and MCP call paths enqueue records without waiting on filesystem I/O.
The queue is bounded; an overload is visible as `dropped_events` in the
dashboard/log status.

The active file **rotates automatically**: 5 MiB per file, up to 5 files kept by
default. Set `log_level`, `log_max_bytes`, and `log_backup_count` in
`config.toml`; the Gateway settings card also exposes `log_level`. Changes take
effect after the normal daemon restart. Library logging (from the web server and the MCP
framework) is WARNING-and-above by default; `DEBUG` enables their routine
diagnostics too.

### Viewing and collecting logs

The Gateway row in the Admin dashboard includes a live, bounded tail of the
structured log and the listener/queue/retention counters. The file updates on
events; the dashboard polls for new entries every 3 seconds and displays the
last refresh time. It never exposes the log path as a browser-controlled
parameter and reads the file off the event loop. From a shell, use:

```bash
just logs             # last 100 JSON events
just logs 250         # last 250 events
just logs-follow      # follow the active file (Ctrl-C to stop)
```

Set `MCP_GATEWAY_LOG_FILE` when inspecting a non-default path. The dashboard
endpoint is `GET /admin/api/logs?limit=100` and accepts optional exact `level`
and `event` filters. Rotated files remain available beside the active file for
longer incident review.

Two other files live alongside it under `~/.local/state/mcp-gateway/`:

- **`out.log`** and **`err.log`** — the login service's own capture of anything
  the process prints outside the structured logger. Because nearly all logging
  goes to `gateway.log`, these only catch rare pre-startup or hard-crash text and
  stay small.

For a hard cap on `out.log`/`err.log`, create an optional local `newsyslog`
configuration from `newsyslog.conf(5)`. It must use your own absolute paths and
account ownership, so the repository deliberately does not ship one.

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

## What automated checks do not cover

`just check` is the repeatable local gate, and CI runs it together with a
hermetic MCP conformance job. Those checks use disposable fixtures: they do not
contact your personal backends, read your secrets, or exercise your installed
daemon and launchd state.

Test the installed service, local daemon behavior, and integrations with your
own client/harness separately. `just verify` is deliberately opt-in: it may
contact the public DeepWiki backend, sends neither bearer nor OAuth credentials,
and therefore fits only an equivalent unprotected test instance. See the
[admin guide](admin-guide.md) and [security guide](security.md) before using a
credentialed or remote deployment for live verification.

## Troubleshooting

| Symptom | Likely cause | What to do |
|---------|--------------|------------|
| `/health` doesn't respond at all | Daemon not running | `launchctl print gui/$(id -u)/com.void.mcp-gateway` for status; `launchctl kickstart -k …` to (re)start. Check `gateway.log` and `err.log`. |
| `/health` green but from the wrong path | Ghost process from an old/moved clone | Re-run `./install.sh` from the current clone location. |
| `/ready` returns 503; a backend shows red in the UI | An enabled backend failed to connect or mount | Check that backend's URL/command and its secret; use **Re-inspect** in the UI, or read the error on its status dot. Other backends keep working. |
| Every call to the gateway returns 401 | A bearer token is set but the caller is not sending it | Use the matching Claude Code or Codex registration flow described in [security.md](security.md), or provide the token to the admin UI when prompted. |
| A client still shows the old tool name/description after an edit | The session has not re-listed the backend's tools yet | Reconnect the MCP server, start a new session, or trigger a tool use. Text edits are live in the gateway immediately, but a connected session can cache the old broadcast. |
| A tool you renamed can't be saved | Its name (or a deliberately identical description) collides with another tool | Pick a unique name, or turn on **auto-uniquify** in the ⚙ Gateway header for bulk renames. |
| A client sees a backend's tools twice | The backend is registered *both* directly and through the gateway | Remove the direct registration so the client sees only the gateway's rewritten version. |
| A benign-looking `reusing existing session … context mixing` line in the log | Framework informational message | Expected and harmless here; it is quieted to WARNING and does not indicate a problem. |

## Related

- [installation.md](installation.md) — install, upgrade, move, uninstall.
- [security.md](security.md) — the bearer token and what is (not) protected.
- [configuration.md](configuration.md) — the config file the UI writes.
