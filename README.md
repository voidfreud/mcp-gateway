# mcp-gateway

A thin local MCP proxy that sits between Claude Code and one or more backend MCP
servers and **rewrites every piece of text a backend broadcasts** — tool name,
title, description, and every parameter name + description — while forwarding the
actual tool calls untouched. You control what Claude reads about each tool
without forking the backend.

Runs as one persistent loopback HTTP daemon, started at login, shared by every
Claude Code session in every project. It is a thin wrapper over
[FastMCP](https://github.com/prefecthq/fastmcp) (v3) — mostly config, not code.

## What it does

Per tool, from `config.toml`, it can:

1. Rename the tool, retitle it, rewrite its description.
2. Rename any parameter and rewrite its description.
3. Hide a parameter from the schema.
4. Drop a whole tool from the listing.
5. **Pin a tool (or a whole backend) to load _eagerly_** — upfront, instead of
   deferred by Claude Code's tool search (sets `_meta["anthropic/alwaysLoad"]`).

Calls are forwarded transparently — FastMCP reverse-maps renamed names/params
back to the originals, so the backend never sees your renames.

## Stack

Python 3.12 · FastMCP 3.x · Pydantic · structlog · `uv`.

## Install

```bash
cd ~/Developer/mine/mcp-gateway
uv sync     # create .venv from uv.lock
```

On first run the server **auto-seeds `config.toml`** from the committed
`config.default.toml` (both backends passthrough). `config.toml` is the live,
admin-managed file — it's **gitignored** (the admin regenerates it on every save,
so it never shows up as a git change). Edit it in the admin UI, or by hand.
`config.example.toml` is the full annotated schema reference.

## Run

Foreground (dev):

```bash
uv run server.py              # serves http://127.0.0.1:9100/mcp ; health at /health
```

At login (the intended mode) — a launchd LaunchAgent:

```bash
cp com.void.mcp-gateway.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.void.mcp-gateway.plist
launchctl print     gui/$(id -u)/com.void.mcp-gateway     # status
launchctl kickstart -k gui/$(id -u)/com.void.mcp-gateway  # restart
launchctl bootout   gui/$(id -u)/com.void.mcp-gateway     # stop + unload
```

`RunAtLoad` starts it at login; `KeepAlive` restarts it if it dies.

## Wire into Claude Code

Register once at user scope so every project hits the same shared gateway:

```bash
claude mcp add --transport http gateway --scope user http://127.0.0.1:9100/mcp
claude mcp list               # expect: gateway ... ✔ Connected
```

Stored in `~/.claude.json` under `mcpServers`, active across all projects.
Reversible with `claude mcp remove gateway`.

> **Heads-up on duplicates.** The gateway *proxies* backends. If you also have a
> backend (e.g. `gitnexus`, `deepwiki`) registered directly with Claude Code,
> Claude will see both the direct tools and the gateway's rewritten versions.
> The intended end state is to register the gateway and **remove the direct
> registrations** for the backends it fronts, so Claude sees only your rewrites.

## Admin UI

A built-in web admin is served by the same daemon at **`http://127.0.0.1:9100/admin`**
(loopback only). It shows every backend in a left pane, an **Import MCP** button,
and per-tool editing of **everything Claude Code sees** — broadcast name, title,
description, each parameter's name + description, hide a param, disable a tool.
The backend's *real, provider-facing* names (the original tool + parameter names
the gateway forwards to) are shown **read-only** — those can't change.

Every editable field is **prefilled with its effective value** (your override if
set, else the backend default), so it's never blank — clear it and it falls back
to the default. Only values that actually differ from the default are stored, so
`config.toml` stays minimal. Broadcast names are validated as MCP-safe
identifiers (`[A-Za-z0-9_-]`) **and must be unique** — a rename that would collide
with another tool's broadcast name (or a description set identical to another's)
is rejected with a clear error, so two tools can never share a name. Each
backend's original broadcast is captured once as a baseline
(`~/.local/state/mcp-gateway/defaults/<backend>.json`) for **reset to default**;
`config.toml` is snapshotted to `backups/` on every save.

**Eager loading (pin).** By default Claude Code *defers* MCP tools — only their
names load upfront; descriptions load on demand. A **📌 eager** checkbox on each
tool (and a **"pin all tools"** checkbox per backend) pins it to load **upfront**
instead, by setting the tool's `_meta["anthropic/alwaysLoad"] = true`. Use it for
the few tools you want Claude to reach for reliably. (Per-backend pinning applies
to every tool the backend exposes, including ones you haven't otherwise edited.)
Pinning hot-reloads in-process. Takes effect for Claude on a fresh session.

**Server instructions.** An MCP server can send a server-level `instructions`
blurb at `initialize` — always-loaded context Claude reads upfront (e.g. "use
this server whenever the user asks about a library"). A bare proxy **drops** it.
The gateway captures each backend's original instructions and, by default,
**composes** them back into its own `instructions` (one `# <backend>` section per
contributing server; a single contributor gets no header). The **⚙ Gateway** item
in the admin lets you see what's broadcast now and set a **full manual override**;
each backend's detail has a **Server instructions** box to edit (or add, where the
server sends none) its section. Empty = inherit the original / auto-compose.
Composition hot-reloads in-process and is read fresh on each connect.

**Durability.** Saves write `config.toml` atomically with `fsync` (survives an
unexpected crash/power-loss, never a partial file), debounced ~550 ms to avoid
overload, and flushed on field-blur and on page-close — so no edit is lost.

Edits **auto-save** (no buttons) and apply with no reload latency:

- **Text edits** (rename/description/hide/disable) hot-reload the proxy's
  transforms **in-process** — instant, no restart, no client disconnect. The
  change is live in the gateway immediately; an already-connected Claude session
  picks it up on its next tool list / reconnect / new session.
- **Backend changes** (import/remove/url/auth) rebuild the connection, so they
  write config and restart the daemon (Claude auto-reconnects).

`config.toml` is **runtime-managed by the admin** — UI saves regenerate it
(comments are not preserved; `config.example.toml` keeps the annotated reference).
Hand-editing it is still fine.

## Config

`config.toml` is the source of truth (see `config.example.toml` for the full
annotated schema). Shape:

```toml
host = "127.0.0.1"
port = 9100
log_file = "~/.local/state/mcp-gateway/gateway.log"

[[backends]]
name = "exa"
transport = "http"                       # "http" or "stdio"
url = "https://your-exa-endpoint/mcp"
auth_header = "Authorization"            # optional; both or neither
auth_value  = "Bearer ${EXA_TOKEN}"      # ${ENV} resolved at startup
stateless = true

  [[backends.tools]]
  original = "web_search_exa"            # the backend's own tool name
  name = "web_search"
  description = "What Claude should read."
  enabled = true                         # false -> drop the tool

    [[backends.tools.params]]
    original = "query"
    description = "What Claude should read for this param."

    [[backends.tools.params]]
    original = "internal_flag"
    hide = true
```

**Secrets** are never written in `config.toml` — put `${ENV_VAR}` and supply the
value via the environment (the LaunchAgent's `EnvironmentVariables`, or a run
shim). `config.toml` therefore holds only env *references* and public endpoints,
and is safe to commit.

**Tool-name prefixing.** With **one** backend, tools keep their bare name
(`ask_question`). With **two or more**, FastMCP prefixes them with the backend
name (`deepwiki_ask_question`). The `original` field in `config.toml` is always
the bare backend name — the loader computes the prefix. On startup the gateway
lists live tools and logs an `override_no_match` warning for any `original` that
no backend exposes (catches typos).

## Safety

- Binds `127.0.0.1` only — never `0.0.0.0`. Nothing off-machine can reach it.
- Any local process could hit the port; on a single-user Mac this is a non-issue.
  > Optional bearer-token requirement on the loopback is tracked at [#26](https://github.com/voidfreud/mcp-gateway/issues/26).
- Keep dangerous backend tools disabled via `enabled = false`.

## Operations

- **Logs:** structured JSON via structlog to `~/.local/state/mcp-gateway/gateway.log`
  (`gateway_built`, `reconcile_done`, `tool_call` with latency, errors).
  launchd stdout/stderr go to `out.log` / `err.log` in the same dir.
- **Health:** `curl -s http://127.0.0.1:9100/health` → `ok`.
- **Restart:** `launchctl kickstart -k gui/$(id -u)/com.void.mcp-gateway`.
- **Verify rewrites end-to-end:** with the gateway running,
  `uv run verify_rename.py http://127.0.0.1:9100/mcp` asserts every configured
  rename/hide/disable and makes a real passthrough call.

### A benign log line

FastMCP may emit `Proxy detected connected client - reusing existing session …
context mixing in concurrent scenarios` at INFO. For this gateway it is benign:
the top-level factory creates a fresh session per request, backends here carry no
per-request user secrets, and a stdio backend stays one resident subprocess by
design. It is quieted to WARNING in the daemon (`FASTMCP_LOG_LEVEL=WARNING`).
> Per-session-isolation tripwire tracked at [#25](https://github.com/voidfreud/mcp-gateway/issues/25).

## Session strategy (efficiency)

`stateless` is parsed per backend but the MVP uses FastMCP's default
per-request sessions (one resident subprocess per stdio backend; HTTP backends
re-handshake per call). Shared-session reuse for stateless HTTP backends is a
tier-2 optimization — add it only if per-call latency bothers you.

## Out of scope (parked for later)

> Migrated to GitHub Issues — 2026-06-28. See [#12](https://github.com/voidfreud/mcp-gateway/issues/12), [#13](https://github.com/voidfreud/mcp-gateway/issues/13), [#14](https://github.com/voidfreud/mcp-gateway/issues/14), [#15](https://github.com/voidfreud/mcp-gateway/issues/15), [#17](https://github.com/voidfreud/mcp-gateway/issues/17), [#18](https://github.com/voidfreud/mcp-gateway/issues/18).
