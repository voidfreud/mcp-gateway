# ADR-0003: Check-only CI and a local live gate

**Status:** Accepted

**Decision date:** 2026-07-03

**Deciding issue / PR:** [#98](https://github.com/voidfreud/mcp-gateway/issues/98)
and [#144](https://github.com/voidfreud/mcp-gateway/pull/144)

**Supersedes:** [ADR-0001](0001-no-github-ci.md)

## Context

ADR-0001 decided against GitHub Actions CI because the load-bearing tests
(`just verify`) need local backends and a running launchd daemon. That
reasoning applies only to the live gate. The pure-logic gate `just check` —
ruff + `ruff format --check` + pytest/Hypothesis + import smoke — needs zero
backends and zero launchd, and runs in seconds. ADR-0001 conflated "the full
gate needs local infra" with "no CI is possible"; the practical cost showed up
as reliance on local discipline (and one period where `just check` was
avoided entirely, #135).

## Decision

Add a minimal GitHub Actions workflow (`.github/workflows/check.yml`) that
runs only `just check` on pull requests and pushes to main (ubuntu-latest,
`uv sync`, no secrets). `just verify` stays local — it needs the running
daemon and real backends.

## Consequences

- Lint/format/test regressions are caught on every PR without relying on
  local discipline.
- Cheap: seconds of runtime, no secrets, no backends, no self-hosted anything.
- Coverage is partial by design: live per-endpoint behavior is still verified
  only locally via `just verify`. Accepted.
- ADR-0001 is superseded for hermetic checks. Its local-infrastructure boundary
  remains: the live gate is not run in hosted CI.

## Relationship to later CI decisions

[ADR-0006](0006-hermetic-mcp-contract-ci.md) extends the hermetic CI surface
with disposable MCP protocol receipts. It does not move the configured-daemon
and live-backend `just verify` gate into CI.

## Alternatives considered

- Status quo (no CI) — fine while solo and disciplined; no net on a bad push.
- Full CI including `just verify` — needs backends + launchd or containerized
  mocks in CI; high effort, out of scope.
