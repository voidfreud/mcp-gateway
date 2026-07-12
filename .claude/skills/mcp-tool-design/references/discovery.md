# What a cold agent sees — the Claude Code discovery model

Every tuning judgment is made from the seat of an agent that has never seen these tools. This file is what that agent actually sees, stage by stage, in Claude Code with tool search on (the default). Grounded in the live Claude Code docs, the corpus, and direct observation of a running session (2026-07-03).

## The four stages

**Stage 0 — session start, before any user text.** The system prompt already contains:
- each connected server's `instructions`, injected verbatim under an "MCP Server Instructions" heading (one block per server, truncated at 2 KB);
- the full definition — name, description, schema with every param description — of each pinned tool (`always_load` → `_meta["anthropic/alwaysLoad"]`);
- the bare callable names (`mcp__<server>__<tool>`) of every deferred tool, in a list, with no descriptions.

**Stage 1 — discovery.** When the task needs a capability, the agent calls `ToolSearch` with a query. The matcher searches tool names, descriptions, argument names, and argument descriptions, and returns the 3–5 most relevant tools, whose full definitions then load into context.

**Stage 2 — selection.** Among loaded tools, the agent picks by reading name + description (+ schema). If two loaded tools read as interchangeable, the pick is effectively chance.

**Stage 3 — invocation.** Arguments are constructed from the schema and each param's name + description.

**Persistence.** Server instructions live in the system prompt and survive compaction. Discovered tool definitions persist for the session but may be dropped at compaction — the agent then searches again.

## The tuning map

| Surface | Visible at | What it must accomplish alone |
|---|---|---|
| Server instructions | Stage 0, always | Route: exactly when to come to this server, when to go to a named sibling instead |
| Tool name | Stage 0 (bare, in the deferred list) + search match | Telegraph action + domain with no description in sight; carry search keywords |
| Tool description | Stage 1 (search corpus) + stage 2 (selection contract) | Match the task language of a search query; then make the pick and the when-not unambiguous |
| Param names + descriptions | Stage 3 (and stage 1 — args are searched too) | Make correct argument construction possible without guessing |

The asymmetry is the whole game: instructions and names are the only per-server persuasion an undecided agent ever sees. A brilliant description on an unconvincing name behind uninformative instructions is never read.

## Pinning (`always_load`)

Pinning exempts a tool from deferral: its full definition is resident from turn 0 of every session, whether or not it is ever used. That buys stage-0 visibility and skips the search round-trip, and costs context in every session (the ⚙ Gateway panel in `/admin` shows the byte footprint, all-tools vs eager-upfront). Claude Code's own registration config also takes a per-SERVER `alwaysLoad: true` (and a per-server `timeout` ms field) — coarser than the gateway's per-tool pin, which stays the right lever here. Pin per-tool, not per-backend, and only the tool an agent should reach for without searching — a server's primary entry point. A pinned tool's description is also the strongest place to steer stage-2 selection, since it is always loaded when competitors surface via search.

## When tool search is off

`ENABLE_TOOL_SEARCH=false` (or `auto` under the threshold, or unsupported environments) loads every definition upfront: no stage 1, everything at stage 0. Tune for the default-on world; text that is sharp per-stage stays sharp when the stages collapse.

## Hard limits (verified against live docs + corpus)

| Limit | Value |
|---|---|
| Server `instructions` truncation | 2 KB |
| Tool description truncation | 2 KB — front-load critical content |
| Tool name | `^[a-zA-Z0-9_-]{1,64}$` (Anthropic cap; the gateway validates the same set) |
| ToolSearch results | 3–5 tools per query; catalog max 10,000; unsupported on Haiku |
| Tool output | warns at 10k tokens; default cap 25k (`MAX_MCP_OUTPUT_TOKENS`); a tool's `_meta["anthropic/maxResultSizeChars"]` overrides the cap for its text content (per-tool, wins over the env var) |

## Sources

Live docs: `code.claude.com/docs/en/mcp.md`, `code.claude.com/docs/en/context-window.md` (append `.md` for clean markdown). Corpus: `corpus/docs/claude-code/claude-code-tool-search-guide.md`, `corpus/docs/anthropic/claude-platform-tool-search-tool.md`. Stage-0 composition confirmed firsthand in a live session (deferred names listed bare; gateway server instructions injected verbatim; pinned `ask_repo_questions` schema resident).
