# mcp-gateway contributor manual

## Purpose and boundaries

mcp-gateway is a local MCP gateway. It presents independent backend endpoints
to MCP clients, improves their broadcast metadata, and forwards tool calls.
The product is client-neutral: Claude Code and Codex are supported clients, not
the product boundary. Keep changes small, reviewable, and compatible with the
documented public behavior.

This file is the canonical manual for people and coding agents working in this
repository. Read it before changing the project. Do not copy its policy into
tool-specific instruction files.

## Where truth lives

| Topic | Source of truth |
| --- | --- |
| Product promise and first use | [README.md](README.md) |
| Installation, configuration, operations, security, admin UI, and HTTP API | [docs/](docs/) |
| Repository commands | [justfile](justfile) |
| Package metadata and current version | [pyproject.toml](pyproject.toml) |
| Release history | [CHANGELOG.md](CHANGELOG.md) |
| Automated checks and releases | [.github/workflows/](.github/workflows/) |
| Accepted architecture decisions and their process | [docs/decisions/](docs/decisions/README.md) |
| Advertised MCP surface and safe gateway tuning | [.agents/skills/mcp-tool-design/SKILL.md](.agents/skills/mcp-tool-design/SKILL.md) |
| Example configuration | [config.example.toml](config.example.toml) |
| Verified behavior | [tests/](tests/) and the implementation in [src/](src/) |

When these disagree, do not guess. Treat the implementation, tests, and
automation as evidence of current behavior; correct the documentation in the
same pull request or open an issue when the intended behavior is unclear.

## Start every session

1. Read this file and the documentation that owns the area you will change.
2. Inspect the checkout with `git status --short --branch` and do not overwrite
   unrelated work.
3. Prepare the locked environment with `uv sync --locked`.
4. Check the relevant tests, automation, and open GitHub issues before choosing
   a solution. Run `just check` before handing off a code change unless a
   narrower failure is being investigated first.

## Scope, issues, and decisions

Work only on the requested problem and its necessary tests and documentation.
Prefer a clear module boundary and a small interface over a broad rewrite.
Remove obsolete code only when its ownership and callers are understood; do not
hide uncertainty with scaffolding, compatibility shims, or speculative
abstractions.

GitHub Issues are the tracker for bugs, deferred work, parked features,
follow-ups, questions, and proposed decisions. Open or use an issue before
parking work; link the pull request to the issue it resolves. Do not leave
private backlogs, durable TODOs, or decision records in ad-hoc notes. A
decision is not settled merely because code exists. Follow the canonical
[Issue-to-ADR process](docs/decisions/README.md): discuss and defer decisions
in Issues, then add or update an ADR only when a decision is accepted.

## Change and review workflow

1. Start from current `main` and create one focused branch.
2. Make an intentional, scoped change with tests and documentation that match
   the observable behavior.
3. Run the applicable local verification and review `git diff --check` plus
   `git diff` for accidental files or secrets.
4. Push the branch and open a pull request. Every contribution goes through a
   PR; do not push directly to `main`.
5. Wait for required CI checks and review feedback. Resolve both before the PR
   is ready.
6. Squash-merge the approved PR, then deploy only through the documented
   deployment path.

## Verification layers

Use the layer that matches the risk. CI is intentionally hermetic: it runs the
repository’s automated checks without personal accounts, secrets, installed
services, or live backends. It cannot prove machine-specific daemon behavior.

- `just check` is the normal local and CI gate: lint, formatting, tests, and
  import smoke.
- `uv run python tests/live/run_mcp_wire.py` and
  `uv run python tests/live/run_virtual_tools.py` start disposable loopback
  fixtures and an isolated gateway process. They are safe to use for their
  covered integration contracts and do not use the installed daemon.
- `just verify` targets the running local gateway and its live backends. Run it
  only when the requested change needs that receipt and only with authorization
  to exercise that machine and its configured services. It is a local
  complement to CI, not a CI substitute.

## Local state, secrets, and destructive commands

Keep personal configuration, credentials, local service state, caches, tool
indexes, generated files, and machine-specific tooling untracked. Never add a
secret, access token, absolute personal path, or captured live-service data to
the repository. Use documented environment references for secrets and redact
them from logs, tests, issues, commits, and pull requests.

Treat commands that install, update, restart, stop, uninstall, purge, or
otherwise change a daemon, service, configuration, or personal backend as
stateful. `just install`, `just update`, `just restart`, `./install.sh`, and
any uninstall or purge command require the user’s explicit authorization for
the exact target. Prefer documented dry runs where available. Do not broaden
that authorization to another machine, account, backend, or data directory.

## Documentation and release discipline

Documentation is part of the product. Update the owning README or docs page
with any user-visible, operational, configuration, security, or API change;
update tests when behavior changes; and remove stale statements rather than
preserving historical clutter. Keep this manual focused on workflow, not a
second architecture reference.

Do not invent version numbers, release tags, changelog conventions, or release
commands. Follow the version and release automation currently documented in
the repository when a release change is explicitly in scope, and update the
sources that own that automation together.

## Handoff checklist

Before requesting review, confirm that the branch is scoped, the linked issue
is accurate, relevant documentation and tests are current, required checks have
run, `git diff --check` is clean, and no local state or secrets are included.
State what was verified, what intentionally was not verified, and why.
