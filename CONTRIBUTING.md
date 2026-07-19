# Contributing to mcp-gateway

The repository's [project policy](docs/project-policy.md) is authoritative for
planning, validation, security, decisions, merging, and releases. This guide
turns that policy into the everyday contribution flow.

## Set up a development checkout

Use Python 3.12 or later and install [uv](https://docs.astral.sh/uv/). From a
clone, synchronize the locked development environment:

```bash
uv sync --locked
just check
```

Use `just` to list available commands. The usual development commands are:

```bash
just check                 # lint, format check, tests, import smoke
uv run mcp-gateway         # run a development gateway
just verify                # validate an applicable running gateway
```

Read [installation](docs/installation.md) and [operations](docs/operations.md)
before installing, updating, restarting, or removing the macOS service. Do not
use a feature checkout as a deployment target.

## From Issue to merged change

1. Open or adopt a GitHub Issue before work begins. Include outcome, scope,
   acceptance criteria, risks, and required validation. Use an Issue for
   proposals, deferrals, parked work, and decisions as well as defects and
   features.
2. Create a short-lived branch from current `main`, named with the Issue and a
   concise subject, for example `codex/200-governance` or
   `fix/317-ready-timeout`.
3. Make the smallest safe change. Keep tests, user/operator documentation, and
   durable accepted decisions current when the behavior or constraint changes.
4. Open a pull request that links the Issue, uses one conventional title, and
   completes the repository pull-request template. The title proposes release
   classification: `fix:` for a patch, `feat:` for a minor release, and
   `feat!:` or `BREAKING CHANGE:` for a major release.
5. Run the applicable validation tiers, record the evidence, resolve review
   feedback, and squash-merge only after required checks and protected-branch
   rules pass. Do not develop directly on `main`.

Release automation is a target, not the current workflow. Until it is delivered,
do not create a `v*` tag without an approved release-acceptance Issue and
verified version metadata. See the [release policy](docs/project-policy.md#release-policy)
for the full interim rule.

## Validation evidence

Required CI covers reproducible, hermetic checks. The current mandatory CI
surface is both `check` and `mcp-contract` in `.github/workflows/check.yml`;
treat both as mandatory even while GitHub branch protection has not been
verified. Enforcing them through branch protection is separate work. Advisory
CI is reviewed but does not block until its signal is mature. Local/live
validation is required when a change affects a real installation, daemon or
service manager, configuration migration, credentials, client registration,
configured backend, or another machine-specific integration. One tier never
substitutes for another.

Link a redacted local/live receipt from the pull request or release Issue; do
not commit it. For example:

```text
Date: <YYYY-MM-DD>
Revision: <commit SHA>
Platform: macOS 15, Python 3.12
Scenario: Path A update and Claude Code reconnect
Procedure: just update; curl -fsS http://127.0.0.1:9100/health; reconnect client
Result: pass — health and readiness succeeded; updated tool metadata was visible
Limits: no external production backend was exercised
```

Redact tokens, private URLs, personal paths, private backend names when needed,
and customer or tool-output data. State why a normally relevant scenario is not
applicable; never imply that CI verified personal daemon state, credentials, or
real integrations.

## Documentation, security, and completion

Keep the README and affected [user documentation](AGENTS.md#user-documentation)
accurate. Do not add session diaries, personal machine state, untracked
backlogs, or secret-bearing examples to repository guidance. Put deferred work
and decision discussion in GitHub Issues; summarize only accepted durable
constraints in `docs/decisions/` as the policy directs.

Never include suspected vulnerabilities, credentials, private endpoints, or
exploit details in public Issues, pull requests, commits, CI logs, or receipts.
Follow [SECURITY.md](SECURITY.md), [the security guide](docs/security.md), and the policy's
[security and disclosure rules](docs/project-policy.md#security-and-disclosure).
For an active-harm or time-sensitive security exception, use only the narrow
[emergency bypass](docs/project-policy.md#emergency-bypass), record the reason
as soon as disclosure permits, and follow with a corrective pull request.

Before requesting merge, use this checklist:

- [ ] Linked Issue acceptance criteria are met; risks and follow-ups are linked.
- [ ] Both `check` and `mcp-contract` CI jobs passed; advisory findings are addressed, justified, or tracked.
- [ ] Applicable local/live validation has a redacted receipt.
- [ ] Tests, compatibility or migration notes, and user/contributor docs are current.
- [ ] Security and secret-handling expectations were reviewed.
- [ ] The pull request has the correct conventional title and is ready to squash merge.
