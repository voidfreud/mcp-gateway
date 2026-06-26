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

Calls are forwarded transparently — FastMCP reverse-maps renamed names/params
back to the originals, so the backend never sees your renames.

## Stack

Python 3.12 · FastMCP 3.x · Pydantic · structlog · `uv`.

## Install

```bash
cd ~/Developer/mine/mcp-gateway
uv sync                              # create .venv from uv.lock
cp config.example.toml config.toml   # then edit (a seeded config.toml ships too)
```

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
  For belt-and-suspenders, require a bearer token on the gateway and pass it from
  Claude Code with `-H "Authorization: Bearer ..."`.
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
**Revisit** only if you add a backend that forwards per-user auth from the
incoming request — then per-session isolation matters.

## Session strategy (efficiency)

`stateless` is parsed per backend but the MVP uses FastMCP's default
per-request sessions (one resident subprocess per stdio backend; HTTP backends
re-handshake per call). Shared-session reuse for stateless HTTP backends is a
tier-2 optimization — add it only if per-call latency bothers you.

## Out of scope (parked for later)

On-demand tool search / deferred loading, code mode, multi-backend composite
tools, resource/prompt text rewriting, LLM-generated descriptions, public
exposure. See the project spec's appendix.
