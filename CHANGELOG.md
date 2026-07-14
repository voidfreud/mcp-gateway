# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- **Admin UI visual revamp** (#170) — the single-file admin page got a
  deliberate design pass: a light theme (follows `prefers-color-scheme`, dark
  stays the base), inline-SVG iconography replacing the mixed emoji/text
  affordances, the backend control bar regrouped into captioned clusters
  (Broadcast / Session / Display name / Claude Code), a sticky backend
  header, skeleton placeholders for the async first paint, smooth
  expand/hover/toggle transitions (honoring `prefers-reduced-motion`), an
  always-visible byte-budget counter (amber near, red over the 2 KB cap), and
  a read-only **Behavior hooks** section on tool cards showing the configured
  `validate`/`post_process` specs plus a live hook-error badge (#16 surfaced
  in the UI for the first time). Still one hand-editable HTML file — vanilla
  JS, no build step, no external assets; every control keeps its endpoint and
  save semantics. The backend detail's **Register in CC** button is now
  **Register** inside the Claude Code cluster. After-screenshots live in
  `docs/img/` and the README.

### Added

- **Composite tools** — config-defined synthetic tools (`[[composites]]`) served
  on a shared `/composite/mcp` endpoint: one call fans out to member tools
  across one or many backends concurrently (per-member timeout), and returns a
  labeled merge with honest per-member status — a failed member never sinks the
  call. Members are called through the gateway's own per-backend proxies, so
  every override applies. Member selection is a pluggable strategy (`"all"`
  today) — the dispatch seam smart routing (#21) will build on. Admin API:
  `GET /admin/api/composites` and a live enable/disable toggle. (#14)
- **Code mode** — an opt-in `/meta/mcp` endpoint (`[meta] enabled = true`)
  exposing three meta-tools that let an agent script against the whole gateway
  catalog instead of loading every tool: `search` (deterministic keyword
  ranking across every mounted backend and the composite endpoint),
  `get_schema` (one tool's full **exposed**, post-rewrite definition), and
  `execute` (runs any tool through the gateway's own proxy path — every
  override applies — with honest structured errors for unknown targets).
  Disabled by default: the endpoint is simply absent. (#13)
- **Age-gated post-mount baseline refresh** (#157) — a new top-level
  `baseline_max_age` config knob (seconds, default `86400` = 24 h, `0`
  disables the gate). At boot/remount, a backend whose captured baseline is
  younger than the knob is not re-introspected (`baseline_fresh_skipped` in
  the log), sparing slow stdio backends a second cold start per boot. The
  event-driven refresh triggers — `tools/list_changed`, admin page load,
  manual Re-inspect — are never gated.
- **Orphan-sweep disjoint-config guard** (#157) — the boot sweep of stale
  `defaults/*.json` files now refuses to run (loud `orphan_sweep_refused`
  warning, nothing deleted) when more than half the captured baselines would
  be removed, which means the loaded config isn't the one that captured them
  (e.g. a scratch daemon on a test config sharing the real state dir — this
  wiped all real baselines once).

- **Per-tool output-cap lever** (#162) — a tool override can set
  `max_result_chars` (positive integer), broadcast as
  `_meta["anthropic/maxResultSizeChars"]`, which Claude Code honors over its
  global 25k-token `MAX_MCP_OUTPUT_TOKENS` cap for text content. It merges
  into the tool's captured `_meta` alongside the `always_load` pin flag, is
  editable in the admin UI (an "Output cap (chars)" field on the tool card)
  and via `PUT /admin/api/override` (`max_result_chars`; `null` clears; #139
  merge semantics), round-trips through export/import and override
  migration, and hot-reloads like every other broadcast edit.
- **`./install.sh --uninstall`** (#171) — one-command removal, symmetric with
  the install: boots out the LaunchAgent (tolerating not-loaded), removes the
  installed plist and the `~/.local/opt/mcp-gateway` symlink, and prints what
  was removed vs deliberately kept. User data is kept by default — config
  (`./config.toml` / `~/.config/mcp-gateway/`) and state/logs/backups
  (`~/.local/state/mcp-gateway/`); add `--purge` to delete the config and
  state directories too, after an explicit confirmation. Claude Code
  registrations are never touched (the script prints the
  `claude mcp remove gateway-<name>` hint). Idempotent — with nothing
  installed it says so and exits 0 — and `--dry-run` composes with both
  flags, like the installer. Also available as `just uninstall`.

- **Per-tool behavior hooks** (#16) — a tool override can name two
  user-authored Python hooks: `validate = "module:function"` and
  `post_process = "module:function"`, resolved in a dedicated hooks directory
  (`MCP_GATEWAY_HOOKS` > `./hooks/` > `~/.config/mcp-gateway/hooks/`; specs
  are imported, never eval'd, and cannot name a path outside the directory).
  `validate(args)` runs before the backend sees the call — raise
  `ValueError("why")` to reject it with that message as the tool error;
  `post_process(result)` reshapes the answer (truncate, strip, reformat)
  before the caller sees it. Sync or async; hooks compose with renames and
  hidden+injected params (they see the exposed argument names). This is
  deliberate arbitrary code execution in the daemon — documented as such in
  the security guide. A hook that fails to load fails **closed, per tool**:
  the mount and every other tool stay up while the hooked tool's calls error
  with the load failure (`hook_load_error` in the log, `hook_error` in
  `/admin/api/state`). Hooks are hand-authored in `config.toml`; the admin
  state shows them read-only and UI saves preserve them.

- **Resource and prompt text rewriting** (#15) — the tool override story now
  covers everything a backend broadcasts: resources and resource templates
  (display name/title/description, keyed by URI — the URI itself is never
  rewritten; disabling hides the entry and blocks reads) and prompts (rename
  with reverse-mapped `prompts/get`, title/description, per-argument
  descriptions; argument names stay fixed). Captured at introspection alongside
  tools, stored as diffs vs the captured defaults, hot-reloaded, validated
  (identifier rule + collision + transform dry-build), included in settings
  export/import, and editable in the admin UI via new sections below the tool
  cards. New endpoints: `PUT /admin/api/resource-override`,
  `POST /admin/api/resource-reset`, `PUT /admin/api/prompt-override`,
  `POST /admin/api/prompt-reset`.

- **Guarded non-loopback bind** (#18) — `host` may now point beyond loopback
  (e.g. a Tailscale IP) for multi-host use, but the config refuses to load a
  non-loopback bind without `bearer_token` set, so the gateway can never start
  exposed and unauthenticated. Documented in security.md ("Binding beyond
  loopback").

### Documentation

- **ADR-0004: per-session isolation** — recorded that caller isolation is the
  existing per-backend `stateless` lever, not a new gateway mode; added a
  "Session isolation between callers" section to the security guide (#25).

## [1.0.0] - 2026-07-12

The first feature-complete release. This wave (#150) turns the gateway from a
text-rewriting proxy into a full admin surface, then hardens and packages it for
distribution as 1.0.0.

### Added

- **Injected parameter defaults** — set a fixed value the gateway sends to a
  backend on every call, which also makes it safe to hide a *required* parameter
  from Claude (#35).
- **Connection status dots** — each backend shows a live health dot with its tool
  count, probed asynchronously through the running proxy so a down backend marks
  only itself (#23).
- **Auto-refreshing tool lists** — the captured baseline refreshes itself on
  every reconnect, on a backend's `tools/list_changed` signal, and on admin page
  load; overrides are stored as diffs and never clobbered (#43).
- **Hard rename** — change a backend's real identity (endpoint, config key,
  captured defaults, and Claude Code registration all move together) with a
  restart and a re-registration prompt, distinct from the cosmetic display name
  (#44).
- **One-click Claude Code registration** — register or remove a backend's gateway
  endpoint from the admin UI at a chosen scope, without the terminal (#45).
- **Auto-uniquify for bulk renames** — an opt-in toggle that retries a
  name-colliding save once with a deterministic `_2`/`_3` suffix and reports the
  final name (#22).
- **Stable-symlink install** — `install.sh` routes every login-service path
  through `~/.local/opt/mcp-gateway`, so moving the repo is fixed by re-running
  the script; `/health` now names the daemon's resolved code path to expose a
  stale process from an old clone (#149).
- **Warm sessions with self-repair** — a backend can hold one persistent
  connection (2–4× faster on live probes) instead of reconnecting per call; a
  dead session is detected and recycled automatically, at most once per 30
  seconds. Newly imported backends are warm by default, with a live per-backend
  toggle (#161).
- **Stale-override repair** — when a backend renames its tools upstream, the
  admin UI shows the now-inactive edits with one-click migrate/discard, and the
  sidebar flags the backend (#153).
- **Gateway settings card** — the bearer-token reference and the scheduled
  re-scan interval are editable in the UI (#155), with a one-click
  **Re-register all in Claude Code** for after a token change (#154).
- **Registration indicator** — each backend shows whether it is actually
  registered in Claude Code (#46).
- Startup sweep removes captured-defaults files for backends that no longer
  exist (#156); admin favicon; `verify_rename.py` gained status/injection/
  bearer receipts (#158).

### Security

- **Optional bearer token** — set `bearer_token` to require
  `Authorization: Bearer <token>` on every backend endpoint (#26).
- **Admin API gated by the token too** — a 2026-07-12 audit closed the gap where
  an open admin API let any local process rewrite config, restart the daemon, or
  run backend tools even with a token set; only `/health`, `/ready`, and the bare
  `GET /admin` page shell stay open (#159).
- **Origin guard** — a middleware on the parent app rejects cross-origin browser
  requests (the MCP spec's required DNS-rebinding protection, returning 403),
  covering the admin UI as well as the MCP endpoints (#163).

### Fixed

- **Duplicate broadcast names can no longer brick a backend** — a stale or
  disabled override sharing a transform target name used to pass validation and
  then fail every mount; saves now dry-build the transforms and reject with a
  clear message (#152).
- `claude mcp add --header` argument order (the flag is variadic and swallowed
  the name/URL when placed first) — caught in the live testing pass (#150).

### Changed

- **Packaging** — the code now lives in `src/mcp_gateway/` as an installable
  package with a `mcp-gateway` console script (and a `--version` flag); the
  login-service plist ships as a template rendered per user by `install.sh`; the
  project is MIT licensed and distributed via uv from GitHub, not PyPI (#164).
- Pinned FastMCP to 3.4.4 (#163).
- **CI** — dependency caching, lockfile consistency check, superseded-run
  cancellation, a wheel-integrity gate, and a tag-triggered release workflow
  that attaches the built wheel to a GitHub release (#165).

[1.0.0]: https://github.com/voidfreud/mcp-gateway/releases/tag/v1.0.0
