# Reset plan

A living document for the 2026-09 reset. It records where the project stands,
what must be decided, and the order of work. Delete it when the last phase
ships; it is not reference documentation.

## Where it stands (2026-09-01)

| Measure | Value |
| --- | --- |
| Source lines (`src/`) | 18,603, of which 2,749 are one HTML file |
| Test lines | 19,556 in 28 files; 4 files are over 2,800 lines each |
| Docs and repo prose | 5,963 lines across 33 files |
| Source files over 400 lines | 12 of 26 |
| Longest functions | `backend_routes` 404, `virtual_routes` 379, `_build_app` 372 |
| Runtime dependencies | 4 direct, 105 packages locked |
| Issue numbers cited in source comments | 285 |
| Tests that verify repo tooling, not the product | about 1,600 lines in 8 files |
| Open issues | 5, all epic-sized, batch-created 2026-08-04 |
| CI | security workflow red on every PR since 2026-08-16 (pip advisory) |

The suite is green (1,152 tests). The product works when its network does
not change. The problems are structural: three control surfaces (admin API,
CLI, dashboard) each grew their own validation and formatting; routes and CLI
commands are registered inside 200 to 400 line closures; config loading,
transforms, and byte-limit policy share one 2,470 line module; and the repo
carries a second product's worth of release, audit, skill, and receipt
tooling.

### The restart bug

When a backend cannot be reached, `_mount_backend` logs a warning and returns
`False`, the backend's runner exits, and nothing ever tries again. A warm
session that dies mid-call triggers one recycle, but a recycle inside the
30-second cooldown is skipped and a recycle whose reconnect fails leaves the
endpoint absent for good. A VPN toggle therefore strands every remote backend
until the daemon restarts. The runner needs an owned reconnect loop with
backoff, a periodic liveness probe on warm sessions, and readiness that
reports "reconnecting" honestly. This is the first code change of the reset.

## Decisions to make

Each item has a recommendation. Nothing below is started until it is decided.

1. **Product scope.** Keep: per-backend proxy endpoints, tool and instruction
   overrides, connection resilience, CLI control, macOS service, health and
   readiness. Recommend cutting or parking: Virtual Tools (about 2,000 lines
   plus a fifth of the tests), behavior hooks (user Python run inside the
   daemon), resource and prompt overrides, metadata byte limits, settings
   import and export, in-daemon self-update (`uv tool upgrade` already does
   it), and the SSE log viewer. Keep OAuth only if a backend you use needs it.
2. **Size limits.** Recommend 300 lines per source file with a hard CI stop at
   400, 400 per test file, 50 statements per function. Enforced by one small
   test plus existing ruff rules.
3. **Dashboard.** CLI first. Recommend a read-mostly dashboard: status, tool
   catalog with inline edits, logs. It is one small HTML page plus a CSS file
   and JS modules, each under the limit, and it calls the same admin API as
   the CLI. Client registration, settings bundles, and Virtual Tool editing
   do not return.
4. **Repository tooling.** Recommend removing `tools/release_contract.py`,
   `tools/ci_receipts.py`, `tools/measure_service_resources.py`,
   `verify_rename.py`, the `mcp-tool-design` skill and its `corpus/`, the
   official Node conformance runner, `docs/audit-report.md`, and the eight
   tests that cover them. CI becomes lint, tests, and build.
5. **justfile.** Recommend keeping a ten-line version (`check`, `test`,
   `lint`) or dropping it entirely in favor of documented `uv run` commands.
   Removing it today breaks CI, so it goes with the CI rewrite in Phase 1.
6. **Decision records.** Recommend deleting the ten ADRs and the process
   around them. Decisions live in this plan while the reset runs, then in
   short "Design" sections of the owning doc page.
7. **The uncommitted work.** Branch `wip/soft-metadata-limits` holds the
   change that was sitting uncommitted on `main` (soft byte limits with red
   highlighting). If metadata limits are cut under item 1, delete the branch.
8. **Open issues.** Recommend closing #285 (client registration) as out of
   scope and closing #287, #288, #289, #290 in favor of this plan, which
   supersedes them.
9. **Versioning.** The reset is a breaking release: 2.0.0 on PyPI, with a
   short migration note for the config keys that disappear.

## Phases

Each phase is a small number of pull requests. A phase ends when its
definition of done is met and CI is green.

### Phase 0: stabilize (now)

- Merge `fix/ci-pip-audit`, then the two Dependabot PRs.
- Fix the reconnect bug described above, with tests for: backend down at
  boot, backend dies mid-call, backend dies while idle, backend returns.
- Add the file-size gate in advisory mode so the baseline is visible.
- Done when: CI green, a VPN toggle no longer needs a restart.

### Phase 1: cut

- Remove every feature and tool rejected in the decisions above, with their
  tests, config keys, docs, and CLI commands.
- Rewrite CI to lint, test, build.
- Delete `docs/audit-report.md`, `docs/decisions/`, `CHANGELOG.md` history
  older than the last release, and all doc sections describing removed
  features.
- Done when: no reference to a removed feature remains anywhere in the tree.

### Phase 2: restructure

Split the surviving code into the layout below. Each module has one job and
sits under the limit. Nothing is renamed by search and replace; use the graph
tools to move symbols with their callers.

```text
src/mcp_gateway/
  __main__.py          entry point
  config/
    schema.py          pydantic models, validation
    store.py           read, write, backup, migrate
  proxy/
    mount.py           build and mount one backend
    lifecycle.py       runner, reconnect loop, recycle
    transforms.py      tool and instruction rewrites
  admin/
    api.py             HTTP routes (thin: parse, call core, respond)
    edits.py           apply overrides to a config (shared by API and CLI)
    state.py           build the state view
  cli/
    main.py            argument parsing
    client.py          admin API client
    backend.py  tool.py  service.py  logs.py
    output.py          human and JSON formatting
  service/
    launchd.py         install, status, uninstall
  web/
    index.html  app.css  app.js (plus modules as needed)
  logging.py  runtime.py  oauth.py (if kept)
```

- Done when: every file is under the limit, the size gate is required, no
  function is over 50 statements, and the backend-lookup idiom exists once.

### Phase 3: harden

- Resource ownership audit: every connection, subprocess, task, subscriber,
  and cache has a close path and a test that proves it does not grow.
- Edge-case matrix per feature: unreachable backend, slow backend, malformed
  responses, config edited while running, concurrent clients, restart during
  a call. Each cell is a test.
- Done when: the matrix is complete and the suite runs in under a minute.

### Phase 4: re-document

- README: what it is, install, first backend, first override, in under five
  minutes of reading.
- `docs/`: one page each for configuration, CLI reference, dashboard,
  service, security. Nothing else.
- Done when: every doc statement is verifiable against the current code and
  no page exceeds 300 lines.

### Phase 5: release 2.0

- Tag, publish to PyPI, update the install path in the README.
- Delete this file.

## Definition of done for the reset

- `PRINCIPLES.md` holds in every file, enforced by CI where a machine can
  check it.
- A stranger can install, add a backend, rename a tool, and see it in their
  client by following the README alone.
- The gateway survives a VPN toggle, a laptop sleep, and a backend restart
  without intervention.
