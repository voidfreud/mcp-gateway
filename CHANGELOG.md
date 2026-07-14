# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
