# Releases

This is the canonical release process for mcp-gateway. Every release publishes
the `mcp-local-gateway` distribution to public PyPI through OIDC Trusted
Publishing and retains checksummed artifacts in a public GitHub Release. For
verification tiers and the redacted local receipt, see
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
4. Release Please creates the `vX.Y.Z` tag and GitHub Release. The release
   workflow rebuilds and verifies one wheel plus one source archive, uploads
   them with `SHA256SUMS` and an SBOM when available, then publishes exactly
   those wheel/source artifacts to PyPI through short-lived OIDC.

The `mcp-local-gateway` distribution exposes the unchanged `mcp-gateway`
command and `mcp_gateway` import. There is no long-lived PyPI API token in
GitHub; the pinned publisher action receives `id-token: write` only in its
dedicated `pypi` environment job. Manual artifact-repair dispatches never
publish to PyPI.

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

The release job revokes its broad release token before dependency resolution.
It uses a separate read-only token for the exact PR checkout, performs the
lockfile refresh with no repository credential, then mints a contents-write
token only for the validated push and revokes it immediately afterward.

### PyPI Trusted Publisher

Create the public PyPI project `mcp-local-gateway` with a pending Trusted
Publisher before the first release. In the PyPI publishing form, use:

| Field | Value |
| --- | --- |
| PyPI project | `mcp-local-gateway` |
| GitHub owner | `voidfreud` |
| Repository | `mcp-gateway` |
| Workflow | `release-please.yml` |
| Environment | `pypi` |

The repository must also have a GitHub Actions environment named `pypi`.
Environment reviewers are optional; add them if releases require an explicit
human deployment approval. Do not configure a `PYPI_TOKEN` secret. After the
first successful OIDC publication converts the pending publisher into the live
project, verify the project name, release files, hashes, metadata, and Trusted
Publisher association on PyPI.

GitHub's repository-level **Immutable Releases** setting is a separate GitHub
policy. The workflow neither enables nor verifies it. Enable and verify that
setting in GitHub if platform-enforced release immutability is required; do not
infer it from this repository's workflow alone.

## Build contract

Release jobs use the repository-pinned `uv` version and the exact
`hatchling` version in `uv.lock`. After `uv sync --locked`, the release contract
builds offline with `--no-build-isolation`, inspects wheel and sdist metadata,
clean-installs the wheel, exports the SBOM, and only then writes checksums. A
toolchain change therefore requires a reviewed lock refresh and the same full
gate as application code.


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
release history. For a GitHub artifact-upload failure only, use the documented
manual `workflow_dispatch` repair path. It checks the existing published release
at the selected tag and SHA, verifies rebuilt checksums, skips byte-identical
assets, uploads only missing approved names, and fails rather than overwriting a
byte-mismatched or unexpected asset.

The repair dispatch deliberately does not publish to PyPI. If the initial PyPI
job failed before upload, correct its environment/Trusted Publisher setup and
rerun that failed workflow job. For code, metadata, release-note, or already
published package corrections, ship a new corrective SemVer release. If a
Release Please label becomes stale, close the obsolete release pull request or
remove the stale release label and let automation propose the current release;
investigate workflow logs and repository setup before manually editing release
files.

Security reports use the repository's private vulnerability-reporting form; see
[the security policy](../.github/SECURITY.md). Release hardening that is not
implemented belongs in a current GitHub issue rather than this guide.

## Installing a release

The normal path needs no GitHub account or repository access:

```bash
uv tool install mcp-local-gateway
mcp-gateway --version
```

For an exact update or rollback after installation:

```bash
mcp-gateway update --version X.Y.Z
```

The selected gateway version is exact, but `uv` resolves its compatible
dependencies from public PyPI when installing. Release checksums cover the
published gateway artifacts, and the SBOM records the release build
environment; neither is a lockfile for every future public installation.

The public GitHub Release remains a verifiable fallback. Authenticate the
GitHub CLI, download the chosen release, and verify every asset:

```bash
gh auth login
gh release download vX.Y.Z --repo voidfreud/mcp-gateway --dir mcp-gateway-vX.Y.Z
cd mcp-gateway-vX.Y.Z
shasum -a 256 -c SHA256SUMS
uv tool install --reinstall ./*.whl
mcp-gateway --version
```

On systems without `shasum`, use an equivalent SHA-256 checker. A tag-pinned Git
install is a developer convenience, not a release artifact:

```bash
uv tool install "git+https://github.com/voidfreud/mcp-gateway@vX.Y.Z"
```

For foreground development, clone the public repository, prepare its locked
environment, and run from the checkout instead of treating `main` as a stable
release channel:

```bash
git clone https://github.com/voidfreud/mcp-gateway
cd mcp-gateway
uv sync --locked
uv run mcp-gateway
```
