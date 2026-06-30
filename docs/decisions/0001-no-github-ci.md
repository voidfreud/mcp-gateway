# 1. No GitHub Actions CI

Status: Accepted (2026-06-28) — issue
[#24](https://github.com/voidfreud/mcp-gateway/issues/24)

## Context

The repo has two test layers: a pytest + Hypothesis pure-logic suite (no backend
dependency) and `just verify` (live-daemon assertions via `verify_rename.py`,
hitting the running launchd-supervised gateway against stdio gitnexus + HTTP
deepwiki/context7).

A GitHub Actions runner can run the pure-logic suite. It cannot run `just verify`
— the load-bearing tests need local backends and a running launchd daemon,
neither of which GitHub Actions can provide.

## Decision

We will not add a GitHub Actions CI workflow for this repo. The local gate is
`just check` (ruff + pytest + import smoke) before commit, plus `just verify`
against the live daemon for behavior.

## Consequences

- No green/red badge on PRs. Acceptable: this is a private solo repo, no external
  contributors.
- The pure-logic suite (which IS CI-able) is verified locally each commit, same
  as everything else.
- If circumstances change (a contributor joins, or the test layers split so the
  pure-logic suite stands alone as a meaningful signal), revisit.

## Alternatives considered

- A ~15-line Actions workflow running only the pure-logic suite — rejected as
  low-value: it would catch only what `just check` already catches locally, and
  adds an external dependency.
- A self-hosted runner on this Mac — rejected as over-engineering for a solo
  project; the local gate already runs on this Mac.
