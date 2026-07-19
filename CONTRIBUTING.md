# Contributing to mcp-gateway

Start with the repository’s canonical [contributor manual](AGENTS.md). It
defines scope, issue tracking, verification, documentation, release, and PR
policy for every contributor and coding client.

## Prerequisites

Install [`uv`](https://docs.astral.sh/uv/) and
[`just`](https://just.systems/). From a checkout, prepare the locked
development environment and run the standard gate:

```sh
uv sync --locked
just check
```

For product setup, configuration, and local service operations, use the
[installation guide](docs/installation.md),
[configuration reference](docs/configuration.md), and
[operations guide](docs/operations.md). Do not run stateful install, update,
restart, uninstall, or purge commands without explicit authorization.

## Pull requests

Use a focused branch and a GitHub Issue for bugs, deferred work, features,
follow-ups, or decisions. Include the relevant tests and documentation, explain
which verification layers ran, and open a pull request for CI and review.
Use an accurate Conventional Commit pull-request title: `fix:` releases a patch,
`feat:` releases a minor, and `!` releases a major. The title check does not
accept a `BREAKING CHANGE` footer alone as a major signal. Approved changes are
squash-merged; do not push directly to `main`.

[The release guide](docs/releases.md) is the canonical policy for versioning,
Release Please, release pull-request review, correction, and private release
consumption. Do not manually bump ordinary versions or duplicate its workflow.
Use the [testing and verification guide](docs/testing.md) to distinguish
required CI from advisory checks and machine-specific release receipts.

For a decision proposal or accepted ADR, follow the
[decision process](docs/decisions/README.md).
