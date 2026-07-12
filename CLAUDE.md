# CLAUDE.md

## What this is (the north star)

A local MCP proxy daemon (FastMCP v3) between Claude Code and backend MCP
servers. Most MCP servers ship sloppy broadcast text — vague names, empty
instructions, useless param docs — so agents can't use them. The gateway
rewrites EVERYTHING a backend broadcasts (tool name/title/description, every
param name/description, server instructions, pinning, hide+inject) until a
sloppy server reads like a well-designed one, while forwarding real calls
untouched. One loopback daemon at login, shared by all sessions. Mission
statement: issue #121. The ongoing hand-work is tuning backends with the
`.claude/skills/mcp-tool-design` skill — 4 configured backends (gitnexus,
graphitti, serena, xapi) are still disabled/untuned.

## Stack & layout

Python 3.12 · FastMCP 3.4.4 · Pydantic · structlog · `uv` · hatchling (src
layout, console script `mcp-gateway`). MIT; distributed via
`uv tool install git+…` (NOT PyPI — name taken; pyproject blocks upload).

- `src/mcp_gateway/config_loader.py` — Pydantic models; config.toml → proxy
  config + `ToolTransform`s; `${ENV}` expansion (env > `~/.config/mcp-gateway/
  secrets.env`, values kept out of os.environ); atomic `save`.
- `src/mcp_gateway/server.py` — one single-backend `create_proxy` per backend
  mounted at `/<backend>/mcp` under a parent Starlette (+ `/admin`, `/health`,
  `/ready`); per-backend runner tasks own each mount's lifecycle (anyio scopes
  are task-owned — the LIFO rule is load-bearing); middleware chain: Origin
  guard → body limit → optional bearer. `_AutoRefresh` (baseline refresh) and
  the recycle worker (#161) live in the lifespan.
- `src/mcp_gateway/admin.py` — admin API + helpers: capture/defaults, override
  diffing, collision + transform-dry-build validation, hot reload, Claude Code
  CLI integration, settings, stale-override migration.
- `src/mcp_gateway/admin.html` — single-file vanilla-JS admin UI (no build).
  Guarded by tests: no merge-conflict markers, inline JS must pass
  `node --check`.
- `src/mcp_gateway/config.default.toml` — seed; ships in the wheel.
  `config.example.toml` — annotated schema reference (repo root).
- `config.toml` — LIVE admin-managed config (gitignored, regenerated on UI
  save). Runtime state: `~/.local/state/mcp-gateway/` (defaults/, backups/,
  gateway.log).
- `install.sh` + `deploy/com.void.mcp-gateway.plist.template` — the macOS
  install: bootstraps venv, renders the plist for the user (@@HOME@@), wires
  the `~/.local/opt/mcp-gateway` symlink (#149). The repo carries no personal
  paths (tests enforce).
- `docs/` — the user manual (installation, admin guide, configuration, ops,
  security, API). `CHANGELOG.md` — Keep-a-Changelog. `verify_rename.py` —
  live receipt script.
- `.claude/skills/mcp-tool-design/` — the tuning pipeline (research → grade
  cold → differentiate → draft → apply → verify live). **Sync rule:** any app
  change to how overrides are authored/applied/verified MUST update its
  `references/levers.md` in the same change; its `scripts/surface.py` imports
  the package.
- `corpus/` — read-only third-party research material; excluded from GitNexus.

## Run / verify / ship

- Dev: `uv run mcp-gateway` (`--version` smoke). Config precedence:
  `MCP_GATEWAY_CONFIG` > `./config.toml` > `~/.config/mcp-gateway/config.toml`
  (auto-seeded).
- Login service: `just install` (idempotent; re-run after moving the repo).
- Gate: `just check` = ruff lint+format, pytest, import smoke. CI runs it on
  every PR/push (cached, ~30s) + wheel-integrity check; pushing a `v*` tag
  releases with the wheel attached.
- Live receipts: `uv run verify_rename.py http://127.0.0.1:9100` (46 checks:
  bare names, budgets, passthrough, status, injection, bearer).
  `/health` names the daemon's resolved code path — if it isn't this repo,
  you're talking to a ghost process from an old clone.
- Ship: branch → PR (one closing keyword per issue) → squash-merge → deploy is
  `./install.sh` (plist/code changed) or `POST /admin/api/restart` (code only).

## Gotchas (verified against FastMCP 3.4.4)

- v3 API: `create_proxy()`; transforms are `ToolTransform({original:
  ToolTransformConfig(..., arguments={p: ArgTransformConfig(...)})})`. Private
  attrs we rely on (`proxy._transforms`, `_mcp_server.notification_options`,
  MessageHandler dispatch) are tripwired by tests.
- Tools are exposed BARE per endpoint; apply `add_transform` AFTER `_reconcile`
  (reconcile must see source names).
- **Transform target names are globally unique per backend — enabled OR
  disabled.** The broadcast-level collision check allows what FastMCP's
  build rejects, so `apply_tool_override` ends with a transform dry-build; a
  duplicate is a 400, never a persisted config that fails every mount (#152).
- **Sessions:** `stateless=false` (warm, default for new imports) = one
  persistent client; FastMCP does NOT heal a dead shared session, so the
  gateway does: `is_session_death()` classification in CallLogMiddleware →
  queued recycle (unmount→remount, 30s cooldown), also fired from a failed
  status probe. `stateless=true` = fresh session per call (the safe fallback).
  Toggle live via `POST /admin/api/backend/{name}/stateless`.
- **Baseline auto-refresh (#43):** post-mount, on `tools/list_changed`
  (stateful clients only; handler only ENQUEUES — never block the message
  pump), on admin page load; throttled (300s / 2s push floor) in in-process
  `admin._last_refresh`. Overrides are diffs by original name — refresh never
  clobbers them; a backend renaming tools upstream leaves DANGLING overrides
  (text silently inactive) → the UI's stale-override banner migrates/discards
  them (#153). Baselines capture concurrently at boot.
- **Auth:** optional `bearer_token` (${ENV} ref, resolved once at boot) gates
  backend endpoints AND `/admin/api/*` (open admin = config writes + tool
  execution for any local process); only `/health`, `/ready`, bare `GET
  /admin` stay open. Origin guard 403s foreign browser origins on every route
  (MCP-spec MUST, DNS rebinding). `claude mcp add --header` is VARIADIC — it
  must come after `<name> <url>` or it swallows them.
- **Admin editing model:** every broadcast text is editable; original names
  read-only. Fields prefill with effective values; only diffs vs captured
  defaults are stored (`_override_vs_default`). Hiding a REQUIRED param needs
  an injected `default` (#35). Text edits hot-reload in-process but a
  connected Claude session shows old text until reconnect (`/mcp`) — verify
  after reconnect or you grade stale text.
- Topology changes restart via launchd (`restart_response` is honest in dev);
  `install.sh` waits out launchd's ASYNC bootout before bootstrapping (race →
  "Bootstrap failed: 5"). launchd runs the venv console script through the
  symlink; recreate the venv with `uv sync`.
- Accepted spec gaps (#92): completions capability not forwarded; tools/list
  served as one page. Framework-level, irrelevant to Claude Code.
- Backlog: #162 (per-tool output-cap lever), #157 (age-gate boot refresh),
  plus the parked set (#10, #13–16, #18, #21, #25) and north-star #121.

## Hard-won session learnings (2026-07-12 — read before repeating them)

- **After moving this repo:** venv shebangs, the GitNexus registry, AND the
  installed LaunchAgent all go stale. `rm -rf .venv && uv sync`,
  `node .gitnexus/run.cjs index .`, `./install.sh` — in that order. A ghost
  daemon keeps `/health` green from deleted inodes; trust the path in the
  body, not the 200.
- **Never judge a merge by `tail` of its output.** A CONFLICT line scrolled
  away once and conflict markers shipped inside admin.html (it has no linter).
  Check `git status` / `git diff --check`; the test suite now guards
  admin.html specifically.
- **Upstream tool renames are a real, recurring failure** (openrouter renamed
  its entire tool set; all overrides silently detached while the tuned
  instructions pointed at nonexistent names). The stale-override banner exists
  for exactly this; the auto-refresh log line to watch is `override_no_match`.
- **Live verification catches what unit tests can't:** the `--header` argv
  order bug, the launchd bootstrap race, and the warm-session latency numbers
  (2.3s → 0.6s per probe on deepwiki) all came from running the real thing.
  `verify_rename.py` + a scratch daemon on a spare port are cheap — use them.
- **Agent worktrees branch from origin/main**, not your local branch — push
  foundations first or agents build on stale code. Same-file fan-outs merge
  with conflicts; keep-both is almost always the right resolution here.
- The mcp-tool-design skill's cold-eval (fresh Opus seats, turn-0 surface
  only) is the only grading that counts — the author cannot cold-read its own
  drafts. 5/5 routes after the openrouter re-tune.

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
