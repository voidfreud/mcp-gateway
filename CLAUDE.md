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
- Dev: `uv run server.py` → one `/<backend>/mcp` endpoint per backend, plus `/admin` + `/health`.
- Login: launchd LaunchAgent `com.void.mcp-gateway.plist` (see README "Run").

## How to verify
With the daemon up: `uv run verify_rename.py http://127.0.0.1:9100` — checks every
backend endpoint (bare names, instructions <=2KB) + a real passthrough call.
`uv run config_loader.py config.toml` prints parsed backends + transform keys.

## Layout
- `config_loader.py` — Pydantic models + `config.toml` → proxy config + transforms; also `dump_toml`/`save` (admin writes config back).
- `server.py` — one single-backend `create_proxy` per backend, each mounted at `/<backend>/mcp` under a parent Starlette (+ `/admin` + `/health`); lifespans composed via `AsyncExitStack`.
- `admin.py` — admin UI/API at `/admin`: introspect/defaults, edit overrides, in-process hot-reload, import/remove (restart).
- `admin.html` — single-file vanilla-JS admin page (no framework, no build).
- `config.toml` — the LIVE, admin-managed config (**gitignored** — regenerated on
  every UI save). Auto-seeded from `config.default.toml` on first run (`config_loader.ensure_config`).
- `config.default.toml` — committed runnable seed (both backends passthrough).
  `config.example.toml` — full annotated schema reference. Defaults/backups for
  the runtime live under `~/.local/state/mcp-gateway/`.

## Conventions
- `config.toml` holds only `${ENV}` refs + public endpoints; secret VALUES come
  from the environment (LaunchAgent `EnvironmentVariables`). It's gitignored, so
  it never shows as a git change after a UI edit. To change the shipped seed,
  edit `config.default.toml`.
- A tool's `original` is the bare backend tool name. Each backend is proxied on
  its OWN endpoint (`/<backend>/mcp`), so tools are exposed BARE — no `<backend>_`
  prefix; the endpoint / Claude-Code server registration namespaces them.

## Gotchas (verified against FastMCP 3.4.2)
- Spec was written for FastMCP 2.x. v3 changes: `create_proxy()` (not
  `FastMCP.as_proxy`); transforms are `ToolTransform({name: ToolTransformConfig(
  ..., arguments={p: ArgTransformConfig(...)})})`; arg field is `arguments`,
  not `transform_args`.
- Tools are exposed BARE on each backend's own endpoint (`exposed_name` returns
  the original). The old `<backend>_` prefix is gone — per-backend endpoints
  namespace by server registration (#29).
- Apply `add_transform` AFTER the per-backend reconcile, else every renamed tool
  is falsely flagged `override_no_match` (reconcile must see source names).
- FastMCP's INFO "reusing existing session … context mixing" line is benign
  here (fresh session per request; no per-request user secrets). Quieted via
  `FASTMCP_LOG_LEVEL=WARNING`.
  > Per-session-isolation tripwire tracked at [#25](https://github.com/voidfreud/mcp-gateway/issues/25).
- **Collision validation:** `admin.check_no_collision` (called in
  `apply_tool_override`) rejects a save whose broadcast NAME would equal another
  enabled tool's, or a deliberately-set DESCRIPTION identical to another's.
  `admin.effective_tools(cfg)` computes every enabled tool's effective name/desc.
  Scope is per-backend now: names only need to be unique WITHIN a backend (each
  backend is its own endpoint/MCP server, so cross-backend clashes can't confuse
  Claude).
- **Eager loading (always_load):** `ToolOverride.always_load` (per-tool) /
  `Backend.always_load` (per-server) → `build_transforms` sets the tool's
  `_meta["anthropic/alwaysLoad"]=true` (FastMCP `ToolTransformConfig.meta`), which
  exempts it from Claude Code tool-search deferral (loads upfront). Verified the
  meta propagates to the wire `_meta`. Per-backend pinning needs the live tool
  list, so `build_transforms(cfg, backend, all_tools)` — built per backend in the
  server lifespan, after `ensure_defaults`. Pinning hot-reloads (meta only).
- **Server instructions (per endpoint):** a bare proxy drops each backend's
  server-level `initialize.instructions`. The gateway captures them (in the
  defaults JSON, alongside `server_info`/`capabilities`) and
  `config_loader.backend_instructions(b, captured)` sets EACH backend proxy's own
  `instructions` (its `Backend.instructions` override else the captured original).
  Each backend is its own endpoint → its own ~2KB budget (#29; no cross-backend
  composition or gateway-level override anymore). Set live in
  `server._mount_backend` and re-set on `admin.hot_reload`; edit via
  `PUT /admin/api/instructions` ({backend, value}). Old defaults files
  (pre-capture) auto-re-introspect on startup.
- One resident subprocess per stdio backend — calls do NOT re-spawn it.
- Hot-reload swaps a backend's transform by mutating its proxy's `_transforms`
  (a list); tools/list applies transforms live per request, so the swap is
  instant. `admin.hot_reload(registry, holders, cfg, backend, log)` targets that
  one backend's live proxy (registry: name→proxy, holders: name→[transform]).
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

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **mcp-gateway** (280 symbols, 652 relationships, 24 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/mcp-gateway/context` | Codebase overview, check index freshness |
| `gitnexus://repo/mcp-gateway/clusters` | All functional areas |
| `gitnexus://repo/mcp-gateway/processes` | All execution flows |
| `gitnexus://repo/mcp-gateway/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
