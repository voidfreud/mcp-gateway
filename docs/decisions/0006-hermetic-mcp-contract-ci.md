# 6. Hermetic MCP protocol contract CI

**Status:** accepted

## Context

The original live gate depends on one developer's configured backends and
login daemon, so ADR-0003 correctly kept it out of CI. That left a different
class of risk uncovered: the gateway's own Streamable HTTP, JSON-RPC, session,
security, and independent-endpoint behavior was tested mainly through the same
FastMCP client library that implements the server.

The official MCP conformance runner is valuable but is not a generic
certification sweep. Its complete server suite expects a purpose-built backend
with prescribed tool, resource, prompt, sampling, progress, and elicitation
behavior. Running that full suite against arbitrary third-party backends would
produce fixture failures rather than useful gateway evidence.

## Decision

Every pull request and main push runs two isolated protocol receipts:

1. `tests/live/run_mcp_wire.py` drives direct HTTP and JSON-RPC messages through
   disposable stateful and stateless fixture backends, the permanent Virtual
   Tools mount, and the real gateway process. It checks lifecycle, sessions,
   negotiated capabilities, catalogs, calls, errors, bearer/origin handling,
   and the independent endpoint topology—including the deliberate absence of
   an aggregate `/mcp` route.
2. `tests/conformance/run_official.py` runs the pinned official
   `@modelcontextprotocol/conformance@0.1.16` checks for initialization, ping,
   tool-list shape, and DNS-rebinding protection against a fixture routed
   through `/<backend>/mcp` at stable MCP `2025-11-25`.

Both launchers create private HOME, config, state, hooks, logs, ports, and
process groups. They never contact the installed daemon, configured user
backends, secrets, or external MCP providers. CI retains their receipts as an
artifact. The official subset is described as a smoke test, not full MCP
certification.

## Consequences

- Protocol and topology regressions become required CI failures without making
  personal infrastructure part of the build.
- The original `just verify` live-backend gate remains local and complementary.
- The official runner and stable protocol revision are pinned. Upgrades are
  reviewed changes, not moving CI inputs.
- Expanding the official suite requires adding the exact prescribed fixture
  behavior first; expected-failure baselines are not used to manufacture a
  passing compliance claim.
