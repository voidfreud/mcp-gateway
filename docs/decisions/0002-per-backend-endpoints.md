# 2. One gateway MCP endpoint per backend

Status: Accepted (2026-07-01) — issue
[#29](https://github.com/voidfreud/mcp-gateway/issues/29) (fixes the shared 2KB
instructions budget; also resolves
[#9](https://github.com/voidfreud/mcp-gateway/issues/9))

## Context

The gateway originally aggregated all backends behind ONE `create_proxy` served
at a single `/mcp` endpoint, registered as one `gateway` MCP server in Claude
Code. Claude Code truncates each MCP server's `instructions` at ~2KB. With one
server, every backend's server-level instructions were composed into that single
2KB budget — measured at ~2306–2332 B for the configured backend set, over the
cap, so the tail was silently truncated, and each added backend made it worse
([#29](https://github.com/voidfreud/mcp-gateway/issues/29)).

The single aggregating proxy also forced an all-or-nothing session strategy: any
`stateless=false` backend made the whole proxy hold persistent connections, so a
down external backend could block boot
([#9](https://github.com/voidfreud/mcp-gateway/issues/9)).

## Decision

We will expose **one MCP endpoint per backend**: build one single-backend
`create_proxy` per backend, mount each at `/<backend>/mcp` under a single parent
Starlette app (alongside `/admin` and `/health`), composing each mounted app's
session-manager lifespan via an `AsyncExitStack`. Each backend is registered as
its own MCP server in Claude Code (`gateway-<backend>` → `/<backend>/mcp`). Each
proxy carries only its own backend's `instructions` and its own session strategy.

## Consequences

- Each backend gets Claude Code's full ~2KB instructions budget — none truncated.
- Tools are exposed BARE (no `<backend>_` prefix); the endpoint / server
  registration provides the namespace. Collision checks scope to within a backend.
- Per-backend session strategy falls out for free
  ([#9](https://github.com/voidfreud/mcp-gateway/issues/9)): `stateless=false` →
  warm persistent client, `stateless=true` → per-request; a down backend only
  fails its own endpoint, never blocks the rest of the daemon from booting.
- The cross-backend "gateway instructions" composition and gateway-level full
  override are removed (there is no single gateway `instructions` field anymore).
- **User-visible:** Claude Code now lists N servers (`gateway-deepwiki`, …)
  instead of one `gateway`. A one-time re-registration is required (see README
  "Wire into Claude Code").

## Alternatives considered

- **Interim trim under 2KB** — compress the composed blob to fit. Rejected as the
  real fix: it loses information and breaks again as backends grow.
- **Keep one endpoint, relocate instructions** (e.g. into a synthetic tool's
  description) — rejected: server `instructions` are a distinct, always-loaded
  slot; relocating changes semantics and Claude would not treat it the same.
