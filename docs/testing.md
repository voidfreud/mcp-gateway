# Testing and verification

This guide defines what a green check means—and what it does not—for
mcp-gateway. It complements the release process in [releases.md](releases.md).

## Supported runtime

The supported and CI-tested runtimes are **CPython 3.12 and 3.13**. The package
metadata intentionally restricts the project to those minors until another
minor has explicit CI coverage. A dependency resolver accepting another version
is not support evidence.

## Verification tiers

### Required CI

GitHub must pass the configured pull-request checks before merge. The normal
quality gate is `just check`: Ruff lint and formatting, unit/property tests,
import smoke, and the local release-contract build. The hermetic MCP contract
job uses disposable fixtures. These checks may run in a clean environment, but
they do not use personal accounts, secrets, a running daemon, or live backends.

### Advisory local checks

Run the narrowest repeatable command that matches the change before review:

```bash
just hygiene
just docs-check
just check
uv run python tests/live/run_mcp_wire.py
uv run python tests/live/run_virtual_tools.py
```

`just hygiene` protects the tracked review tree from local-only tooling, cache,
and secret material. The loopback launchers are disposable integration checks;
they do not exercise the installed daemon. `just types` records the complete,
unsuppressed `ty` baseline but is deliberately advisory and non-blocking: it
reports the checker output, then returns success so it cannot be mistaken for a
required gate. It remains advisory until the tracked type-debt Issue closes and
the checker exits cleanly without exclusions or blanket suppressions.

`just docs-check` is deterministic and offline: it verifies tracked Markdown
links that point to repository-relative files or fragments. External URL
reachability is intentionally outside this command and remains an advisory,
manual review concern.

The scriptable `mcp-gateway` CLI is covered by the unit suite: command-tree
shapes, JSON-file and `-` stdin inputs, `--json` single-value output, stderr
errors with nonzero exit, and the never-prompt `--yes` guard. A live CLI
session against an installed daemon belongs in the release receipt when a
change touches the entry point or service lifecycle.

### Required local release receipt, when applicable

CI cannot validate a developer's machine. For a release that touches the
daemon, installer, credentials, or live backend behavior, obtain
authorization and attach a redacted receipt to the release-acceptance Issue.
Do not run stateful commands merely to fill out the receipt.

Use this concise format:

```text
Environment: macOS <version>; CPython 3.12; mcp-gateway <candidate SHA>
Scope: <what was authorized>; credentials/backends: <available | unavailable | not applicable>

- install/update/uninstall: PASS | FAIL | NOT RUN | NOT APPLICABLE — <redacted evidence>
- daemon lifecycle and /health,/ready: PASS | FAIL | NOT RUN | NOT APPLICABLE — <redacted evidence>
- live backend or harness integration: PASS | FAIL | NOT RUN | NOT APPLICABLE — <redacted evidence>

Secrets omitted: yes
Known limitations or follow-up Issue: <link or none>
```

Never include tokens, private endpoints, personal paths, raw configuration,
captured backend payloads, or other sensitive local state in an Issue, pull
request, commit, or release artifact.
