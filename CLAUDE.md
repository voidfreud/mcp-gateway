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
- Dev: `uv run mcp-gateway` → one `/<backend>/mcp` endpoint per backend, plus `/admin` + `/health`. Code lives in `src/mcp_gateway/` (installable package; console script `mcp-gateway`).
- Login: launchd LaunchAgent `com.void.mcp-gateway.plist`, installed via
  `just install` / `./install.sh` (#149): every plist path routes through the
  stable symlink `~/.local/opt/mcp-gateway` → the repo, so after a repo move you
  re-run the script and nothing else. `/health` names the daemon's resolved code
  path (`ok mcp-gateway <ver> @ /path`) — if it doesn't match the repo's real
  location, you're talking to a ghost process from an old clone.

## How to verify
With the daemon up: `uv run verify_rename.py http://127.0.0.1:9100` — checks every
backend endpoint (bare names, instructions <=2KB) + a real passthrough call.
`uv run config_loader.py config.toml` prints parsed backends + transform keys.
Per-backend liveness: `GET /admin/api/status` (#23).

## Layout
- `src/mcp_gateway/config_loader.py` — Pydantic models + `config.toml` → proxy config + transforms; also `dump_toml`/`save` (admin writes config back).
- `src/mcp_gateway/server.py` — one single-backend `create_proxy` per backend, each mounted at `/<backend>/mcp` under a parent Starlette (+ `/admin` + `/health`); lifespans composed via `AsyncExitStack`.
- `src/mcp_gateway/admin.py` — admin UI/API at `/admin`: introspect/defaults, edit overrides, in-process hot-reload, import/remove (restart).
- `src/mcp_gateway/admin.html` — single-file vanilla-JS admin page (no framework, no build).
  Tests guard it against merge-conflict remnants and JS syntax errors
  (`node --check` of the inline script) — it broke silently once.
- `install.sh` — idempotent launchd (re)install: bootstraps the venv (uv sync),
  renders `deploy/com.void.mcp-gateway.plist.template` (@@HOME@@) for the
  installing user, wires the `~/.local/opt` symlink (#149). `--dry-run` prints
  actions. `just install` wraps it. The repo carries NO personal paths.
- `config.toml` — the LIVE, admin-managed config (**gitignored** — regenerated on
  every UI save). Auto-seeded from `config.default.toml` on first run (`config_loader.ensure_config`).
- `src/mcp_gateway/config.default.toml` — committed runnable seed (ships in the wheel; seeds config.toml on first run).
  `config.example.toml` — full annotated schema reference. Defaults/backups for
  the runtime live under `~/.local/state/mcp-gateway/`.
- `.claude/skills/mcp-tool-design/` — the tuning pipeline for the backend
  broadcast text that is this project's core work: research each tool (cached in
  the skill's `research/`) → grade as a cold agent → differentiate overlapping
  tools across backends → draft → apply → verify live. Reach for it when editing
  any instructions / tool name / description / parameter for a backend.
  **Keep it in sync:** any app change that alters how overrides are authored,
  applied, or verified (schema, transform mechanics, admin flow, capture/defaults)
  must update the skill's `references/levers.md` in the same change — its
  `scripts/surface.py` also imports `config_loader`/`admin`, so lever-schema
  changes can break it.
- `corpus/` — the research the skill was distilled from (MCP spec, Anthropic /
  AWS / Google guidance, articles, third-party reference skills). Read-only source
  material; excluded from the GitNexus index via `.gitnexusignore`.

## Conventions
- `config.toml` holds only `${ENV}` refs + public endpoints; secret VALUES come
  from the environment or the gateway-scoped `~/.config/mcp-gateway/secrets.env`
  (override via `MCP_GATEWAY_SECRETS`; env wins; file values are kept out of
  `os.environ` so stdio backends can't read them). It's gitignored, so
  it never shows as a git change after a UI edit. To change the shipped seed,
  edit `config.default.toml`.
- A tool's `original` is the bare backend tool name. Each backend is proxied on
  its OWN endpoint (`/<backend>/mcp`), so tools are exposed BARE — no `<backend>_`
  prefix; the endpoint / Claude-Code server registration namespaces them.

## Gotchas (verified against FastMCP 3.4.4)
- **Origin guard (MCP spec MUST):** `server.OriginGuardMiddleware` 403s any
  browser request whose `Origin` isn't the gateway's own loopback origin — the
  spec's DNS-rebinding protection for Streamable HTTP. Non-browser clients send
  no Origin and pass. FastMCP grew its own opt-in Host/Origin guard in 3.4.3/4;
  ours sits on the parent app so /admin is covered too.
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
- **Warm-session recycle (#161):** warm (`stateless=false`) backends reuse ONE
  backend session for the daemon's life — but fastmcp's shared clients never
  self-heal a dead remote session (verified upstream), which is why http backends
  used to be left stateless. The gateway now supervises them: `server.is_session_death`
  classifies a dead-session exception (ClosedResourceError / broken pipe / session
  terminated / disconnected — conservative, never a blanket Exception), the
  call-log middleware fires the per-backend `recycle` hook fire-and-forget (never
  awaited in the call path; the failing call still fails, the NEXT finds a fresh
  session), and the runner tears down + re-mounts (fresh AsyncExitStack → fresh
  client). The status probe recycles a warm backend that probes `error` too.
  Cooldown: at most one recycle per backend per 30s (`server.RECYCLE_COOLDOWN`,
  module-scoped `_last_recycle` — resets on restart), so a hard-down backend can't
  flap; a recycle inside the window logs `recycle_skipped`. The recycle re-reads
  fresh config, so `POST /admin/api/backend/{name}/stateless` just saves + recycles
  (no daemon restart). **Import now defaults new backends to warm for EVERY
  transport** (`stateless: false` in admin.html `doImport`) now that recycling exists.
- One resident subprocess per stdio backend — calls do NOT re-spawn it.
- **Accepted spec gaps (#92):** the proxy does not forward the `completions`
  capability (FastMCP's server side has no completion handler to register —
  verified in fastmcp 3.4.2 source; a backend's argument-autocompletion is
  dropped), and FastMCP's proxy auto-paginates the backend's tools/list into
  one page with no `nextCursor`. Both are framework behavior, low impact for
  Claude Code (it uses neither), documented here instead of worked around.
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
- **Override text edits need a client reconnect to show up.** A name/description/
  param edit hot-reloads on the server instantly, but an already-running Claude
  Code session keeps the old broadcast until you restart the session or manually
  reconnect the MCP server (`/mcp`). Distinct from topology/registration changes
  (which also need the launchd restart). Reconnect before verifying an override,
  or you grade stale text. (Also step 2 of the verify loop in the mcp-tool-design
  skill's `references/levers.md`.)
- **Injected param defaults (#35):** `ParamOverride.default` (scalar only, mirrors
  `ArgTransformConfig.default`) → the only way to hide a *required* param. Pass the
  kwarg to `ArgTransformConfig` only when set — its `to_arg_transform` uses
  `exclude_unset`, so an explicit None differs from never-set.
- **Baseline auto-refresh (#43):** `admin.refresh_defaults` re-captures + diffs;
  throttle stamps live in in-process `admin._last_refresh` (reset on restart = the
  post-mount refresh runs once per boot, by design; failures keep the stamp so a
  down backend retries at throttle cadence, not per trigger). `tools/list_changed`
  is subscribed only for STATEFUL backends (persistent client), and the handler
  only ENQUEUES into `server._AutoRefresh`'s bounded queue — never re-capture
  inside the message handler, it shares the session's message pump with live tool
  calls. Overrides survive every refresh (diffs by original name).
- **Status probes (#23):** `/admin/api/status` runs one `Client(proxy)` +
  `list_tools` per backend concurrently (STATUS_TIMEOUT 5s). Counts post-transform
  tools; a probe of a stateless stdio backend spawns a short-lived subprocess.
- **Bearer token (#26):** resolved via `expand_env` ONCE in `_build_app` (missing
  env fails boot loudly); gates backend endpoints AND `/admin/api/*` (an open
  admin API would let any local process rewrite config or run backend tools —
  2026-07-12 audit); only `/health`, `/ready`, and the bare `GET /admin` page
  are exempt (the UI prompts for the token and stores it in localStorage). In `claude mcp add`
  the `--header` option is VARIADIC — it must come AFTER `<name> <url>` or it
  swallows them (live-caught); `claude_mcp_command` encodes this and the register
  route redacts the token from everything it echoes.
- **Hard rename (#44)** is a topology op: config key + defaults file +
  `_last_refresh` stamp migrate, then restart. `deregister` deliberately works for
  backends absent from config (the post-remove/rename cleanup path).
- **Uniquify (#22)** is a per-save escape hatch (`"on_collision": "uniquify"` at
  the payload top level) — names only; description collisions still reject.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **mcp-gateway** (747 symbols, 2054 relationships, 66 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
