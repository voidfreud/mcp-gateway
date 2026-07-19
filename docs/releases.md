# Releases

This is the canonical release process for mcp-gateway. Releases are GitHub
Releases for this private repository; mcp-gateway is never published to PyPI.
For the verification tiers and the redacted local release receipt, see
[testing and verification](testing.md).

## Version policy

Release Please derives the next [Semantic Versioning](https://semver.org/)
version from squash-merge pull-request titles. Use a Conventional Commit title:

- `fix: ...` produces a patch release.
- `feat: ...` produces a minor release.
- A `!` in the title (for example, `feat!: ...`) produces a major release.
- Other Conventional Commit types are allowed when accurate, but may not create
  a release.

The PR-title check validates this title-only policy. Do not rely on a
`BREAKING CHANGE` footer alone to request a major release; use `!` in the
title.

The current baseline is `v1.0.0`. The first automation proposal is expected to
be `v1.1.0` because the post-baseline history includes a `feat`; that is a
bootstrap expectation, not a version to set by hand.

## Normal release flow

1. A focused pull request uses a Conventional Commit title, passes required CI,
   and is squash-merged to `main`.
2. Release Please opens or updates its release pull request. It owns the version
   in `pyproject.toml`, generated release notes in `CHANGELOG.md`, and the
   automated `uv.lock` refresh; contributors do not make a separate version
   bump for ordinary changes.
3. A human reviews that release pull request and its required CI, then
   squash-merges it.
4. Release Please creates the `vX.Y.Z` tag and its GitHub Release.
   The release workflow rebuilds and verifies the wheel and source archive,
   uploads them with `SHA256SUMS`, and uploads an SBOM when one is produced.

No PyPI publishing occurs. The tracked workflow is the authority for the exact
assets and checks.

## Release verification evidence

Release review has three deliberately separate evidence tiers:

- **Required CI:** the PR checks configured by GitHub, including `just check`,
  the hermetic MCP contract job, and release-artifact/version validation where
  applicable. CI has no personal credentials, installed daemon, or configured
  backend.
- **Advisory local checks:** repeatable checks that can inform review but are
  not an additional GitHub merge gate, such as `just hygiene`, disposable
  loopback harnesses, and a local wheel inspection.
- **Required local receipt when applicable:** machine-specific checks that CI
  cannot prove: daemon lifecycle, installed-service behavior, client
  registration, configured backends, and authenticated integrations. Run these
  only with authorization for that machine and record a redacted receipt in the
  release-acceptance Issue.

The supported runtimes are CPython 3.12 and 3.13. `requires-python` and CI
intentionally match those tested minors. A later Python minor is not supported
merely because a resolver can produce a lockfile for it; add it only with
explicit CI coverage and an accompanying compatibility decision.

## One-time GitHub setup

The automation uses a GitHub App token so the release pull request can trigger
the repository's normal pull-request CI. Configure a GitHub App **only for this
repository** with these repository permissions:

| Permission | Access |
| --- | --- |
| Contents | Read and write |
| Pull requests | Read and write |
| Issues | Read and write |

Install the App on `voidfreud/mcp-gateway` only. Set its ID as the Actions
variable `RELEASE_PLEASE_APP_ID` and its private key as the Actions secret
`RELEASE_PLEASE_APP_PRIVATE_KEY`. Do not substitute a personal access token.
The workflows fail closed if either value is absent.

This guide describes the required setup; it does not claim that the repository
settings are already configured. Confirm them in GitHub before relying on a
release run.

GitHub's repository-level **Immutable Releases** setting is a separate GitHub
policy. The workflow neither enables nor verifies it. Enable and verify that
setting in GitHub if platform-enforced release immutability is required; do not
infer it from this repository's workflow alone.

## Corrections and failures

Before the release pull request is merged, close or rework it when its version,
notes, lock refresh, or CI result is wrong. Do not merge until the lock refresh
and required checks pass.

GitHub Actions events created with a workflow's default `GITHUB_TOKEN` do not
trigger downstream workflows. If a merge to `main` produces neither the
expected check nor a Release Please run, treat it as a missing workflow trigger:
investigate the event and authentication path, then merge a normal reviewed,
non-release documentation or CI follow-up using eligible human,
personal-access-token, or GitHub App authentication. Do not synthesize tags or
releases, and do not use the artifact-repair workflow for a missing release
trigger.

After a tag and GitHub Release exist, never retag, delete, or rewrite normal
release history. For an artifact-upload failure only, use the documented manual
`workflow_dispatch` repair path. It checks the existing published release at
the selected tag and SHA, verifies the rebuilt checksums, skips byte-identical
assets, uploads only missing approved asset names, and fails rather than
overwriting a byte-mismatched or unexpected existing asset. For any code,
metadata, or release-note correction, ship a new corrective SemVer release
instead. If a Release Please label becomes stale, close the obsolete release
pull request or remove the stale release label and let the automation propose
the current release again; investigate workflow logs and repository setup before
manually editing release files.

Attestation, provenance, and broader security policy are tracked in Issue
[#201](https://github.com/voidfreud/mcp-gateway/issues/201). Final release
acceptance is tracked in Issue
[#212](https://github.com/voidfreud/mcp-gateway/issues/212).

## Installing a private release

Use a GitHub account authorized for this private repository, then download a
specific release and verify it before installing its wheel:

```bash
gh auth login
gh release download vX.Y.Z --repo voidfreud/mcp-gateway --dir mcp-gateway-vX.Y.Z
cd mcp-gateway-vX.Y.Z
shasum -a 256 -c SHA256SUMS
uv tool install --reinstall ./mcp_gateway-*.whl
mcp-gateway --version
```

Replace `vX.Y.Z` with the latest GitHub Release tag after reviewing its notes.
On systems without `shasum`, use an equivalent SHA-256 checker. This is the
stable installation path. A tag-pinned Git install is only a developer
convenience, not a release artifact:

```bash
uv tool install "git+https://github.com/voidfreud/mcp-gateway@vX.Y.Z"
```

For foreground development, clone the repository, prepare its locked
environment, and run from the checkout instead of treating `main` as a stable
release channel:

```bash
gh auth login
gh auth setup-git
git clone https://github.com/voidfreud/mcp-gateway
cd mcp-gateway
uv sync --locked
uv run mcp-gateway
```
