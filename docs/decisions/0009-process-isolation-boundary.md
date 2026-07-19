# 9. Process isolation is conditional, not a default gateway mode

**Status:** accepted (2026-07-19)

## Context

The gateway has independent MCP endpoints, but they share one Python daemon.
Adding a worker process per backend would introduce another local HTTP transport,
worker supervision, readiness/race handling, log routing, and a second place to
apply the FastMCP compatibility contract. Shipping that layer without a real
blast-radius requirement would make the gateway harder to operate without
isolating any current untrusted code: remote backends are reached through an
HTTP client, and stdio backends already run in child processes.

## Decision

Do not add a gateway-wide worker-process mode in this release. The current
boundaries are the appropriate isolation for the supported deployment:

- Each stdio backend is a separate child process owned by its FastMCP client.
- Each configured backend has its own lifecycle runner, exit stack, route, and
  recycle path; a failed connection or session does not tear down its peers.
- `stateless = true` provides a fresh backend session for every request when a
  backend must not share session state between callers.
- The gateway never forwards a caller's bearer token to a backend, so warm
  sessions do not mix caller identities.

True worker isolation becomes a production requirement only when a backend runs
untrusted in-process code, needs independent CPU/memory limits, forwards
caller-supplied credentials, or must survive a daemon-level dependency crash.
If one of those conditions appears, introduce an explicitly opt-in worker
protocol with supervised subprocesses, bounded local transport, health/readiness
handshakes, and the same raw-wire/conformance checks as the in-process path.

## Consequences

The architecture remains small and testable today, and the existing per-backend
and per-session levers are documented rather than hidden behind an implied
security guarantee. A future worker implementation is a deliberate feature,
not a silent change to session or authentication semantics.
