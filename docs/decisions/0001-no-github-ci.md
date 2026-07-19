# ADR-0001: No hosted CI for the full live gate

**Status:** Superseded

**Decision date:** 2026-06-28

**Deciding issue / PR:** [#24](https://github.com/voidfreud/mcp-gateway/issues/24)

**Superseded by:** [ADR-0003](0003-check-only-ci.md)

## Context

The repo has two test layers: a pytest + Hypothesis pure-logic suite (no backend
dependency) and `just verify` (live-daemon assertions via `verify_rename.py`,
hitting the running launchd-supervised gateway against a stdio backend plus
HTTP documentation backends).

A GitHub Actions runner can run the pure-logic suite. It cannot run `just verify`
— the load-bearing tests need local backends and a running launchd daemon,
neither of which GitHub Actions can provide.

## Decision

At the time, the project chose not to add a GitHub Actions workflow. The local
gate was `just check` (ruff + pytest + import smoke) before commit, plus
`just verify` against the live daemon for behavior.

## Consequences

- There was no green/red PR signal; the pure-logic suite was verified locally
  with everything else.
- The decision required revisiting if the test layers split into a meaningful
  hermetic signal.

## Supersession

[ADR-0003](0003-check-only-ci.md) replaced this decision for the hermetic
`just check` gate. The only enduring boundary from this record is that the live
`just verify` gate needs a configured local daemon and backends, so it is not a
hosted-CI responsibility.

## Alternatives considered

- A ~15-line Actions workflow running only the pure-logic suite — rejected as
  low-value: it would catch only what `just check` already catches locally, and
  adds an external dependency.
- A self-hosted runner on this Mac — rejected as over-engineering for a solo
  project; the local gate already runs on this Mac.
