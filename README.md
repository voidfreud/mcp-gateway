# mcp-gateway

[![check](https://github.com/voidfreud/mcp-gateway/actions/workflows/check.yml/badge.svg)](https://github.com/voidfreud/mcp-gateway/actions/workflows/check.yml)

A thin local MCP proxy that sits between Claude Code and one or more backend MCP
servers and **rewrites every piece of text a backend broadcasts** — tool name,
title, description, and every parameter name + description — while forwarding the
actual tool calls untouched. You control what Claude reads about each tool
without forking the backend.

Runs as one persistent loopback HTTP daemon, started at login, shared by every
Claude Code session in every project. It is a thin wrapper over
[FastMCP](https://github.com/prefecthq/fastmcp) (v3) — mostly config, not code.

## Why this exists

MCP is largely unusable in Claude Code today, and the fault is upstream: server
authors ship tools with vague or missing names, empty `instructions`, and
parameter descriptions that tell the model nothing. The protocol is fine — the
surface authors expose over it is not, so most MCPs arrive as dead weight the
model can't reliably select or call.

And that waste is doubly a shame, because Claude Code **lazy-loads** tools: an
MCP costs almost nothing to keep connected until one of its tools is actually
called. They are nearly free to have around, and drastically underused for it.

mcp-gateway reclaims them. It lets us override the text a backend broadcasts —
names, `instructions`, parameter docs, and eager-load pinning — until a sloppy
server reads to the model like a well-designed one, without forking it. It's
tedious hand-work, but it turns otherwise-useless MCPs into ones that work. The
[`mcp-tool-design`](.claude/skills/mcp-tool-design/SKILL.md) skill in this repo
is the rubric we grade those overrides against; [`corpus/`](corpus/) holds the
research it was distilled from. As of 2026-07-03 every connected backend's
surface has been tuned through that pipeline
([#121](https://github.com/voidfreud/mcp-gateway/issues/121)), and a fresh-agent
cold-eval (routing intents from the turn-0 surface alone) scores the field
clean.

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
cd /path/to/mcp-gateway   # wherever the clone lives — install.sh handles the rest
uv sync                   # create .venv from uv.lock
```

On first run the server **auto-seeds `config.toml`** from the committed
`config.default.toml` (both backends passthrough). `config.toml` is the live,
admin-managed file — it's **gitignored** (the admin regenerates it on every save,
so it never shows up as a git change). Edit it in the admin UI, or by hand.
`config.example.toml` is the full annotated schema reference.

## Run

Foreground (dev):

```bash
uv run server.py              # serves /admin, /health, and one /<backend>/mcp endpoint per backend
```

At login (the intended mode) — a launchd LaunchAgent, installed via one script:

```bash
just install        # or: ./install.sh   (preview with ./install.sh --dry-run)
```

launchd needs absolute paths, and hardcoding the clone's location in the plist
meant a repo move silently orphaned the LaunchAgent while a ghost process kept
`/health` green ([#149](https://github.com/voidfreud/mcp-gateway/issues/149)).
So the plist points every path through **one stable symlink**,
`~/.local/opt/mcp-gateway` → the repo, and `install.sh` maintains it: it
repoints the symlink at wherever the repo actually lives, copies the plist to
`~/Library/LaunchAgents/`, and (re)loads the agent.

- **First time:** `uv sync`, then `just install`.
- **After moving the repo:** re-run `just install` from the new location —
  that's the only step.
- **Drift check:** `curl -s http://127.0.0.1:9100/health` names the daemon's
  *resolved* code path (`ok mcp-gateway <version> @ /path/to/repo`) — if it
  doesn't match where the repo lives now, you're talking to a ghost from the
  old clone.

> **Machine-specific paths:** the plist's symlink paths still embed this
> machine's home dir (launchd resolves no `~`), and
> `deploy/newsyslog-mcp-gateway.conf` hardcodes absolute paths too — on
> another machine/account edit those to match before installing.

Day-to-day launchctl, when you need it by hand:

```bash
launchctl print     gui/$(id -u)/com.void.mcp-gateway     # status
launchctl kickstart -k gui/$(id -u)/com.void.mcp-gateway  # restart
launchctl bootout   gui/$(id -u)/com.void.mcp-gateway     # stop + unload
```

`RunAtLoad` starts it at login; `KeepAlive` restarts it if it dies.

## Wire into Claude Code

The gateway exposes **one MCP endpoint per backend** (`/<backend>/mcp`) — each is
its own MCP server in Claude Code, so each backend gets its own ~2KB `instructions`
budget instead of all sharing one (see
[#29](https://github.com/voidfreud/mcp-gateway/issues/29)). Register each at user
scope, one line per backend:

```bash
claude mcp add --transport http gateway-gitnexus --scope user http://127.0.0.1:9100/gitnexus/mcp
claude mcp add --transport http gateway-deepwiki --scope user http://127.0.0.1:9100/deepwiki/mcp
claude mcp add --transport http gateway-context7 --scope user http://127.0.0.1:9100/context7/mcp
claude mcp list   # expect each: gateway-<backend> ... ✔ Connected
```

Any prefix works (`gateway-` keeps them grouped). Tools then resolve as
`mcp__gateway-deepwiki__ask_question`, etc. Stored in `~/.claude.json`, active
across all projects; reversible with `claude mcp remove gateway-<backend>`.

Or skip the terminal entirely: each backend's admin page has a **Register in
CC** button (scope selectable) that runs the same `claude mcp add` for you
([#45](https://github.com/voidfreud/mcp-gateway/issues/45)) — also offered as
a checkbox when importing a new backend, and re-offered after a hard rename.
Removing a backend best-effort runs `claude mcp remove` first. (Claude Code
sometimes needs a reload to notice either direction.) If a `bearer_token` is
configured (see Safety), the registration automatically carries the
`Authorization` header.

> **Heads-up on duplicates.** The gateway *proxies* backends. If a backend (e.g.
> `gitnexus`, `deepwiki`) is also registered directly with Claude Code, Claude
> sees both the direct tools and the gateway's rewritten versions. The intended
> end state is to register the per-backend gateway endpoints and **remove the
> direct registrations** for the backends it fronts, so Claude sees only your
> rewrites.

## Admin UI

A built-in web admin is served by the same daemon at **`http://127.0.0.1:9100/admin`**
(loopback only). It shows every backend in a left pane — each with a live
**connection-status dot** ([#23](https://github.com/voidfreud/mcp-gateway/issues/23):
probed asynchronously through the live proxy, so a down backend marks itself
red without stalling the page) — an **Import MCP** button,
and per-tool editing of **everything Claude Code sees** — broadcast name, title,
description, each parameter's description, disable a tool. Parameter *names* are
shown read-only and carry no blanket per-param hide button in the UI
([#128](https://github.com/voidfreud/mcp-gateway/issues/128); the `hide`/rename
levers still exist in config and pass through untouched). The backend's *real,
provider-facing* names (the original tool + parameter names the gateway forwards
to) are likewise **read-only** — those can't change.

**Injected param defaults ([#35](https://github.com/voidfreud/mcp-gateway/issues/35)).**
Each parameter row has an **inject value** field: a fixed scalar the gateway
sends to the backend on every call. Setting one unlocks a **hidden** pill —
with an injected value, hiding is safe even for a *required* param (Claude
never sees it; the backend always receives the value). Hiding a required param
*without* a default stays rejected.

**Tool lists stay fresh ([#43](https://github.com/voidfreud/mcp-gateway/issues/43)).**
The captured baseline auto-refreshes, event-driven: after every (re)connect
(daemon boot, hot-add, re-enable — catches backend upgrades), on a backend's
`tools/list_changed` push, and on opening the admin page (throttled, ~5 min).
A scheduled sweep exists but is off by default (`introspect_interval = 0`).
Overrides are stored as diffs by original name, so refreshes never clobber
edits — new tools appear un-overridden, removed ones drop. **Re-inspect**
forces a refresh and reports the `+N/−N` tool delta.

**Hard rename ([#44](https://github.com/voidfreud/mcp-gateway/issues/44)).**
**Rename…** in a backend's header changes its *real identity* — endpoint URL,
config key, defaults file, `gateway-<name>` registration — with a restart and
an explicit prompt to re-register in Claude Code (one-click, with old-name
cleanup). For a cosmetic label, use **Display name** instead
([#42](https://github.com/voidfreud/mcp-gateway/issues/42)).

Every editable field is **prefilled with its effective value** (your override if
set, else the backend default), so it's never blank — clear it and it falls back
to the default. Only values that actually differ from the default are stored, so
`config.toml` stays minimal. Broadcast names are validated as MCP-safe
identifiers (`[A-Za-z0-9_-]`) **and must be unique** — a rename that would collide
with another tool's broadcast name (or a description set identical to another's)
is rejected with a clear error, so two tools can never share a name. (Renaming
in bulk? An opt-in **auto-uniquify** toggle in the gateway header retries a
colliding save once with a deterministic `_2`/`_3` suffix and tells you the
final name — [#22](https://github.com/voidfreud/mcp-gateway/issues/22).) Each
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
Because each backend is its **own** endpoint/MCP server, the gateway captures each
backend's original instructions and hands them back **on that backend's own
endpoint** — so each gets Claude Code's full ~2KB budget, never a shared one (see
[#29](https://github.com/voidfreud/mcp-gateway/issues/29)). Each backend's detail
has a **Server instructions** box to edit (or add, where the server sends none);
empty = inherit the original. The **⚙ Gateway** item shows a read-only overview of
every endpoint and how much of each 2KB budget its instructions use. Edits
hot-reload in-process and are read fresh on each connect.

**Durability.** Saves write `config.toml` atomically with `fsync` (survives an
unexpected crash/power-loss, never a partial file), debounced ~550 ms to avoid
overload, and flushed on field-blur and on page-close — so no edit is lost.

**Settings export / import
([#136](https://github.com/voidfreud/mcp-gateway/issues/136)).** The complete
stored settings — per-backend instructions, every tool/param override, pins,
display names — round-trip as one JSON bundle: `GET /admin/api/export`
(`?full=true` adds captured defaults) and `POST /admin/api/import`
(`{"mode": "merge"|"replace", "settings": …}` — validated like single saves,
per-item errors, all-or-nothing, hot-reloads affected backends; backend
topology is never imported). The ⚙ Gateway panel's **Save/Load all fields**
buttons are the browser-side equivalent. Scripted `PUT /admin/api/override`
uses **merge semantics** ([#139](https://github.com/voidfreud/mcp-gateway/issues/139)):
a key absent from the payload preserves the stored override instead of
resetting it — a partial scripted write can't clobber a UI toggle.

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
transport = "http"                       # http | streamable-http | sse | stdio
url = "https://your-exa-endpoint/mcp"
auth_header = "Authorization"            # optional; both or neither
auth_value  = "Bearer ${EXA_TOKEN}"      # ${ENV} resolved at startup
# richer auth (#6), all optional:
# headers = { "X-Client-Id" = "${MY_ID}" }   # extra static headers (${ENV} ok)
# auth = "oauth"                             # OAuth-protected MCP (FastMCP runs
#                                            # the browser flow on first connect)
# headers_helper = ["my-sso", "print-headers"]  # list = no-shell (safe); a
#                                            # string form runs via the shell
#                                            # (for $()/pipes) with full shell
#                                            # privilege. Runs ONCE at mount/
#                                            # introspect — good for uptime-valid
#                                            # tokens, not mid-session rotation.
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
value either via the environment or, since the daemon's launchd env is minimal,
via the gateway-scoped secrets file `~/.config/mcp-gateway/secrets.env`
(`KEY=VALUE` lines; path overridable with `MCP_GATEWAY_SECRETS`). The environment
wins on conflict. Put ONLY the tokens the gateway's backends need there — never
point it at a global key store: secrets from this file are kept out of
`os.environ`, so stdio backend subprocesses can't read them, but every backend's
auth ref resolves from the same file. `config.toml` therefore holds only env
*references* and public endpoints, and is safe to commit.

**Bare tool names.** Each backend is proxied on its **own** endpoint/MCP server,
so its tools keep their bare names (`ask_question`) — the backend is namespaced by
its endpoint and Claude-Code server registration (`mcp__gateway-deepwiki__…`), not
by a tool-name prefix. The `original` field in `config.toml` is the bare backend
tool name. On startup the gateway lists each backend's live tools and logs an
`override_no_match` warning for any `original` that backend doesn't expose
(catches typos).

## Safety

- Binds `127.0.0.1` only — never `0.0.0.0`. Nothing off-machine can reach it.
- Any local process could hit the port; on a single-user Mac this is a non-issue.
- **Optional bearer token
  ([#26](https://github.com/voidfreud/mcp-gateway/issues/26)).** Defense-in-depth
  against curious or compromised local processes hitting the loopback port: set
  `bearer_token = "${MCP_GATEWAY_TOKEN}"` in `config.toml` (an `${ENV}` ref like
  every secret, resolved once at startup) and every backend MCP endpoint requires
  `Authorization: Bearer <token>` — anything else gets a 401. The **admin API is
  gated too** (an open one would hand the same local process config writes and
  tool execution via `/admin/api/run`); only `/health`, `/ready` and the bare
  `GET /admin` page shell stay open — the UI prompts for the token on first 401
  and keeps it in localStorage. Register each backend with the header:

  ```bash
  claude mcp add --transport http --header "Authorization: Bearer ${MCP_GATEWAY_TOKEN}" gateway-<name> http://127.0.0.1:9100/<name>/mcp
  ```
- Keep dangerous backend tools disabled via `enabled = false`.

## Operations

- **Logs:** structured JSON via structlog to `~/.local/state/mcp-gateway/gateway.log`
  (`gateway_built`, `reconcile_done`, `tool_call` with latency, errors). The file
  **rotates** (5 MB × 5 via `RotatingFileHandler`), and library logging
  (uvicorn, fastmcp) at **WARNING and above** is routed into it too (the root
  level is WARNING, so library INFO is dropped) — so launchd's `out.log` /
  `err.log` only ever catch rare pre-init or hard-crash text and stay bounded. For a
  belt-and-suspenders cap on those two, an optional `newsyslog` config ships at
  `deploy/newsyslog-mcp-gateway.conf` (install per the comments in that file).
- **Health:** `curl -s http://127.0.0.1:9100/health` →
  `ok mcp-gateway <version> @ /path/to/repo`. The path is the daemon's
  *resolved* code location — compare it against where the repo lives to catch
  a ghost process left over from a repo move
  ([#149](https://github.com/voidfreud/mcp-gateway/issues/149)).
- **Restart:** `launchctl kickstart -k gui/$(id -u)/com.void.mcp-gateway`.
- **Verify rewrites end-to-end:** with the gateway running,
  `uv run verify_rename.py http://127.0.0.1:9100` checks every backend endpoint
  (bare tool names exposed, instructions within the 2KB budget) and makes a real
  passthrough call.
- **CI:** GitHub Actions runs the pure-logic gate (`just check`: ruff + format
  + pytest/Hypothesis + import smoke) on every PR and push to main
  ([ADR-0003](docs/decisions/0003-check-only-ci.md)). The live gate
  (`just verify`) stays local by design — it needs the running daemon and real
  backends ([ADR-0001](docs/decisions/0001-no-github-ci.md), as amended).

### A benign log line

FastMCP may emit `Proxy detected connected client - reusing existing session …
context mixing in concurrent scenarios` at INFO. For this gateway it is benign:
the top-level factory creates a fresh session per request, backends here carry no
per-request user secrets, and a stdio backend stays one resident subprocess by
design. It is quieted to WARNING in the daemon (`FASTMCP_LOG_LEVEL=WARNING`).
> Per-session-isolation tripwire tracked at [#25](https://github.com/voidfreud/mcp-gateway/issues/25).

## Session strategy (per backend)

Each backend's `stateless` flag drives its session independently. A
`stateless = false` backend (e.g. stdio `gitnexus`) is built from **one persistent
connection reused for the daemon's lifetime** — warm, no per-call respawn. A
`stateless = true` backend (e.g. remote HTTP `deepwiki`/`context7`) uses a fresh
per-request session. Because every backend is its own endpoint, a backend that is
down only fails its own endpoint — it never blocks the others or the daemon from
booting (see [#9](https://github.com/voidfreud/mcp-gateway/issues/9)).

## Out of scope (parked for later)

> Migrated to GitHub Issues — 2026-06-28. [#12 tool search / deferred loading](https://github.com/voidfreud/mcp-gateway/issues/12) · [#13 code mode](https://github.com/voidfreud/mcp-gateway/issues/13) · [#14 multi-backend composite tools](https://github.com/voidfreud/mcp-gateway/issues/14) · [#15 resource / prompt text rewriting](https://github.com/voidfreud/mcp-gateway/issues/15) · [#17 LLM-generated descriptions](https://github.com/voidfreud/mcp-gateway/issues/17) · [#18 public exposure + bearer auth](https://github.com/voidfreud/mcp-gateway/issues/18). See the project spec's appendix for the longer-form rationale.
