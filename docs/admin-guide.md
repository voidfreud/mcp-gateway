# Admin UI guide

The gateway serves a built-in web admin at **http://127.0.0.1:9100/admin** by
default. It is the main way to work with the gateway: import backends and
rewrite what each tool broadcasts to MCP clients. The page is
served by the same daemon that does the proxying. Loopback is the safe default;
an authenticated non-loopback deployment is supported when it is intentionally
configured — see [security.md](security.md#binding-beyond-loopback).

Backend text overrides **auto-save**. Virtual Tools use explicit **Save draft**
and **Save & activate** actions because their validate/test/activate lifecycle is
deliberately transactional. Everything is written to `config.toml`.

This guide walks the UI top to bottom. For what each change means in practice —
whether it takes effect instantly or needs a restart — see
[When changes take effect](#when-changes-take-effect) at the end.

## The same surface from the terminal

The web UI and the `mcp-gateway` CLI drive the same admin API
([api.md](api.md)) and write the same `config.toml`, so every dashboard action
has a scriptable command. The CLI is the automation surface; the full
reference — global flags, JSON input, and safety rules — lives in
[operations.md](operations.md#command-line-reference). Highlights:

| UI action | CLI command |
|-----------|-------------|
| Import a backend | `mcp-gateway backend add …` |
| Display name | `mcp-gateway backend display-name <name> …` |
| Backend detail / liveness | `mcp-gateway backend show <name>` / `mcp-gateway status` |
| Broadcasting toggle | `mcp-gateway backend enable\|disable <name>` |
| Master switch | `mcp-gateway backend enable-all\|disable-all` |
| Pin all tools | `mcp-gateway backend pin\|unpin <name>` |
| Warm session toggle | `mcp-gateway backend session <name> …` |
| Re-inspect | `mcp-gateway backend inspect <name>` |
| Rename… | `mcp-gateway backend rename <name> …` |
| Tool card edits | `mcp-gateway tool set <backend> <tool> …` |
| Run tool (mini-inspector) | `mcp-gateway tool run <backend> <tool> …` |
| Stale override migrate/discard | `mcp-gateway tool migrate …` / `tool discard …` |
| Resource edits | `mcp-gateway resource set …` |
| Prompt edits | `mcp-gateway prompt set …` |
| Server instructions | `mcp-gateway instructions set <backend> …` |
| Backend metadata limits | `mcp-gateway backend limits <name> …` |
| Virtual Tools editor | `mcp-gateway virtual create\|update\|validate\|test\|activate …` |
| Export / import | `mcp-gateway settings export\|import` |
| Gateway settings | `mcp-gateway settings show\|set` |
| Restart | `mcp-gateway restart` |
| Live log row | `mcp-gateway logs show\|follow` |

Choose by workflow: the UI is the interactive surface (visual editing, the
transactional Virtual Tools draft → validate → test → activate lifecycle),
while the CLI is the automation surface (`--json` for pipelines, `--yes` for
destructive actions, JSON-file inputs for complex payloads, safe non-TTY
use). The mapping is one-way complete — every dashboard action has a CLI
counterpart. The reverse is not claimed: the CLI additionally owns
automation-only controls that have no dashboard equivalent, such as `run`,
`version`, `update`, `service install|status|uninstall`, `check`, and
pipeline-friendly output. Where a capability exists in both, they operate on
the same admin API and write the same `config.toml`.

## The sidebar and status dots

The left pane lists every backend, plus **Gateway** and **Virtual Tools** items.

Each backend carries a **connection-status dot**, probed live through the running
proxy — the same path an MCP client uses to list tools:

- **Green** — the backend answered and its tools were listed. The dot shows the
  tool count.
- **Red** — the probe failed (the backend is down, misconfigured, or timed out).
  The failure is isolated: a red backend never stalls the page or the others.
- **Grey / disabled** — the backend is turned off, or not currently mounted.

The probe runs asynchronously, so a slow or dead backend marks only itself.

## Virtual Tools

Virtual Tools are a separate first-class category, not another backend import.
They are tools the gateway owns and serves together at the permanent
`/virtual/mcp` endpoint. The endpoint remains mounted with an empty tool list
when no definition is active.

The editor walks one definition through:

1. Public name, description, and input schema. The description has a live
   byte counter against its effective cap, plus a **Description limit
   (UTF-8 bytes)** control (`1`..`1,048,576`) with an **inherit gateway**
   checkbox — checked (the default) inherits the gateway-wide
   `tool_description_max_bytes` (unlimited when unset); unchecked saves a
   scoped number for this Virtual Tool only. An authored description over
   the effective cap is rejected on save — nothing you author is silently
   truncated.
2. Stable source members selected from the live backend catalog. The UI shows
   current effective names but stores backend IDs and original tool/parameter
   identities, so ordinary renames do not silently break the binding.
3. Dispatch: **all** (concurrent fan-out), **keyword** (local regex rules), or
   **llm** (external selection with explicit local fallback and data-egress
   acknowledgement).
4. Partial/strict failure policy and an aggregate byte budget. Text, images,
   audio, embedded resources, resource links, and structured output remain
   representable; omissions carry an explicit marker and metadata.

Save a draft first, use **Validate & resolve**, then normally **Test draft** with
a JSON argument object. Testing is optional because source tools may cost money
or have side effects; activation is still blocked if a source disappeared, a schema no
longer resolves, the live endpoint cannot dry-build, or LLM consent/key settings
are incomplete. Editing an active definition returns it to draft. Activate/disable
hot-reload the shared endpoint without removing it. Last-test and last-dispatch
badges are runtime-only and reset with the gateway process. Removing a referenced backend is rejected until its Virtual Tools are
updated, disabled/deleted, or moved to another source.

## Importing a backend

The **Import MCP** button adds a new backend. You give it a name and its
connection details — a URL for a remote (HTTP) server, or a command for a local
(stdio) one, plus any auth. The gateway connects to it, captures its original
tool list and instructions as a baseline, and mounts it live at
`/<name>/mcp`. Clients then register that independent endpoint using their own
supported configuration or CLI — the gateway does not register it for them
(detailed per-client steps follow in issue #285).

Adding a backend first validates and captures its baseline, then writes config.
In a running daemon with lifecycle mount hooks, the new endpoint is added live
without a restart. If that mount fails, the backend remains saved and the UI
reports the failure so you can repair it and restart or re-enable it. When
lifecycle hooks are unavailable, a launchd-managed daemon is restarted;
foreground/development mode saves the backend for the next real restart.
Reconnect or register the affected MCP client after its endpoint is available.

## Backend detail

Selecting a backend opens its detail view. The header holds the
backend-wide controls:

- **Broadcasting toggle (enabled).** Turns the whole backend on or off. When off,
  none of its tools are broadcast to MCP clients, its endpoint stops responding,
  and —
  for a local (stdio) backend — its process is shut down. Turning it back on
  remounts it live. No restart either way.
- **Pin (pin all tools).** Pins **every** tool this backend exposes to load
  upfront (eager) — see [Pinning](#pinning-claude-code-eager-loading) below.
  Applies even to tools you have not otherwise edited.
- **Display name.** A cosmetic label shown in the UI only. It does **not** change
  the endpoint URL, the config key, or any registered MCP-server name — all of
  those keep using the real name. Use this when you just want a friendlier label.
- **Rename…** A real identity change (see [Rename vs Display name](#rename-vs-display-name)).
- **Warm session toggle.** Keeps one persistent connection to the backend open
  instead of reconnecting on every call — noticeably faster for remote
  backends (measured 2–4× on live probes). If the held connection ever dies,
  the gateway detects it and reconnects by itself (at most one repair per 30
  seconds). Newly imported backends are warm by default; flipping the toggle
  reconnects the backend live, no restart.
- **Re-inspect.** Forces the gateway to reconnect to the backend and re-capture
  its live tool list, then reports how many tools were added or removed (`+N/−N`).
  Use it after you know the backend has changed (a new version, new tools).
- **Stale-override warning.** If the backend renamed its tools upstream (a new
  version, say), your saved edits for the old names stop applying — the detail
  view then shows an amber banner listing each stale entry with two choices:
  **migrate** it onto the tool's new name (your text carries over) or
  **discard** it. The sidebar marks such backends with ⚠.
- **Server instructions.** A box to edit the backend's server-level
  instructions — the always-loaded blurb an MCP client receives about the server
  at connect time (for example, "use this server whenever the user asks about a
  library"). Leaving it empty inherits the backend's original. An authored
  override must fit the backend's **effective** instructions cap — its own
  `server_instructions_max_bytes` when set, else the gateway-wide default
  (`2048`) — and a save over the cap is rejected with a clear error. A directly
  authored TOML `Backend.instructions` value is treated the same way: config
  load rejects it rather than loading it. Only the backend's **captured
  upstream** instructions can exceed the cap — such text is truncated at a
  UTF-8 character boundary at broadcast time, and an amber warning on the card
  reports the truncated byte count so you can see it.
- **Backend metadata limits.** A card with the backend's two per-backend
  caps: **Server instructions limit** and **Tool description limit** (UTF-8
  bytes, `1`..`1,048,576`). Each has an **inherit gateway** checkbox — checked
  (the default) inherits the gateway-wide value, unchecked saves a scoped
  number for this backend only. **Save backend limits** applies the change
  live (no restart); the tool cards and instructions box re-render against the
  new effective caps.

### Rename vs Display name

These look similar but do very different things:

- **Display name** is cosmetic. Nothing about routing changes.
- **Rename…** changes the backend's *route identity*: its endpoint URL
  (`/<name>/mcp`), its key in `config.toml`, its captured-defaults file, and its
  `gateway-<name>` registration name. Because the endpoint itself moves, the
  gateway hot-mounts the new route before responding, while the stable backend
  ID used by Virtual Tools stays unchanged. Re-register every client that still
  points to the old endpoint — the gateway does not manage client
  registrations, so update each client's configuration by hand. The UI shows
  the old and new endpoint and registration names.

### Registering endpoints in MCP clients

The gateway exposes **one MCP endpoint per backend** at `/<name>/mcp`, plus the
shared `/virtual/mcp` endpoint. The gateway does not register endpoints for
you: each MCP client has its own supported configuration or CLI for adding an
MCP server, and you add the endpoint there. The conventional MCP-server name is
`gateway-<name>` for a backend and `gateway-virtual` for the Virtual Tools
endpoint.

Two things apply to every client:

- If a bearer token is configured on the gateway, the registration must carry
  the credential (for example an `Authorization` header, or the client's
  token-environment-variable mechanism) or calls get rejected — see
  [security.md](security.md).
- After adding, changing, or removing a registration, some clients need a
  reload or a new session to notice it.

Follow the client's MCP server configuration documentation, registering
`http://127.0.0.1:9100/<name>/mcp` (or your configured host and port).

## Tool cards

Below the header, each of the backend's tools appears as a card. Everything an
MCP client receives about the tool is editable here. Every field is **prefilled
with its effective value** (your override if set, otherwise the backend's
original), so a field is never blank — clear it and it falls back to the
original.

Per tool you can edit:

- **Name** — the tool's broadcast name. Must be a valid identifier
  (`letters, digits, _ or -`) and unique within the backend. A rename that would
  collide with another tool's name is rejected with a clear message (see
  [Collision handling](#collision-handling)).
- **Title** — a human-readable display title.
- **Description** — the text an MCP client receives to decide when and how to
  use the tool.
  This is usually the most valuable thing to rewrite.
- **Description cap** — a per-tool **Description limit (UTF-8 bytes)** input
  (`1`..`1,048,576`). A number caps what is broadcast for this one tool,
  overriding the backend's and the gateway's caps; the **inherit backend**
  checkbox clears the per-tool value. The description textarea shows a live
  byte counter against the effective cap, and an amber warning appears when a
  captured upstream description exceeds it — such a description is truncated
  at a UTF-8 character boundary at broadcast time, never silently dropped.
- **Enabled** — turn the toggle off to drop the tool from the listing entirely.

Each parameter of the tool has its own row, where you can set:

- **Description** — what an MCP client receives about that parameter.
- **Inject value** — a fixed value the gateway sends to the backend on every call.
  When you set one, a **hidden** pill unlocks: with an injected value, hiding the
  parameter is safe even if the backend marks it *required*, because the client
  never sees it but the backend always receives the value. Hiding a required
  parameter *without* an injected value is rejected. The injected value must be
  a simple scalar (text, number, or true/false).

The parameter's **real, provider-facing name** (the name the gateway forwards to
the backend) is shown read-only — it cannot change. The tool's original name is
likewise read-only. You are editing what the client sees, not what the backend
receives; the gateway maps between them.

### Pinning (Claude Code eager loading)

Claude Code **defers** MCP tools by default: only their names load upfront, and
each tool's full description loads when Claude reaches for it. This makes idle
backends nearly free to keep connected.

A **📌 eager** checkbox on each tool pins it to load **upfront** instead, so
Claude Code has its full description available. Use it for the few tools you
want Claude Code to select reliably. There is also a **pin all tools** checkbox
in the backend header that pins every tool the backend exposes. Pinning takes
effect for Claude Code on a fresh session; other MCP clients may not use this
client-specific hint.

### Resource and prompt cards

If a backend broadcasts MCP **resources** or **prompts**, they appear in their
own sections below the tool cards (backends without any — most — show nothing).
The editing model is identical to tools: fields prefill with effective values,
edits save automatically, and only differences from the captured original are
stored.

- **Resources** (and resource templates) are keyed by their **URI**, which is
  the identity the MCP client reads by and is never rewritten. You can edit the
  display name (free-form text), title, and description, or switch the resource off —
  which both drops it from the listing and blocks reads through the gateway.
- **Prompts** can be **renamed** (same identifier rule as tools, unique within
  the backend's prompts): the MCP client sees the new name and the gateway
  forwards a `prompts/get` to the backend under the original. Title, description, and each
  **argument's description** are editable too. Argument *names* are read-only —
  a prompt call carries its arguments verbatim to the backend.

### Run tool (mini-inspector)

Each tool card has a **Run tool** control. It executes the tool through the
**live proxy** — the exact path MCP calls use, so your renames and injected
values apply — and shows the result, timing, and whether the call errored. Use it to
confirm a rewritten tool still works end to end.

### Collision handling

Two tools in the same backend can never share a broadcast name, and the gateway
also rejects a description you deliberately set identical to another tool's.
A save that would collide is refused with a clear error.

If you are renaming many tools at once and do not want to resolve every collision
by hand, turn on **auto-uniquify** in the ⚙ Gateway header. With it on, a
colliding save is retried once with a deterministic `_2` / `_3` suffix, and the
UI tells you the final name that shipped. The CLI equivalent is per-save:
`mcp-gateway tool set <backend> <tool> --name X --auto-uniquify` (without the
flag a colliding rename is rejected). (Auto-uniquify covers name collisions
only; a duplicate description still has to be fixed by hand.)

## Gateway overview (⚙ Gateway)

The **⚙ Gateway** item collects gateway-wide settings and information:

- **Version and update status.** The header shows the running gateway version.
  When the daily PyPI check finds a newer stable release, an amber badge shows
  the version and the explicit `mcp-gateway update` command; it never applies
  the update.
- **Stats and context footprint.** A read-only overview of every backend
  endpoint: its server-instructions byte count against the effective cap
  (`instructions N / LIMIT B`; the defaults are a 2048-byte instructions cap
  and an unlimited description cap). Each tool card and Virtual Tool card
  carries its description byte count against its effective cap, and an amber
  warning appears wherever a captured upstream blurb exceeds its cap, so
  broadcast-time truncation stays observable. Other clients may budget this
  context differently; the gateway enforces the configured caps, it does not
  claim a client-specific budget.
- **Export / import.** Your complete stored settings — every tool and parameter
  override, pins, server instructions, and display names — round-trip as a single
  JSON bundle. The **Export** button downloads it; **Import** applies one.
  (Each backend's detail bar also has a **Load fields** button that restores
  that one backend from an older per-field snapshot file.) Import validates every item the same way a single edit is
  validated and is all-or-nothing: if any item is invalid, nothing is applied.
  Backend topology (which backends exist, their URLs) is never part of an
  import — only the text overrides move.
- **Auto-uniquify toggle.** The name-collision escape hatch described under
  [Collision handling](#collision-handling).
- **Gateway settings.** The UI edits six boot-time settings: the **bearer token
  reference** (an `${ENV_VAR}` name, never the secret itself — see
  [security.md](security.md)), the **scheduled re-scan interval** (`0` = off),
  the **Daily update checks** privacy toggle, **log verbosity**, and the two
  gateway-wide **metadata limits** — **Server instructions limit (UTF-8
  bytes)** and **Tool description limit (UTF-8 bytes)**, the latter with an
  **unlimited** checkbox (checked by default: no cap). It displays log
  retention read-only; set `log_max_bytes` and `log_backup_count` in
  `config.toml` or through the [admin API](api.md#gateway-settings-payload).
  Saving asks a launchd-managed daemon to restart. In foreground/development
  mode it saves the values and reports that they apply on the next real restart.
  Changing the token invalidates existing client registrations until they carry
  the new value; re-register the endpoints in each client afterwards.
- **Restart.** Restarts the daemon on demand. (In foreground/dev mode, where
  there is no login service to restart, the UI says so honestly instead of
  hanging.)

## When changes take effect

Not every change reaches an already-running MCP client at the same speed. There
are three tiers:

1. **Text edits hot-reload instantly in the gateway.** Renaming a tool, editing a
   description, hiding or disabling a tool, editing server instructions, pinning —
   all of these apply to the running proxy immediately, with no restart and no
   dropped connection.

2. **…but connected clients can retain an old catalog.** Reconnect the MCP
   client before judging a catalog change. The verified Claude Code behavior is
   that an existing session keeps the previous broadcast text until it re-lists
   the backend's tools (on its next tool use, a manual reconnect, or a new
   session).

3. **Topology changes use the safest available lifecycle path.** Importing a
   backend hot-adds its route when the running daemon supplies lifecycle mount
   hooks; a failed mount is reported while the saved configuration remains for
   repair. A live rename hot-mounts the new route and rolls back if that mount
   fails. Removing a backend, or any topology change without live hooks, asks a
   launchd-managed daemon to restart; in foreground/development mode the change
   is saved for the next real restart. Reconnect or re-register every affected
   MCP client after its endpoint changes.

See [operations.md](operations.md) for the daemon lifecycle and
[configuration.md](configuration.md) for the file these edits write to.
