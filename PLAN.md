# Reset plan

The working record of the 2026-09 reset: what was decided, why, and in what
order the work lands. Stages are tracked as GitHub issues #308 to #314; this
file is the single place decisions are written down. Delete it when Stage 6
closes.

## Where it stood on 2026-09-01

| Measure | Value |
| --- | --- |
| Source lines (`src/`) | 18,603, of which 2,749 are one HTML file |
| Test lines | 19,556 in 28 files; 4 files over 2,800 lines each |
| Docs and repo prose | 5,963 lines across 33 files |
| Source files over 400 lines | 12 of 26 |
| Longest functions | `backend_routes` 404, `virtual_routes` 379, `_build_app` 372 |
| Runtime dependencies | 4 direct, 105 packages locked |
| Issue numbers cited in source comments | 285 |
| Tests that verify repo tooling, not the product | about 1,600 lines in 8 files |

The suite was green (1,152 tests). The product worked as long as its network
did not change. The problems were structural: three control surfaces (admin
API, CLI, dashboard) each grew their own validation and formatting; routes
and CLI commands were registered inside 200 to 400 line closures; config
schema, storage, transforms, and byte-cap policy shared one 2,470 line
module; and the repository carried a second product's worth of release,
audit, skill, and receipt tooling.

### The restart bug

When a backend cannot be reached, `_mount_backend` logs a warning and returns
`False`, the backend's runner exits, and nothing ever tries again. A warm
session that dies mid-call triggers one recycle, but a recycle inside the
30-second cooldown is skipped and a recycle whose reconnect fails leaves the
endpoint absent for good. A VPN toggle therefore strands every remote backend
until the daemon restarts. Fixed in Stage 0 by giving the runner an owned
reconnect loop with backoff and honest readiness.

## Decisions

Decided on 2026-09-01 and 2026-09-02. Change a decision by editing this
table, not by drifting.

| # | Decision | Why |
| --- | --- | --- |
| D1 | mcp-gateway is a local MCP proxy: per-backend endpoints, metadata overrides, resilient connections, controlled from a CLI, with a secondary dashboard. | Doing these completely beats doing twenty things halfway. |
| D2 | FastMCP is the base and is extended, never forked or imitated. Every FastMCP touchpoint lives in one adapter module; each private seam used is listed there and guarded by a test; FastMCP stays exact-pinned with a scheduled newest-version canary. | Staying upgradable and MCP-compliant without accidental drift was the project's biggest fear. |
| D3 | One core. Every operation is defined once (parameters, validation, handler). The HTTP API is generated from that definition; the CLI and the dashboard are thin clients of the API. The dashboard shows the CLI equivalent of each action. | Two control surfaces with their own logic is redundancy by definition. |
| D4 | Quiet by default. No timers run unless configured. Catalog refresh happens on mount, on a backend's own change notification, and on a manual button; an optional sweep cannot be set below 15 minutes. Logging records lifecycle and errors at the default level, debug is opt-in, rotation is bounded, an idle gateway writes nothing. | A resident process must not spend the host's disk or attention on itself. Staleness of minutes is acceptable; churn is not. |
| D5 | Keep: proxy endpoints, tool and instruction overrides, warm versus stateless sessions per backend, catalog capture, byte caps, settings import and export, per-backend `auth = "oauth"` passthrough to FastMCP's client, macOS service, health and readiness, admin bearer token, structured logging (shrunk). | Each has a user today. |
| D6 | Cut: Virtual Tools (parked in #315), behavior hooks, resource and prompt overrides, self-update, daily update check, the gateway-side OAuth resource server, SSE log streaming. | Unused, unfinished, or belonging to a different product. |
| D7 | Byte caps truncate at broadcast and never reject a save. The dashboard shows a counter against the effective cap and highlights over-limit text; the CLI warns. Caps stay adjustable per gateway and per backend because harnesses differ. | The cap is a visual guide for the author, not a validation gate. |
| D8 | Leave PyPI: delete the project there. Installation is `uv tool install` from this repository; publishing workflows are removed. Releases are git tags. | The package is not in a shape to be public, nobody depends on it, and updates should come from the repository. |
| D9 | Size limits: 300 lines per source file with a hard stop at 400, 400 per test file, 50 statements per function. Enforced now as a ratchet (new files comply, touched files do not grow) and as a hard CI gate after Stage 3. | Size is the leading indicator of tangled design. The rule must apply to the refactor itself. |
| D10 | Rules that a machine can check live in CI, not in prose. `AGENTS.md` is rewritten to under 60 lines; `CLAUDE.md` becomes a symlink to it. | The more instructions, the fewer are followed. |
| D11 | Test toolkit: pytest, ruff with a broad rule set, ty. Drop hypothesis, jsonschema, the Node conformance runner, and other single-use dev dependencies. Security audit stays as one CI step. | One way to test, fully used, beats five partially used. |
| D12 | Remove repository tooling that verifies process rather than product: release contract, CI receipts, resource measurement, live rename check, the tool-design skill and corpus, the audit report, the decision records, Release Please. | Ceremony without a reader. |
| D13 | Dashboard scope: status, catalog with inline edits, statistics, settings (caps, warm versus stateless, import and export), recent errors, manual refresh, one-click restart with no modal, a redesigned add-backend flow. One small HTML file, one CSS file, JS modules, no framework, no build step. | Secondary to the CLI, but it must look and feel finished. |
| D14 | `justfile` stays until the CI rewrite in Stage 1, then shrinks to the recipes CI calls or is dropped. | It lost its meaning but CI still depends on it. |
| D15 | Root directory target: `src`, `tests`, `docs`, `.github`, `README.md`, `AGENTS.md`, `PRINCIPLES.md`, `LICENSE`, `pyproject.toml`, `uv.lock`, `config.example.toml`. Tool caches are redirected into one ignored directory where the tool allows it. | The root should look as simple as the product is. |
| D16 | The reset ships as 2.0.0, a git tag, with a short migration note for removed config keys. | It is a breaking change and should say so. |
| D17 | Work lands on `main` directly during the reset; no pull requests. CI must still be green on every push. | Speed. The PR ceremony returns, if at all, after Stage 6. |
| D18 | Dashboard statistics show calls, errors, and latency per tool and per backend, plus each backend's last refresh time. Nothing is sampled on a timer; counters are kept in memory by the daemon and reset on restart. | Enough to see what is used and what is failing, without a metrics store. |
| D19 | The pre-reset branch with soft byte caps is deleted. Stage 4 implements D7 fresh in the restructured code. | A patch against the old layout would be rewritten anyway. |

## Open questions

None. Add one here the moment it appears; answer it by adding a decision.

## Stages

Each stage is a GitHub issue with a checklist and a definition of done. A
stage ends when its checklist is complete and CI is green on `main`.

| Stage | Issue | Outcome |
| --- | --- | --- |
| 0 Stabilize | #308 | CI green, reconnect bug fixed, publishing removed, size ratchet on |
| 1 Cut | #309 | Every rejected feature and tool gone, CI reduced to lint, types, tests, build |
| 2 One core | #310 | Operations defined once; API, CLI, dashboard are thin; FastMCP adapter isolated |
| 3 Restructure | #311 | Module layout below, every file under the limit, size gate required |
| 4 Harden | #312 | Reconnect, warmth, quiet refresh, resource ownership, edge-case matrix |
| 5 Dashboard | #313 | The D13 dashboard, finished |
| 6 Document | #314 | README, five doc pages, AGENTS.md, root layout, tag 2.0.0 |

### Target module layout (Stage 3)

```text
src/mcp_gateway/
  __main__.py          entry point
  config/
    schema.py          pydantic models, validation
    store.py           read, write, backup, migrate
  core/
    operations.py      every operation defined once (D3)
    fastmcp.py         the only module that imports FastMCP internals (D2)
  proxy/
    mount.py           build and mount one backend
    lifecycle.py       runner, reconnect loop, recycle
    transforms.py      tool and instruction rewrites, byte caps
  admin/
    api.py             HTTP routes generated from operations
    state.py           the state and statistics view
  cli/
    main.py            argument parsing generated from operations
    client.py          admin API client
    output.py          human and JSON formatting
  service/
    launchd.py         install, status, uninstall
  web/
    index.html  app.css  modules/*.js
  logging.py  runtime.py
```

## Definition of done for the reset

- `PRINCIPLES.md` holds in every file, enforced by CI where a machine can
  check it.
- A stranger can install from the repository, add a backend, rename a tool,
  and see it in their client by following the README alone.
- The gateway survives a VPN toggle, a laptop sleep, and a backend restart
  without intervention, and writes nothing to disk while idle.
- FastMCP can be upgraded by changing one pin and running the suite.
