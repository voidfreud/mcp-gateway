# Operations

Running the gateway day to day: the login service, logs, health checks, backups,
recovery, and a troubleshooting table.

## Command line reference

`mcp-gateway` is one command with grouped subcommands. It is a client of the
same [admin API](api.md) the UI uses, so each domain exposes the same
operations the dashboard offers. `--help` works at every level
(`mcp-gateway --help`, `mcp-gateway backend --help`, …), and every finite,
result-producing command supports `--json` output.

### Global options

Accepted anywhere on the command line (before or after the command):

| Option | Meaning |
|--------|---------|
| `--url URL` | Admin API base URL. Defaults to the resolved config's `host`/`port` (the config is read if it exists, never created), else `http://127.0.0.1:9100`. An explicit `--url` is used verbatim and disables every implicit token source — see **Tokens** below. |
| `--config PATH` | Config file to derive `--url` and the token reference from. Same resolution as the daemon: `MCP_GATEWAY_CONFIG`, then `./config.toml`, then `~/.config/mcp-gateway/config.toml`. |
| `--token-env NAME` | Environment variable holding the bearer token. The named variable must be set, or the command fails. This is the only token source that applies when `--url` is explicit. (`check` is the exception: it probes the auth-exempt `/ready` and ignores token options.) |
| `--timeout SECS` | Per-request timeout for admin API calls (default 30 seconds). |
| `--json` | Print exactly one JSON value on stdout for finite, result-producing commands (the admin-API command groups). Two exceptions: streaming `logs follow --json` emits one JSON event per line (NDJSON), and commands that produce no result — `run` (starts the daemon) and `--help` — print no JSON. The default human output is concise; errors always go to stderr with a nonzero exit. |

**Local vs network commands.** `run`, `version`, `update`, and `service …`
are local commands: the CLI never constructs an Admin API client for them, so
no Admin token resolution and no config read happen as part of CLI setup, and
the network options (`--url`, `--config`, `--token-env`, `--timeout`) are
ignored — `version` works with no config at all. Their handlers still do what
the operation itself requires: `run` starts the daemon, which loads (or seeds)
the config, and `service install`/`update` probe the configured `host`/`port`
to verify the service, so those read config and fail loudly if it is
unreadable. The network options affect the commands that talk to the gateway:
`status`, `check`, `restart`, and the
`backend`/`tool`/`resource`/`prompt`/`instructions`/`virtual`/`settings`/
`logs` groups. `check` is a partial exception: it derives the URL from config
and honors `--timeout`, but skips token resolution entirely because `/ready`
is auth-exempt — a secrets-only deployment or an unset `--token-env` never
blocks a liveness probe.

**Tokens.** The CLI never accepts a token as an argument and never prints a
resolved one. When the gateway requires a bearer token it resolves, in order:
`--token-env NAME`, `MCP_GATEWAY_ADMIN_TOKEN`, then the configured
`bearer_token` / `oauth.admin_bearer_token` reference — see
[security.md](security.md#the-cli-and-the-bearer-token). Two rules apply:

- **An explicit `--url` receives no implicit token.** If you pass `--url`,
  `MCP_GATEWAY_ADMIN_TOKEN` and the configured token reference are *not*
  applied; authenticating against that explicit endpoint requires
  `--token-env NAME`.
- **Transport follows the token.** A token is sent over plain HTTP only
  toward a verified loopback address (`localhost`, `127/8`, `::1`); any
  other token-bearing target must use HTTPS, and all redirects are refused.
  (For the default config-derived loopback URL, plain HTTP with the implicit
  token is fine.)

**Inputs and safety.** Complex payloads (a full backend definition, a tool-run
argument object, a Virtual Tool definition) are read from a JSON file or from
stdin with `-`, and explicit flags override file fields. Scalar edits have
readable flags. Destructive commands (`backend remove`, `service uninstall`,
`virtual delete`, …) require `--yes`; the CLI never prompts, so scripts are
safe in non-TTY contexts. Human output — including error messages on stderr
— escapes terminal control characters from remote data, so a hostile
description or log line cannot spoof the terminal; `--json` output preserves
the data structurally.

### Command map

| Command | Purpose |
|---------|---------|
| `run` | Run the gateway in the foreground (same as no-argument `mcp-gateway`). |
| `version` | Print the installed version. |
| `update [--version X.Y.Z]` | Install the latest (or an exact prior) published version and restart the resident service — see [installation.md](installation.md#updating-and-rollback). |
| `service install [--restart]` | Install/repair the macOS resident LaunchAgent, start it, and require `/health` and `/ready`. |
| `service status` | Loaded state plus gateway and backend-child resources. |
| `service uninstall [--yes] [--keep-data\|--purge-data]` | Remove the service. Config, logs, backups, and state are kept by default (`--keep-data`); `--purge-data` deletes them too. `--yes` is required. |
| `status` | Per-backend liveness through `GET /admin/api/status`. |
| `check` | Probe `/ready`; print readiness, list missing backends, and exit nonzero when the gateway is dead or degraded. |
| `restart` | Restart the daemon (honest no-op in foreground mode). |
| `backend …` | List, show, add, remove, rename, display-name, enable/disable, pin, session, inspect, limits, refresh backends. |
| `tool …` | List, show, set, reset, run, migrate, discard tool and parameter overrides. |
| `resource …` | List, show, set, reset resource/template overrides. |
| `prompt …` | List, show, set, reset prompt overrides. |
| `instructions …` | Show, set, clear a backend's server instructions (authored overrides must fit the effective byte cap). |
| `virtual …` | List, show, catalog, create, update, delete, validate, test, activate, disable Virtual Tools. |
| `settings …` | Show, set, export, import gateway settings. |
| `logs …` | Show or follow the structured log. |

### Backends

```bash
mcp-gateway backend list                          # name, state, tool counts
mcp-gateway backend show exa                      # one backend: config + live state
mcp-gateway backend add exa --transport http \
  --backend-url https://your-exa-endpoint/mcp
mcp-gateway backend add mytool --transport stdio \
  --command npx --arg "@modelcontextprotocol/server-filesystem" --arg /tmp \
  --env API_KEY='${MYTOOL_KEY}'               # stdio-only; value must reference ${ENV_VAR}
mcp-gateway backend add mytool --transport stdio \
  --command mytool --env-literal DEBUG=1      # clearly non-secret literal env entry
mcp-gateway backend add webtools --transport http \
  --backend-url https://…/mcp \
  --auth-header Authorization --auth-value 'Bearer ${TOKEN}' \
  --header-literal 'X-Trace-Id:abc123'        # literal header (non-credential)
mcp-gateway backend add exa --file backend.json   # full JSON payload from a file
mcp-gateway backend display-name exa "Search"     # cosmetic UI label only
mcp-gateway backend display-name exa --clear      # remove the label
mcp-gateway backend rename exa search             # endpoint, config key, defaults move
mcp-gateway backend enable exa                    # mount live
mcp-gateway backend disable exa                   # unmount; stdio child is shut down
mcp-gateway backend enable-all                    # master switch on
mcp-gateway backend pin exa                       # eager-load every tool
mcp-gateway backend unpin exa
mcp-gateway backend session exa --warm            # persistent connection (default)
mcp-gateway backend session exa --stateless       # fresh session per call
mcp-gateway backend inspect exa                   # force re-capture of the live catalog
mcp-gateway backend refresh                       # throttled sweep of every enabled backend
mcp-gateway backend limits exa                    # read view: stored/effective limits + instruction bytes
mcp-gateway backend limits exa --server-instructions-max-bytes 1024
mcp-gateway backend limits exa --tool-description-max-bytes 4096
mcp-gateway backend limits exa --tool-description-max-bytes inherit  # back to backend default
mcp-gateway backend remove exa --yes              # destructive: confirmation required
```

`backend limits` reads or mutates a backend's per-backend metadata caps
(#286), the same values `PUT /admin/api/backend/{name}/limits` owns. With no
flags it prints the stored limits (`inherit` when unset) plus the effective
values after gateway fallback and the backend's live `instructions_bytes`.
With flags, each value is an integer from `1` to `1,048,576`; the `inherit`
keyword clears the scoped value back to inheriting the gateway cap (the
global settings level uses `unlimited` for its unbounded default instead).

`backend add` mirrors `POST /admin/api/backend`. The backend's endpoint is set
with `--backend-url` — the global `--url` is reserved for the Admin API base
(see [Global options](#global-options)). `--env` sets the stdio process
environment (ignored for remote transports): each value under a
credential-like key must be **exactly** `${VAR}` or a `Bearer|Basic|Token
${VAR}` template — never a raw literal and never mixed raw text plus a
reference — and `--env-literal NAME=VALUE` is the explicit form for clearly
non-secret literals (it rejects credential-like names). An env key ending in
`-file`/`-path`/`-dir`/`-directory` (`PASSWORD_FILE`, `TOKEN_CACHE_DIR`) is
exempt as non-secret metadata; headers never are. Likewise
`--header 'Name:${VAR}'` requires the same exact template while
`--header-literal Name:Value` accepts a literal — except credential-bearing
names (Authorization, Proxy-Authorization, X-API-Key, API-Key, …), which are
rejected in literal form. The server re-validates secret-like persisted keys
(`bearer_token`, backend `auth_value`, Virtual Tool router `api_key`) on
save. `--env-literal` and `--header-literal` are deliberate non-secret
escape hatches: the credential-like-key classifier is a guardrail, not a
completeness guarantee, so never pass a credential through them.
Merging is duplicate-last across a `--file` payload and flags: later entries
for the same name win. `--auth-value` must be exactly a `${VAR}` or
`Bearer|Basic|Token ${VAR}` template (empty clears it), and `show` masks
credentials as `(set)`/`unset` rather than printing them.
`list`/`show` merge live status (`ok`/`disabled`/`unmounted`/`error`) with
the stored definition. `display-name` sets or clears the cosmetic UI label
without touching routing (an empty VALUE or `--clear` removes it).

### Tool, resource, prompt, and instructions

```bash
mcp-gateway tool list                             # every tool, all backends
mcp-gateway tool list --backend exa               # scope to one backend
mcp-gateway tool list --dangling                  # stale overrides only
mcp-gateway tool show exa web_search              # effective broadcast + params
mcp-gateway tool set exa web_search --description "…"
mcp-gateway tool set exa web_search --name web-search --auto-uniquify  # suffix _2/_3 on collision
mcp-gateway tool set exa web_search --param query --param-desc "…"
mcp-gateway tool set exa web_search --pin          # eager load one tool
mcp-gateway tool set exa web_search --max-result-chars 12000
mcp-gateway tool set exa web_search --description-max-bytes 1500
mcp-gateway tool set exa web_search --description-max-bytes inherit  # clear back to inherit
mcp-gateway tool set exa web_search --file edit.json   # full override object ('-' = stdin)
mcp-gateway tool run exa web_search --arg query "…"    # execute through the live proxy
mcp-gateway tool run exa web_search --file args.json
mcp-gateway tool reset exa web_search --yes       # revert to the captured original
mcp-gateway tool migrate exa old_name new_name    # carry a stale override onto the new tool
mcp-gateway tool discard exa old_name --yes       # drop the stale override
mcp-gateway resource list --backend exa
mcp-gateway resource show exa "file:///…"
mcp-gateway resource set exa "file:///…" --description "…"
mcp-gateway resource reset exa "file:///…" --yes
mcp-gateway prompt show exa summarize
mcp-gateway prompt set exa summarize --description "…" --arg detail --arg-desc "…"
mcp-gateway prompt reset exa summarize --yes
mcp-gateway instructions show exa
mcp-gateway instructions set exa "Use this server for library questions."
mcp-gateway instructions set exa --file instructions.txt
mcp-gateway instructions clear exa
```

`instructions set` measures the override in UTF-8 bytes and rejects it when it
exceeds the backend's effective instructions cap — the backend's own
`server_instructions_max_bytes` when set, else the gateway-wide default
(`2048`). Captured upstream instructions that exceed the cap are never a
write error: they are truncated at a UTF-8 character boundary only when
broadcast, and `backend show` / `backend limits` reports the live
`instructions_bytes` so the truncation stays visible.

`tool set` posts the same payload the dashboard sends: scalar flags
(`--name`, `--title`, `--description`, `--enabled`/`--disabled`, `--pin`/
`--unpin`, `--max-result-chars N|none`, `--description-max-bytes N|inherit`,
empty string clears a field) plus repeated `--param NAME` edit groups
(`--param-name`, `--param-desc`, `--param-default`, `--hide`/`--show`).
`--description-max-bytes` caps this tool's broadcast description in UTF-8
bytes (integer `1`..`1,048,576`); `inherit` clears the per-tool value so the
backend's cap, then the gateway's, apply. Tool `show` prints the stored and
effective cap plus the tool's live `description_bytes`. A `--file` JSON
override object is merged with #139 semantics — keys absent preserve stored
values. `--auto-uniquify`
retries a rename collision once with a deterministic `_2`/`_3` suffix instead
of failing (the dashboard's auto-uniquify escape hatch); without it, a
colliding rename is rejected. `tool run` accepts the tool's original or
broadcast name and passes `--arg KEY VALUE` pairs or a `--file` argument
object. `reset`/`discard` are destructive and require `--yes`;
`migrate`/`discard` address the stale-override banner from the dashboard.

### Virtual Tools

```bash
mcp-gateway virtual list                        # drafts and active tools, resolution status
mcp-gateway virtual show summarize
mcp-gateway virtual catalog                     # live source-tool catalog for members
mcp-gateway virtual create --name summarize --description "…" \
  --description-max-bytes 2000 \
  --file def.json                               # or a full definition from a file/stdin
mcp-gateway virtual update summarize --description "…"   # stays a draft until activated
mcp-gateway virtual update summarize --description-max-bytes inherit
mcp-gateway virtual validate summarize          # resolve members against live backends
mcp-gateway virtual test summarize --arguments args.json   # or --arg key=value
mcp-gateway virtual activate summarize          # hot-reload into /virtual/mcp
mcp-gateway virtual disable summarize
mcp-gateway virtual delete summarize --yes      # destructive: confirmation required
```

`create`/`update` accept the full `VirtualTool` JSON definition via `--file`
(or `-` for stdin); repeated `--input JSON` / `--member JSON` items replace the
respective lists, and scalar flags (`--description`, `--description-max-bytes`,
`--dispatch`, `--member`, `--router-*`, …) overlay single fields.
`--description-max-bytes N|inherit` caps the definition's broadcast
description in UTF-8 bytes (integer `1`..`1,048,576`); `inherit` follows the
gateway-wide `tool_description_max_bytes` (which itself defaults to
unlimited). `update` merges over the current definition and the server always
stores the result as an inactive draft.

### Settings and logs

```bash
mcp-gateway settings show                       # bearer ref, update check, log + metadata limits
mcp-gateway settings set --log-level DEBUG --no-update-check
mcp-gateway settings set --set introspect_interval=0
mcp-gateway settings set --server-instructions-max-bytes 4096
mcp-gateway settings set --tool-description-max-bytes 8192
mcp-gateway settings set --tool-description-max-bytes unlimited   # clear the global cap (default)
mcp-gateway settings set --bearer-token '${MCP_GATEWAY_TOKEN}'   # a ${ENV} reference, never the secret
mcp-gateway settings export -o bundle.json      # the full settings bundle
mcp-gateway settings export -o bundle.json --force   # replace an existing file
mcp-gateway settings export --full > bundle.json
mcp-gateway settings import bundle.json --yes    # overwrites stored overrides; --yes required
mcp-gateway settings import - --mode merge --yes # bundle from stdin, merge mode
mcp-gateway logs show                           # last 100 structured events
mcp-gateway logs show --limit 250 --level WARNING --event defaults_captured
mcp-gateway logs follow                         # stream new events until Ctrl-C
mcp-gateway logs follow --level ERROR
```

`settings set` updates only the keys given: repeatable `--set KEY=JSON` pairs
plus explicit flags (`--log-level`, `--log-max-bytes`, `--log-backup-count`,
`--introspect-interval`, `--update-check`/`--no-update-check`,
`--server-instructions-max-bytes`, `--tool-description-max-bytes`,
`--bearer-token`). Both metadata limits are integers from `1` to `1,048,576`;
`--tool-description-max-bytes unlimited` clears the gateway cap back to the
unbounded default (the value keyword is `unlimited` at this global level —
scoped values use `inherit` instead). `settings show` prints both globals
(`tool_description_max_bytes: unlimited` or the number). Unknown or
boot-time-only keys fail before any request. `settings export`
writes the bundle to stdout (or `-o FILE`); `settings import` is all-or-nothing
like the dashboard and requires `--yes`.

Writing an export to a file is deliberately safe: `-o` refuses to overwrite
any existing path unless `--force` is given, and even with `--force` only a
regular file is replaced (symlinks and special files are rejected, never
followed). The file is written atomically with private `0600` permissions and
parent directories are never created.

`logs show` reads `GET /admin/api/logs` and accepts `--limit` (1–500, default
100), `--level`, and `--event`; `logs follow` streams the live endpoint and
filters by `--level` only (the stream does not support `--event`). Unlike the
single JSON value other result-producing commands emit with `--json`,
`logs follow --json` is the deliberate streaming exception: one JSON event
per line (NDJSON), until interrupted.

## The daemon

On macOS the optional resident mode is a `launchd` LaunchAgent with label
**`com.void.mcp-gateway`**. `RunAtLoad` starts it at login and `KeepAlive`
restarts non-zero failures. The plist invokes an application-owned stable
wrapper rather than a checkout path, so moving or deleting a source clone does
not strand the job.

Application-owned lifecycle commands — explicit, never prompted:

```bash
# install or repair the resident service, then require /health and /ready
mcp-gateway service install

# loaded state plus gateway and backend-child RSS
mcp-gateway service status

# remove the service; config, logs, backups, and state are retained
mcp-gateway service uninstall --yes
```

A plain `mcp-gateway` run starts the gateway in the foreground on every
platform; there is no first-run install offer. `service uninstall` keeps user
data by default (`--keep-data`) — pass `--purge-data` to delete it too — and
requires `--yes`; the CLI never prompts, so scripts are safe in non-TTY
contexts. The legacy `--install-service`, `--service-status`, and
`--uninstall-service` flags remain as compatibility aliases.

You can restart the loaded job from the admin UI's **Gateway → Restart** button,
with `mcp-gateway restart`, or directly with:

```bash
launchctl kickstart -k gui/$(id -u)/com.void.mcp-gateway
```

### Resource footprint

Resident mode keeps one gateway process alive. Warm `stdio` backends can remain
child processes so repeated calls avoid session startup; that configurable
latency tradeoff, not service-management overhead, will usually dominate memory.
Disabled backends consume no session resources, and backends that do not need a
warm session can use stateless mode.

`mcp-gateway service status` measures the gateway and its complete descendant
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

For a public package installation:

```bash
mcp-gateway update
```

The command resolves and installs an exact PyPI version, verifies the new shim,
then uses that new code for one controlled service restart and requires both
`/health` and `/ready`. An activation failure triggers an automatic attempt to
reinstall and restart the old exact version. Use
`mcp-gateway update --version X.Y.Z` for deliberate rollback. No package swap
touches config or runtime state.

For a checkout deployment, the contributor recipe `just update` remains the
guarded equivalent from a clean `main` branch. It refuses dirty worktrees and
feature branches, fast-forwards source, installs the checkout, and performs the
same verified restart. `just` is a repository/contributor tool; users of a
packaged install update with `mcp-gateway update` instead.

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
are `missing`, plus a `backends` map with each enabled backend's connection
state: `connecting`, `up`, `down`, or `reconnecting` (with the last error and
the seconds until the next attempt). HTTP status is `200` when everything is
ready and `503` when an enabled backend has not mounted — so a monitor can
tell "up" from "up but degraded".

A backend that cannot be reached, or whose warm session dies, is never given
up on: its runner reconnects with backoff (one second, doubling to a minute)
until it succeeds or the backend is disabled. A network change such as a VPN
toggle therefore heals itself; the first call after the change may fail, and
the next one finds a fresh session.

`mcp-gateway check` runs the `/ready` probe from the terminal: it prints
whether the gateway is ready and which backends are missing, then exits `0`
only when readiness passes — a dead or degraded gateway exits nonzero with
the reason on stderr. `mcp-gateway status` reports per-backend liveness
through the admin API (`GET /admin/api/status`) and `mcp-gateway restart`
restarts the daemon (an honest no-op in foreground mode).

### The daemon is running an old version

Compare the installed command with the live health response:

```bash
mcp-gateway version
curl -s http://127.0.0.1:9100/health
```

The package version and the version reported by `/health` should match. A normal
`mcp-gateway update` verifies this after its controlled restart. If a manually
installed package was swapped without restarting, reinstall the resident service
with `mcp-gateway service install`; if the path still names a legacy checkout,
running `./install.sh` once migrates that installation.

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
mcp-gateway logs show        # last 100 structured events
mcp-gateway logs show --limit 250
mcp-gateway logs follow      # follow the active log (Ctrl-C to stop)
```

The CLI reads through the admin API (`GET /admin/api/logs`), so it works
against a running daemon wherever the admin API is reachable. Contributor
recipes `just logs` and `just logs-follow` still tail the raw file directly
from a checkout and are handy when the daemon itself is down.

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
| `/health` reports an older version than `mcp-gateway version` | Package was replaced without restarting the resident process | Run `mcp-gateway service install`; normal `mcp-gateway update` performs and verifies this automatically. |
| `/ready` returns 503; a backend shows red in the UI | An enabled backend cannot connect; the gateway keeps retrying with backoff | Read the error in `/ready` (`backends.<name>.error`) or `mcp-gateway check`; fix that backend's URL/command or secret. It mounts on the next attempt, other backends keep working. |
| Every call to the gateway returns 401 | A bearer token is set but the caller is not sending it | Register the endpoint with the credential as described in [security.md](security.md), or provide the token to the admin UI when prompted. |
| A client still shows the old tool name/description after an edit | The session has not re-listed the backend's tools yet | Reconnect the MCP server, start a new session, or trigger a tool use. Text edits are live in the gateway immediately, but a connected session can cache the old broadcast. |
| A tool you renamed can't be saved | Its name (or a deliberately identical description) collides with another tool | Pick a unique name, or turn on **auto-uniquify** in the ⚙ Gateway header for bulk renames. |
| A client sees a backend's tools twice | The backend is registered *both* directly and through the gateway | Remove the direct registration so the client sees only the gateway's rewritten version. |
| A benign-looking `reusing existing session … context mixing` line in the log | Framework informational message | Expected and harmless here; it is quieted to WARNING and does not indicate a problem. |

## Related

- [installation.md](installation.md) — install, upgrade, move, uninstall.
- [security.md](security.md) — the bearer token and what is (not) protected.
- [configuration.md](configuration.md) — the config file the UI writes.
