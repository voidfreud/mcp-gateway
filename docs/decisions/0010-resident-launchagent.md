# ADR-0010: Keep a resident macOS LaunchAgent

**Status:** Accepted

**Decision date:** 2026-08-03

**Deciding issue / PR:** [#214](https://github.com/voidfreud/mcp-gateway/issues/214)
and the A7 availability-model decision recorded during implementation

**Related to:** [ADR-0002](0002-per-backend-endpoints.md),
[ADR-0004](0004-per-session-isolation.md), and
[ADR-0009](0009-process-isolation-boundary.md)

## Context

The supported macOS deployment exposes stable local HTTP endpoints and one
shared Admin UI. It must choose deliberately between an always-running daemon,
launchd socket activation, and a separate stdio gateway process per client.
That choice also controls upgrade, removal, latency, and idle-resource behavior.

Socket activation would reduce idle residency, but it would add first-connect
latency, remove warm backend sessions, and require the HTTP server to adopt
launchd-owned sockets. Stdio-per-client would discard the shared endpoint and
Admin model and multiply the gateway process for multiple clients.

A resident process has a real cost. The gateway stays in memory, and enabled
warm stdio backends may keep child processes alive. Those backend processes can
dominate the footprint; service-management code must not add polling loops or
workers that obscure that cost.

## Decision

Keep one resident macOS LaunchAgent. Preserve the existing shared HTTP service,
Admin UI, and warm-session behavior. Users can disable a backend to unmount it
and release its session, or set `stateless = true` when a persistent backend
session is unnecessary or undesirable.

The service lifecycle is application-owned:

- First run asks once before installing; scripts use explicit
  `--install-service` or `--foreground` modes.
- The LaunchAgent runs a stable app-owned wrapper, and that wrapper execs the uv
  tool shim. A missing shim exits successfully and leaves the job inert instead
  of producing a launchd crash loop.
- Plist and wrapper writes are atomic and versioned. Upgrades re-render stale
  service files before a controlled restart.
- Installation captures the user's PATH, does not double-bootstrap an already
  loaded job, and migrates checkout-era service artifacts.
- Removal boots out the job, removes the plist, wrapper, prompt marker, and
  legacy symlink, then makes config/log retention an explicit choice.
- Updates replace only application code, preserve config/state, restart the
  resident service, and verify both `/health` and `/ready`; rollback installs an
  immutable prior version through the same path.
- Update discovery is one offline-tolerant check at startup and at most daily.
  It never applies an update without an explicit command.
- Resource reporting is on demand. Verification measures idle CPU/RSS and
  process-tree growth across restart/update cycles instead of asserting an
  arbitrary memory ceiling.

## Consequences

The product retains immediate endpoint availability and warm backend latency.
It also retains the operational complexity of a resident service: upgrades and
uninstalls must coordinate with launchd, and warm backends consume resources
while enabled. The wrapper, atomic service files, bounded lifecycle operations,
and explicit data retention make those costs controlled rather than implicit.

Socket activation remains the preferred future option if zero idle residency
becomes more important than warm-session latency. Adopting it requires a new ADR
and an implementation that consumes launchd-provided sockets; it is not a plist-only
switch. Stdio-per-client remains a product-boundary change rather than an
installation mode.
