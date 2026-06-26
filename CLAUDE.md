# CLAUDE.md

## What this is
A thin local MCP proxy (FastMCP v3) between Claude Code and backend MCP servers.
Its job: rewrite every broadcast text a backend shows Claude — tool name/title/
description + each parameter name/description, hide params, disable tools — while
forwarding the real calls untouched. One loopback HTTP daemon, started at login,
shared by all Claude Code sessions.

## Stack
Python 3.12 · FastMCP 3.x · Pydantic · structlog · `uv`.

## How to run
- Dev: `uv run server.py` → `http://127.0.0.1:9100/mcp`, health at `/health`.
- Login: launchd LaunchAgent `com.void.mcp-gateway.plist` (see README "Run").

## How to verify
With the daemon up: `uv run verify_rename.py http://127.0.0.1:9100/mcp` — asserts
every configured rename/hide/disable + a real passthrough call.
`uv run config_loader.py config.toml` prints parsed backends + transform keys.

## Layout
- `config_loader.py` — Pydantic models + `config.toml` → proxy config + transforms; also `dump_toml`/`save` (admin writes config back).
- `server.py` — `create_proxy` → reconcile → `add_transform` → register admin → run http.
- `admin.py` — admin UI/API at `/admin`: introspect/defaults, edit overrides, in-process hot-reload, import/remove (restart).
- `admin.html` — single-file vanilla-JS admin page (no framework, no build).
- `config.toml` — backends + per-tool rewrites. **Runtime-managed by the admin** (UI saves regenerate it, comments lost). Defaults/backups live under `~/.local/state/mcp-gateway/`.

## Conventions
- `config.toml` holds only `${ENV}` refs + public endpoints (safe to commit);
  secret VALUES come from the environment (LaunchAgent `EnvironmentVariables`).
- A tool's `original` is the bare backend tool name; the loader adds the
  `<backend>_` prefix that FastMCP applies when there are 2+ backends.

## Gotchas (verified against FastMCP 3.4.2)
- Spec was written for FastMCP 2.x. v3 changes: `create_proxy()` (not
  `FastMCP.as_proxy`); transforms are `ToolTransform({name: ToolTransformConfig(
  ..., arguments={p: ArgTransformConfig(...)})})`; arg field is `arguments`,
  not `transform_args`.
- Single backend → bare tool names; 2+ backends → `<backend>_<tool>` prefix.
- Apply `add_transform` AFTER the startup reconcile, else every renamed tool is
  falsely flagged `override_no_match` (reconcile must see source names).
- FastMCP's INFO "reusing existing session … context mixing" line is benign
  here (fresh session per request; no per-request user secrets). Quieted via
  `FASTMCP_LOG_LEVEL=WARNING`. Revisit if a backend forwards per-user auth.
- One resident subprocess per stdio backend — calls do NOT re-spawn it.
- Hot-reload swaps the transform by mutating `mcp._transforms` (a list); tools/list
  applies transforms live per request, so the swap is instant. Remove the old
  transform before adding the new one or the list grows.
- Backend topology changes restart via launchd; the restart runs as a Starlette
  `BackgroundTask` with `subprocess.run` (reaps the child — no zombie). Don't
  detach a `sleep` shell for this (it zombies).
- launchd runs the venv python directly (not `uv run`) → one process, no uv
  supervisor. Recreate the venv with `uv sync` after cloning.
- Admin editing model: everything broadcast to Claude is editable (incl. the
  tool name); the *original* tool/param names (provider-facing) are read-only.
  Fields prefill with the effective value; the server stores an override only if
  it differs from the default (`_override_vs_default`), so config stays minimal.
  Broadcast names are validated `[A-Za-z0-9_-]`.
- `config_loader.save` is atomic + fsync (durable across crash); the UI debounces
  ~550ms and flushes on blur/page-close (`keepalive`) so no edit is lost.
