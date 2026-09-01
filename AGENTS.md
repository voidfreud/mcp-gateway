# Working in mcp-gateway

mcp-gateway is a local MCP proxy built on FastMCP. `PRINCIPLES.md` is the
rulebook; read it once. `PLAN.md` says what is being done now and why.

## Before changing anything

1. `git status --short --branch`. Do not touch unrelated work.
2. `uv sync --locked`.
3. Read the current stage issue named in `PLAN.md`.

## Rules a machine cannot check

- Reuse before writing. A copied block is a defect; extract it or call the
  original.
- Only the FastMCP adapter module imports FastMCP internals.
- No timers, polling, or background writes unless the plan asks for them.
- Comments say why. No issue numbers, no history, no restating the code.
- Delete what you replace: code, tests, docs, config keys, and CLI commands
  go together, in the same commit.
- A behavior change updates the owning doc page in the same commit.
- Prefer the standard library and the four runtime dependencies. Adding a
  dependency is a plan decision, not a convenience.

## Rules a machine checks

`just check` must pass before every commit: lint, format, tests, and, once
Stage 0 lands, the size limits (source files 300 lines with a hard stop at
400, test files 400, functions 50 statements). New files comply; touched
files may not grow.

## Landing work

- Commit to `main` directly with a Conventional Commit subject. CI must be
  green on every push.
- Never commit secrets, personal paths, local state, or generated files.
- Commands that install, restart, update, or uninstall the resident service,
  or change a configured backend, need the user's explicit say-so each time.

<!-- gitnexus:start -->
<!-- gitnexus:keep -->
## Code graph

GitNexus indexes this repository. Before moving, splitting, or renaming a
symbol, run `impact` on it and read the callers; rename through the tool, not
search and replace. If the index is stale, run
`node .gitnexus/run.cjs analyze` from the project root.
<!-- gitnexus:end -->
