# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.3.2](https://github.com/voidfreud/mcp-gateway/compare/v1.3.1...v1.3.2) (2026-08-04)


### Documentation

* refresh audit release and security receipt ([#279](https://github.com/voidfreud/mcp-gateway/issues/279)) ([5e1a0ba](https://github.com/voidfreud/mcp-gateway/commit/5e1a0ba7af630e9b776d5e13a5f70aa29efa8890))

## [1.3.1](https://github.com/voidfreud/mcp-gateway/compare/v1.3.0...v1.3.1) (2026-08-04)


### Bug Fixes

* stop self-check from printing resolved secrets ([#275](https://github.com/voidfreud/mcp-gateway/issues/275)) ([9a24378](https://github.com/voidfreud/mcp-gateway/commit/9a243781d009e88ff275f21699b33c8f49782aee))

## [1.3.0](https://github.com/voidfreud/mcp-gateway/compare/v1.2.1...v1.3.0) (2026-08-04)


### Features

* complete public release readiness ([#271](https://github.com/voidfreud/mcp-gateway/issues/271)) ([eaf341d](https://github.com/voidfreud/mcp-gateway/commit/eaf341d8ba24b558978fb939a1f85d0e6cda9561))
* show update command after service install ([#273](https://github.com/voidfreud/mcp-gateway/issues/273)) ([43ec530](https://github.com/voidfreud/mcp-gateway/commit/43ec530d94098eb85212374941753da1a36d6f8d))


### Documentation

* record A8 public release receipt ([#260](https://github.com/voidfreud/mcp-gateway/issues/260)) ([5c76baf](https://github.com/voidfreud/mcp-gateway/commit/5c76baf7e13b4488edfa86ee774370cd1b34e557))

## [1.2.1](https://github.com/voidfreud/mcp-gateway/compare/v1.2.0...v1.2.1) (2026-08-03)


### Bug Fixes

* **deps:** upgrade cryptography to 50.0.0 ([#258](https://github.com/voidfreud/mcp-gateway/issues/258)) ([7dde596](https://github.com/voidfreud/mcp-gateway/commit/7dde596d547142d72b149282c82240ccb961a686))

## [1.2.0](https://github.com/voidfreud/mcp-gateway/compare/v1.1.0...v1.2.0) (2026-08-03)


### Features

* add auth-free distribution and updates ([#255](https://github.com/voidfreud/mcp-gateway/issues/255)) ([13b0ae8](https://github.com/voidfreud/mcp-gateway/commit/13b0ae899baec63e07b17fb736d27737c4ec05ab))

## [1.1.0](https://github.com/voidfreud/mcp-gateway/compare/v1.0.0...v1.1.0) (2026-08-03)


### Features

* add application-owned resident service lifecycle ([#253](https://github.com/voidfreud/mcp-gateway/issues/253)) ([31487b4](https://github.com/voidfreud/mcp-gateway/commit/31487b47bcc216a106d38e002c1020090c24dc57))
* add asynchronous structured logging and dashboard visibility ([20c02f5](https://github.com/voidfreud/mcp-gateway/commit/20c02f5174f7e3346a2a67ba91e80a0a6c33b87f))
* add first-class virtual tools ([#190](https://github.com/voidfreud/mcp-gateway/issues/190)) ([24180ba](https://github.com/voidfreud/mcp-gateway/commit/24180ba6553c056a3dd2bd65b04c69c88343d06c))
* add guarded update workflow ([#195](https://github.com/voidfreud/mcp-gateway/issues/195)) ([bf85992](https://github.com/voidfreud/mcp-gateway/commit/bf85992405205f6779bd64c732b16e615bbac1b1))
* add MCP client controls and live admin logs ([17b311f](https://github.com/voidfreud/mcp-gateway/commit/17b311f6f4c20263f1312ad12067ee06e47d18a5))
* add standards-compliant OAuth resource mode ([#191](https://github.com/voidfreud/mcp-gateway/issues/191)) ([52a35d7](https://github.com/voidfreud/mcp-gateway/commit/52a35d7e67cd77d6675545f09f5c210823bd852e))
* admin UI visual revamp — light+dark themes, grouped controls, iconography ([#170](https://github.com/voidfreud/mcp-gateway/issues/170)) ([#188](https://github.com/voidfreud/mcp-gateway/issues/188)) ([57d8dce](https://github.com/voidfreud/mcp-gateway/commit/57d8dce41156529bee33dd44382d3e00b955dced))
* age-gate the post-mount baseline refresh ([#157](https://github.com/voidfreud/mcp-gateway/issues/157)) ([#186](https://github.com/voidfreud/mcp-gateway/issues/186)) ([eda8684](https://github.com/voidfreud/mcp-gateway/commit/eda86842c93ec38077f0076235a44925004f3a03))
* guard non-loopback bind behind bearer_token ([#18](https://github.com/voidfreud/mcp-gateway/issues/18)) ([#178](https://github.com/voidfreud/mcp-gateway/issues/178)) ([48e3e76](https://github.com/voidfreud/mcp-gateway/commit/48e3e76987f74f752fae5a01c3df98fe49bbc1ce))
* install.sh --uninstall — one-command removal, symmetric with install ([#184](https://github.com/voidfreud/mcp-gateway/issues/184)) ([21ac24e](https://github.com/voidfreud/mcp-gateway/commit/21ac24e623cc0a5029bdb79d1ea72864a3860c13)), closes [#171](https://github.com/voidfreud/mcp-gateway/issues/171)
* log admin actions and lifecycle events ([074b29a](https://github.com/voidfreud/mcp-gateway/commit/074b29afd8ff88f482eaeb86c42b4fdd8ea15212))
* modernize MCP tool design skill ([#224](https://github.com/voidfreud/mcp-gateway/issues/224)) ([25b94b8](https://github.com/voidfreud/mcp-gateway/commit/25b94b801349c6f0af2ff115bef06bea3964285e))
* per-tool behavior hooks — validate and post-process ([#16](https://github.com/voidfreud/mcp-gateway/issues/16)) ([#177](https://github.com/voidfreud/mcp-gateway/issues/177)) ([c0d16f9](https://github.com/voidfreud/mcp-gateway/commit/c0d16f9190340262361ae96921808a7a4fd5aa0b))
* per-tool output-cap lever (anthropic/maxResultSizeChars) ([#185](https://github.com/voidfreud/mcp-gateway/issues/185)) ([cbb4dc6](https://github.com/voidfreud/mcp-gateway/commit/cbb4dc6c31379cf7276a49278c38b96ae922bfdd)), closes [#162](https://github.com/voidfreud/mcp-gateway/issues/162)
* register Virtual Tools in Codex ([#193](https://github.com/voidfreud/mcp-gateway/issues/193)) ([59d0771](https://github.com/voidfreud/mcp-gateway/commit/59d077135ea7e772a2ddb61a294a2c9b960ed05c))
* resource and prompt text rewriting ([#174](https://github.com/voidfreud/mcp-gateway/issues/174)) ([98b60f7](https://github.com/voidfreud/mcp-gateway/commit/98b60f7f4c2aa1186a5cb387c8aeea49968f8320)), closes [#15](https://github.com/voidfreud/mcp-gateway/issues/15)


### Bug Fixes

* create installer state directory ([#248](https://github.com/voidfreud/mcp-gateway/issues/248)) ([2cd26bd](https://github.com/voidfreud/mcp-gateway/commit/2cd26bd18a527e58f54d77544180e14590106fcf))
* enforce virtual schema contracts ([#251](https://github.com/voidfreud/mcp-gateway/issues/251)) ([257fb1c](https://github.com/voidfreud/mcp-gateway/commit/257fb1c97a4da6e415fa8e4cac1daf5add518fab))
* harden admin auth and API against empty tokens and malformed input ([4cc6ec4](https://github.com/voidfreud/mcp-gateway/commit/4cc6ec40c9c15323b66470bcbd32134262e39b51))
* harden audited MCP contracts ([#252](https://github.com/voidfreud/mcp-gateway/issues/252)) ([b398bd3](https://github.com/voidfreud/mcp-gateway/commit/b398bd317534fa6adbeb9a1c7e57a2a4fa574cf4))
* make guarded update checks executable ([a4d45ca](https://github.com/voidfreud/mcp-gateway/commit/a4d45ca57a15848d81b68d6aad5b716af8ebeda7))
* reject live prompt name collisions ([#250](https://github.com/voidfreud/mcp-gateway/issues/250)) ([c83303c](https://github.com/voidfreud/mcp-gateway/commit/c83303c9f0a665d8869cf1d1992b27113c06ca55))
* restrict bearer auth exemptions ([#249](https://github.com/voidfreud/mcp-gateway/issues/249)) ([63d6587](https://github.com/voidfreud/mcp-gateway/commit/63d658700ff2ec04799c3d65e0e99e124f431a57))
* retry daemon readiness after update ([5a437a3](https://github.com/voidfreud/mcp-gateway/commit/5a437a3e829e4ef98947a97aa491fa74d7cc1f22))
* satisfy ruff formatting check ([6fa1fca](https://github.com/voidfreud/mcp-gateway/commit/6fa1fca964241329eb2b2c7b8e25b60608e08cea))


### Documentation

* add audit report with prioritized action plan ([#247](https://github.com/voidfreud/mcp-gateway/issues/247)) ([1f92d53](https://github.com/voidfreud/mcp-gateway/commit/1f92d538fe3a6e4790c198889de266ddccdeab87))
* admin-guide names the real Export/Import buttons ([#169](https://github.com/voidfreud/mcp-gateway/issues/169)) ([015b81f](https://github.com/voidfreud/mcp-gateway/commit/015b81f198d166497c226bd15e9c5d06ab983971))
* ADR-0004 — per-session isolation is the stateless lever, not a mode ([#172](https://github.com/voidfreud/mcp-gateway/issues/172)) ([572fad1](https://github.com/voidfreud/mcp-gateway/commit/572fad17983497c469eea888a76df18818722f77)), closes [#25](https://github.com/voidfreud/mcp-gateway/issues/25)
* CLAUDE.md backlog reflects the cleared tracker ([#189](https://github.com/voidfreud/mcp-gateway/issues/189)) ([60aeb45](https://github.com/voidfreud/mcp-gateway/commit/60aeb45c2142c137222166e675a5dbefb5362b16))
* CLAUDE.md backlog synced to the parked-set resolution ([#179](https://github.com/voidfreud/mcp-gateway/issues/179)) ([6428a10](https://github.com/voidfreud/mcp-gateway/commit/6428a1064268edf5461d6e148ab372e76398a590))
* CLAUDE.md GitNexus rules use the gateway broadcast names ([#182](https://github.com/voidfreud/mcp-gateway/issues/182)) ([83cd7ba](https://github.com/voidfreud/mcp-gateway/commit/83cd7ba774413f563439c31724fae7581088d721))
* cold-eval harness caveat — seats inherit the live session's MCP context ([#183](https://github.com/voidfreud/mcp-gateway/issues/183)) ([f1259a1](https://github.com/voidfreud/mcp-gateway/commit/f1259a1f6991ddf9d264034ecea490fa3109a2d1))
* document suppressed release trigger recovery ([700e497](https://github.com/voidfreud/mcp-gateway/commit/700e4971e3c3792b5ef212813aa73454377cd8cf))
* establish project operating manual ([#218](https://github.com/voidfreud/mcp-gateway/issues/218)) ([cbb0fe1](https://github.com/voidfreud/mcp-gateway/commit/cbb0fe1abc6e0f9eb61fd1b3711b6da988b3acda))
* normalize decision governance ([b3fbe86](https://github.com/voidfreud/mcp-gateway/commit/b3fbe86b7d748af337c636da726b47796f0b86d6))

## [Unreleased]

### Changed

- **Application-owned macOS resident lifecycle** — the installed CLI now owns
  atomic versioned LaunchAgent setup, one-time interactive onboarding, stable
  wrapper/PATH capture, health/readiness-gated controlled restarts, migration
  from checkout-era service artifacts, explicit keep-or-purge removal, and
  on-demand gateway/backend process-tree resource reporting. Checkout
  `install.sh` is now a thin compatibility wrapper around the same lifecycle.

- **Guarded Path A updates** — `just update` now provides one explicit,
  fail-closed deployment command: it requires a clean `main` checkout, pulls
  only fast-forward changes from `origin/main`, synchronizes `uv.lock`, reloads
  launchd, and verifies daemon health/readiness without touching user config or
  runtime state.

- **Separated client registration policy** — Claude Code and Codex CLI
  discovery, command construction, auth handling, and registration parsing now
  live in dedicated reusable modules. The Admin API keeps compatibility
  exports and dynamic test seams, while its typed route groups remain the one
  path used by backend and Virtual Tools onboarding.

- **Typed live-runtime ownership and a smaller Admin facade** — backend proxies
  and their transform holders now move together through `BackendRuntime`, while
  snapshot inputs and Virtual Tool proxy lookup have explicit narrow types. The
  per-backend Codex registration routes moved into a typed route module; the
  existing `admin.py` API remains a compatibility facade with the same cache,
  monkeypatch, response, and hot-reload behavior.

- **Bounded MCP catalogs and structured Virtual Tool results** — every backend
  and `/virtual/mcp` now serves its transformed `tools/list` catalog in
  50-tool pages with gateway-owned opaque cursors, after fully consuming any
  upstream pagination. Virtual Tools advertise a stable JSON Schema 2020-12
  output envelope and preserve each member's upstream `_meta` inside that
  member record under the existing strict serialized-result budget.

- **FastMCP compatibility canary** — keep production exact-pinned at 3.4.4
  while a daily isolated checkout upgrades only FastMCP, then exercises the
  complete unit/property suite, public raw-wire contracts, and import smoke
  using the candidate environment and its installed console entry point.

- **Truthful Virtual Tools capabilities** — `/virtual/mcp` now advertises
  `tools.listChanged=false` (along with resources/prompts) because its Admin
  hot swaps do not yet have a server-wide downstream-session broadcast API.
  Explicit `tools/list` immediately observes every committed swap; clients are
  no longer promised a notification the gateway cannot deliver.

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

- **MCP OAuth resource-server mode** — each independent backend endpoint and
  `/virtual/mcp` can validate JWT access tokens from an external authorization
  server, publish RFC 9728 protected-resource metadata, bind tokens to the
  endpoint audience, and return MCP-compliant 401/403 scope challenges. Remote
  deployments require a separate Admin bearer token; legacy `bearer_token`
  mode remains unchanged.

- **MCP protocol contract CI** — every PR now drives raw Streamable HTTP and
  JSON-RPC messages through isolated stateful/stateless backends, Virtual Tools,
  and the real independent gateway mounts, then runs a pinned official MCP
  conformance smoke subset for stable protocol `2025-11-25`. Receipts are
  retained as CI artifacts; no installed daemon, user backend, secret, or
  aggregate `/mcp` endpoint is involved.

- **One-click Codex registration** — every backend remains an independent MCP
  and now has its own Codex status plus **Add/Remove** control in the Admin UI.
  Import and hard-rename can update Codex explicitly; backend removal performs
  best-effort cleanup. The integration uses `codex mcp add/remove/list --json`,
  detects ChatGPT desktop's bundled CLI, and passes bearer authentication only
  as an environment-variable name—never a resolved secret. Codex must be
  restarted or a new task opened after registration changes.

- **First-class Virtual Tools** — the Admin UI now has a separate Virtual
  Tools catalog for composing and routing live backend tools behind one
  permanent `/virtual/mcp` endpoint. Definitions use stable backend IDs and
  original tool/parameter identities, support concurrent `all`, deterministic
  `keyword`, and consent-gated OpenRouter `llm` dispatch, preserve rich MCP
  result content under a strict serialized-byte budget, and follow an explicit
  draft → validate/test → activate lifecycle. The endpoint participates in
  readiness and remains mounted with an empty catalog; backend removal is
  blocked while referenced. An isolated socket-level acceptance harness covers
  lifecycle, concurrency, routing/fallback, failures, rich results, budgeting,
  and backend rename stability.

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

### Fixed

- **MCP protocol compliance hardening** — reject live prompt-name collisions,
  preserve JSON Schema 2020-12 definitions through transforms, advertise
  endpoint-accurate initialize identity/capabilities, preserve request context
  across stateless proxy sessions, and return standard JSON-RPC prompt errors.
  The CI conformance gate now exercises all 24 applicable official server
  scenarios, while raw-wire receipts pin current and legacy initialization,
  auth/origin precedence, session termination, and transport errors.

- **Bounded backend I/O** — each backend now has validated, persisted
  `init_timeout` and `request_timeout` settings (30s/300s by default), so a
  hung handshake degrades only that endpoint and a stuck forwarded call cannot
  retain a request forever.

- **Fresh-install and bearer-route hardening** — the checkout installer creates
  its LaunchAgent log directory before bootstrap, and health/readiness auth
  exemptions now match complete path segments instead of attacker-controlled
  prefixes.

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
