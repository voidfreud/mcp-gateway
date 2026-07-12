# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

- **Transform ordering** — apply tool transforms *after* the per-backend
  reconcile, so renamed tools are no longer falsely flagged as unmatched
  overrides (#152).

### Changed

- **Packaging** — the code now lives in `src/mcp_gateway/` as an installable
  package with a `mcp-gateway` console script (and a `--version` flag); the
  login-service plist ships as a template rendered per user by `install.sh`; the
  project is MIT licensed and distributed via uv from GitHub, not PyPI (#164).
- Pinned FastMCP to 3.4.4 (#163).

[Unreleased]: https://github.com/voidfreud/mcp-gateway/compare/main...HEAD
