# Principles

These rules govern every change to mcp-gateway. They are not aspirations; a
pull request that violates one is not ready. When a rule and a feature
conflict, the feature loses.

## 1. Do a few things completely

mcp-gateway is a local MCP proxy. It mounts each backend on its own endpoint,
lets the operator rewrite what that backend advertises, keeps the connection
alive through network changes, and is controlled from a CLI with a secondary
dashboard. That is the whole product. Anything else must earn its place by naming the person who needs it
today and the failure they hit without it.

- A feature nobody uses is deleted, not maintained.
- A feature that is used but half-working is finished or deleted. There is no
  third state.
- Saying no is the default. "It might be useful" is a reason to say no.

## 2. One source of truth for every behavior

Every rule, validation, lookup, and message exists in exactly one place and is
imported everywhere else. The admin API, the CLI, and the dashboard are three
views of one core; none of them re-implements what another already does.

- Copying a block of code is a defect. Extract it or call the original.
- Two functions that differ only in names are one function with a parameter.
- Documentation states each fact once, in the page that owns it, and links
  from everywhere else.

## 3. Small units, strict limits

Size is the leading indicator of tangled design, so it is enforced, not
advised.

| Unit | Limit |
| --- | --- |
| Source file (`src/`) | 300 lines, hard stop at 400 |
| Test file | 400 lines |
| Function or method | 50 statements, 4 nesting levels |
| Module | one responsibility, named by that responsibility |

A file that needs to grow past its limit is telling you it has two jobs. Split
it by responsibility, never by page count. The limits are checked in CI.

## 4. Nothing half-built

The repository holds only what ships or verifies what ships.

- No scaffolding, placeholders, `TODO`s, feature flags for unfinished work, or
  compatibility shims for callers that no longer exist.
- No dead code, no unused parameters, no unreachable branches. Coverage of a
  line is not the point; a line that no behavior needs is deleted.
- Every resource has an owner that closes it: connections, subprocesses,
  tasks, file handles, subscribers, caches. Growth without a bound is a bug.
- Comments explain why the code is the way it is. They do not cite issue
  numbers, retell history, or restate what the code already says. History
  lives in git.

## 5. Resilience is the product

A proxy that needs a restart is a broken proxy. The gateway must survive
backend restarts, laptop sleep, VPN and network changes, and slow or hung
backends without operator action.

- Every failure has a defined recovery: retry with backoff, degrade, or
  report. "Log and give up" is not a recovery.
- Every edge case that has been hit is written down as a test before it is
  fixed.
- Health and readiness always tell the truth about the current state.

## 6. Quiet by default

The gateway is a resident process on someone's machine. It does not poll,
sweep, rescan, or log unless something happened or the operator asked.

- No timer runs unless it is configured, and none can be set below fifteen
  minutes. Catalog refresh happens on mount, on a backend's own change
  notification, and on a manual request.
- Logging records lifecycle events and errors. Debug detail is opt-in,
  rotation is bounded, and an idle gateway writes nothing.
- Staleness measured in minutes is acceptable. Churn is not.

## 7. Built on FastMCP, never drifting from it

FastMCP is the base, and the gateway extends it rather than imitating it. The
MCP protocol is what the gateway is compliant with; FastMCP is how.

- Every FastMCP import lives in one adapter module. Nothing else in the tree
  touches FastMCP internals.
- Each private seam the adapter relies on is listed there with a test that
  breaks when FastMCP changes it.
- FastMCP is pinned exactly. A scheduled job tries the newest release so an
  upgrade is a pin change plus a green suite, never a surprise.

## 8. Dependencies are liabilities

Runtime dependencies are pinned, few, and each carries most of its weight.
Before adding one, prove the existing set cannot do the job. A dependency used
once, or replaceable by twenty lines of standard library, is not added. Dev
dependencies follow the same rule.

## 9. Tests verify the product

Tests exist to prove the gateway behaves; they are not a place to store
process. The suite covers the documented behavior, every failure path, and
every edge case that was ever a bug. It does not test the repository's own
release scripts, link checkers, skills, or ceremony. Test files obey the same
size and duplication rules as source.

## 10. Documentation describes now

The README is the front door and says what the product is, how to install it,
and how to use it in under five minutes. Reference pages describe current
behavior only. Nothing in `docs/` records history, audits, or plans that have
been executed; those belong in git, the changelog, and closed issues.

## 11. Ship like a product

The `main` branch is always releasable. Changes land as small commits with a
Conventional Commit title, green CI, and updated docs and tests in the same
change. Releases are git tags, installed with `uv tool install` from this
repository. The repository looks the
same to a stranger as it does to its author: no personal paths, local state,
generated artifacts, or tool residue.
