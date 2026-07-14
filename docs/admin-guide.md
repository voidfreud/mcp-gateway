# Admin UI guide

The gateway serves a built-in web admin at **http://127.0.0.1:9100/admin**. It is
the main way to work with the gateway: import backends, rewrite what each tool
broadcasts to Claude Code, and register the results. The page is served by the
same daemon that does the proxying, and is reachable only from your own machine
(loopback).

Everything you change **auto-saves** — there are no save buttons. Edits are
written to `config.toml` (debounced by about half a second, and flushed when a
field loses focus or you close the page), so nothing is lost.

This guide walks the UI top to bottom. For what each change means in practice —
whether it takes effect instantly or needs a restart — see
[When changes take effect](#when-changes-take-effect) at the end.

## The sidebar and status dots

The left pane lists every backend, plus a **⚙ Gateway** item at the top for
gateway-wide settings.

Each backend carries a **connection-status dot**, probed live through the running
proxy — the same path Claude Code uses to list tools:

- **Green** — the backend answered and its tools were listed. The dot shows the
  tool count.
- **Red** — the probe failed (the backend is down, misconfigured, or timed out).
  The failure is isolated: a red backend never stalls the page or the others.
- **Grey / disabled** — the backend is turned off, or not currently mounted.

The probe runs asynchronously, so a slow or dead backend marks only itself.

## Importing a backend

The **Import MCP** button adds a new backend. You give it a name and its
connection details — a URL for a remote (HTTP) server, or a command for a local
(stdio) one, plus any auth. The gateway connects to it, captures its original
tool list and instructions as a baseline, and mounts it live at
`/<name>/mcp`. Importing offers a checkbox to **register it in Claude Code** at
the same time.

Adding a backend rebuilds connections, so it writes config and restarts the
daemon (Claude Code reconnects automatically).

## Backend detail

Selecting a backend opens its detail view. The header holds the
backend-wide controls:

- **Broadcasting toggle (enabled).** Turns the whole backend on or off. When off,
  none of its tools are broadcast to Claude, its endpoint stops responding, and —
  for a local (stdio) backend — its process is shut down. Turning it back on
  remounts it live. No restart either way.
- **Pin (pin all tools).** Pins **every** tool this backend exposes to load
  upfront (eager) — see [Pinning](#pinning-eager-loading) below. Applies even to
  tools you have not otherwise edited.
- **Display name.** A cosmetic label shown in the UI only. It does **not** change
  the endpoint URL, the config key, or the Claude Code registration — all of
  those keep using the real name. Use this when you just want a friendlier label.
- **Rename…** A real identity change (see [Rename vs Display name](#rename-vs-display-name)).
- **Register in CC.** Registers this backend's gateway endpoint with Claude Code
  in one click, with a selectable scope (see [Registering in Claude Code](#registering-in-claude-code)).
  A small chip next to the button shows whether the backend is currently
  **registered in Claude Code** (checked via the `claude` command and cached for
  a minute; the chip disappears if the command isn't installed).
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
  instructions — the always-loaded blurb Claude reads about the whole server at
  connect time (for example, "use this server whenever the user asks about a
  library"). Leaving it empty inherits the backend's original. A counter shows
  how much of Claude Code's ~2KB per-server budget the text uses; the gateway
  rejects instructions that exceed it.

### Rename vs Display name

These look similar but do very different things:

- **Display name** is cosmetic. Nothing about routing changes.
- **Rename…** changes the backend's *real identity*: its endpoint URL
  (`/<name>/mcp`), its key in `config.toml`, its captured-defaults file, and its
  `gateway-<name>` registration in Claude Code. Because the endpoint itself
  moves, a rename restarts the daemon and then prompts you to re-register in
  Claude Code (one click, and it cleans up the old registration for you). The
  UI tells you the exact old and new endpoint and registration names.

### Registering in Claude Code

The gateway exposes **one MCP endpoint per backend**. To use a backend from
Claude Code, that endpoint has to be registered as an MCP server. **Register in
CC** does this for you — it runs the same `claude mcp add` you would type by
hand, registering the endpoint as `gateway-<name>`.

- You choose the **scope** — `local` (this project), `user` (all your projects),
  or `project` (shared via the project's config).
- If a bearer token is configured on the gateway, the registration automatically
  includes the `Authorization` header, so calls do not get rejected.
- Removing a backend best-effort de-registers it from Claude Code first.
- Claude Code sometimes needs a reload to notice a registration change in either
  direction.

The `claude` command must be available to the daemon for this to work; if it is
not, the button reports that clearly. You can always register by hand instead —
see [operations.md](operations.md) and the README.

## Tool cards

Below the header, each of the backend's tools appears as a card. Everything
Claude Code reads about the tool is editable here. Every field is **prefilled
with its effective value** (your override if set, otherwise the backend's
original), so a field is never blank — clear it and it falls back to the
original.

Per tool you can edit:

- **Name** — the tool's broadcast name. Must be a valid identifier
  (`letters, digits, _ or -`) and unique within the backend. A rename that would
  collide with another tool's name is rejected with a clear message (see
  [Collision handling](#collision-handling)).
- **Title** — a human-readable display title.
- **Description** — the text Claude reads to decide when and how to use the tool.
  This is usually the most valuable thing to rewrite.
- **Enabled** — turn the toggle off to drop the tool from the listing entirely.

Each parameter of the tool has its own row, where you can set:

- **Description** — what Claude reads about that parameter.
- **Inject value** — a fixed value the gateway sends to the backend on every call.
  When you set one, a **hidden** pill unlocks: with an injected value, hiding the
  parameter is safe even if the backend marks it *required*, because Claude never
  sees it but the backend always receives the value. Hiding a required parameter
  *without* an injected value is rejected. The injected value must be a simple
  scalar (text, number, or true/false).

The parameter's **real, provider-facing name** (the name the gateway forwards to
the backend) is shown read-only — it cannot change. The tool's original name is
likewise read-only. You are editing what Claude sees, not what the backend
receives; the gateway maps between them.

### Pinning (eager loading)

By default Claude Code **defers** MCP tools: only their names load upfront, and
each tool's full description loads when Claude reaches for it. This makes idle
backends nearly free to keep connected.

A **📌 eager** checkbox on each tool pins it to load **upfront** instead, so
Claude always has its full description available. Use it for the few tools you
want Claude to select reliably. There is also a **pin all tools** checkbox in the
backend header that pins every tool the backend exposes. Pinning takes effect
for Claude on a fresh session.

### Resource and prompt cards

If a backend broadcasts MCP **resources** or **prompts**, they appear in their
own sections below the tool cards (backends without any — most — show nothing).
The editing model is identical to tools: fields prefill with effective values,
edits save automatically, and only differences from the captured original are
stored.

- **Resources** (and resource templates) are keyed by their **URI**, which is
  the identity Claude reads by and is never rewritten. You can edit the display
  name (free-form text), title, and description, or switch the resource off —
  which both drops it from the listing and blocks reads through the gateway.
- **Prompts** can be **renamed** (same identifier rule as tools, unique within
  the backend's prompts): Claude sees the new name and the gateway forwards a
  `prompts/get` to the backend under the original. Title, description, and each
  **argument's description** are editable too. Argument *names* are read-only —
  a prompt call carries its arguments verbatim to the backend.

### Run tool (mini-inspector)

Each tool card has a **Run tool** control. It executes the tool through the
**live proxy** — the exact path Claude uses, so your renames and injected values
apply — and shows the result, timing, and whether the call errored. Use it to
confirm a rewritten tool still works end to end.

### Collision handling

Two tools in the same backend can never share a broadcast name, and the gateway
also rejects a description you deliberately set identical to another tool's.
A save that would collide is refused with a clear error.

If you are renaming many tools at once and do not want to resolve every collision
by hand, turn on **auto-uniquify** in the ⚙ Gateway header. With it on, a
colliding save is retried once with a deterministic `_2` / `_3` suffix, and the
UI tells you the final name that shipped. (Auto-uniquify covers name collisions
only; a duplicate description still has to be fixed by hand.)

## Gateway overview (⚙ Gateway)

The **⚙ Gateway** item collects gateway-wide settings and information:

- **Stats and context footprint.** A read-only overview of every backend
  endpoint and how much of each one's ~2KB instructions budget its server
  instructions use — so you can see, at a glance, the always-loaded context each
  backend costs Claude.
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
- **Gateway settings.** Edit the two config-file-only knobs without touching a
  file: the **bearer token reference** (an `${ENV_VAR}` name, never the secret
  itself — see [security.md](security.md)) and the **scheduled re-scan
  interval** (0 = off). These are read at daemon start, so saving offers a
  restart. Changing the token breaks existing Claude Code registrations until
  they carry the new one — the UI offers **Re-register all** right after such a
  save.
- **Re-register all in CC.** One click to refresh every enabled backend's
  Claude Code registration (remove + add each, sequentially, with a per-backend
  result). Use after changing the bearer token or the port.
- **Restart.** Restarts the daemon on demand. (In foreground/dev mode, where
  there is no login service to restart, the UI says so honestly instead of
  hanging.)

## When changes take effect

Not every change reaches an already-running Claude Code session at the same
speed. There are three tiers:

1. **Text edits hot-reload instantly in the gateway.** Renaming a tool, editing a
   description, hiding or disabling a tool, editing server instructions, pinning —
   all of these apply to the running proxy immediately, with no restart and no
   dropped connection.

2. **…but Claude Code shows the old text until it reconnects.** A Claude Code
   session that is already connected keeps the *previous* broadcast text until it
   re-lists the backend's tools — which happens on its next tool use, a manual
   reconnect of the MCP server, or a new session. **If you are checking whether an
   edit took, reconnect first, or you will be reading stale text.**

3. **Backend/topology changes need a restart.** Importing, removing, renaming, or
   changing a backend's URL or auth rebuilds connections, so they write config and
   restart the daemon. Claude Code reconnects automatically.

See [operations.md](operations.md) for the daemon lifecycle and
[configuration.md](configuration.md) for the file these edits write to.
