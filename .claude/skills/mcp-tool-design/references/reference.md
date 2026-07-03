# MCP tool design — reference tables

## Contents
- [Tool annotations: semantics and defaults](#tool-annotations-semantics-and-defaults)
- [Tool object fields](#tool-object-fields)
- [Claude Code hard limits](#claude-code-hard-limits)
- [Optional tool properties (Claude API)](#optional-tool-properties-claude-api)
- [Tool search and defer_loading](#tool-search-and-defer_loading)
- [strict tool use](#strict-tool-use)
- [Prompts vs resources vs tools](#prompts-vs-resources-vs-tools)
- [The contested point: imperative language in descriptions](#the-contested-point-imperative-language-in-descriptions)
- [Evaluation and iteration](#evaluation-and-iteration)
- [Client-specific and workflow patterns](#client-specific-and-workflow-patterns)
- [Corpus source map](#corpus-source-map)

## Tool annotations: semantics and defaults

All four are hints — not guaranteed, and untrusted from an untrusted server. A client must never make a trust decision on an annotation from a server it doesn't trust. Defaults are deliberately pessimistic ("worst until told otherwise").

| Annotation | Meaning | Default | When true |
|---|---|---|---|
| `readOnlyHint` | Tool does not modify its environment | `false` | Client may skip confirmation / auto-approve |
| `destructiveHint` | If it writes, the change is destructive vs additive (meaningful only when `readOnlyHint` is false) | `true` | Client shows a warning/confirmation |
| `idempotentHint` | Repeated calls with same args have no additional effect (meaningful only when `readOnlyHint` is false) | `false` | Safe to retry on failure |
| `openWorldHint` | Tool touches an open world of external entities vs a closed domain | `true` | Scrutinize output as a trust-boundary crossing |
| `title` | Human-readable display name | — | Display only; no trust implication |

Combine `destructiveHint: true` with `idempotentHint: true` to say "dangerous but safe to retry." Real risk is a session property (private data + untrusted content + external comms), not capturable by any single tool's annotation.

## Tool object fields

| Field | Notes |
|---|---|
| `name` | Required, unique per server. Spec: 1–128 chars, `A-Z a-z 0-9 _ - .`, case-sensitive. Anthropic client cap: `^[a-zA-Z0-9_-]{1,64}$` → stay ≤64. |
| `title` | Optional human display name. |
| `description` | Human/LLM-readable behavior description (the firing predicate). |
| `inputSchema` | JSON Schema object (never null); defaults to draft 2020-12. No-arg tool: `{"type":"object","additionalProperties":false}`. |
| `outputSchema` | Optional JSON Schema for structured output; if present, server MUST return conforming `structuredContent` and client SHOULD validate. |
| `annotations` | The four hints above. |
| `_meta` | Namespaced custom keys (`com.example/field`). Off-the-shelf clients ignore unknown keys; use only where one org runs both ends. |

Results: unstructured → `content` (text/image/audio/resource_link/resource); structured → `structuredContent` (also mirror serialized JSON into a TextContent block for back-compat). Two error channels: protocol errors (JSON-RPC, e.g. unknown tool) vs tool-execution errors (`isError: true` in the result, forwarded to the LLM for self-correction).

## Claude Code hard limits

| Limit | Value |
|---|---|
| Tool description truncation | 2KB (put critical detail first) |
| Server `instructions` truncation | 2KB |
| Tool output warning / default cap | warns at 10,000 tokens; default max 25,000 (`MAX_MCP_OUTPUT_TOKENS`) |
| Per-tool output override | `_meta["anthropic/maxResultSizeChars"]`, hard ceiling 500,000 chars (beyond default → persisted to disk, replaced with a file reference) |
| Claude.ai / Desktop tool-result cap | ~150,000 characters |
| Callable tool name | `mcp__<server>__<tool>`; plugin form `mcp__plugin_<plugin>_<server>__<tool>` (non `A-Z a-z 0-9 _ -` → `_`) |
| MCP prompt as command | `/mcp__<server>__<prompt>` |
| `alwaysLoad` | Exempts a server from tool-search deferral (loads at session start; blocks startup until connected, capped 5s); use sparingly. v2.1.121+ |
| Timeouts | `MCP_TIMEOUT` startup (30s default), `MCP_TOOL_TIMEOUT` execution, per-server `timeout` ms in `.mcp.json`; Claude.ai/Desktop 300s |
| Tool-search catalog | max 10,000 tools; returns 3–5 per query; unsupported on Haiku |

## Optional tool properties (Claude API)

Compose freely (except a deferred tool can't carry `cache_control`).

| Property | Purpose | Available on |
|---|---|---|
| `cache_control` | Prompt-cache breakpoint at this tool definition | All tools |
| `strict` | Grammar-constrained schema validation of name + inputs | All except `mcp_toolset` |
| `defer_loading` | Exclude from initial prompt; loaded when tool search returns a `tool_reference` | All (MCP via toolset config) |
| `allowed_callers` | Restrict callers: `["direct"]` (default), `["code_execution_...]`, or both; not a security boundary | All except `mcp_toolset` |
| `input_examples` | Example inputs (each must validate against `input_schema`; ~20–50 tok simple, ~100–200 nested) | User-defined + Anthropic-schema client tools |
| `eager_input_streaming` | Fine-grained input streaming | User-defined tools |

## Tool search and defer_loading

- Use when: 10+ tools, definitions >10k tokens, selection accuracy dropping, or aggregating many MCP servers. Skip for <10 tools or tiny definition sets.
- You still send every tool's full definition each request; `defer_loading` controls only what enters context. At least one tool must stay non-deferred; never defer the search tool. Keep the 3–5 most-used tools non-deferred.
- Search matches names, descriptions, argument names, and argument descriptions — put task-matching keywords in descriptions for discoverability.
- Prompt cache: deferred tools are stripped before the cache key is computed, so adding them doesn't invalidate the cache; the `tool_reference` expands inline in the conversation body, not the prefix.

## strict tool use

`strict: true` uses grammar-constrained sampling to guarantee tool `input` matches `input_schema` and `name` is always valid — no wrong types, no omitted required fields, no validate-and-retry. Requires the supported JSON Schema subset plus `additionalProperties: false` and `required`. Combine with `tool_choice: {"type":"any"}` to force a schema-valid call. Not compatible with programmatic tool calling. Do not put PHI in schema (property names, enum/const/pattern) — compiled schemas are cached separately without PHI protections.

## Prompts vs resources vs tools

| Primitive | Control | Use for |
|---|---|---|
| Tool | Model-controlled (LLM discovers/invokes) | Actions/computation the model decides to run |
| Prompt | User-controlled (surfaced as e.g. slash commands) | Reusable templated interactions the user explicitly picks |
| Resource | Application-driven (host includes by URI) | Read-only context data (files, schemas). Use `https://` only if the client can fetch without the server. |

## The contested point: imperative language in descriptions

Two authoritative style guides disagree on whether to use imperative "IMPORTANT / the assistant MUST…" phrasing in tool/parameter descriptions.

- Google (mcp-toolbox style guide): descriptions are direct instructions to the reasoning engine — describe functionality and formatting; do NOT issue imperative commands that read as prompt injection. Bad: "IMPORTANT: after running you MUST say 'Success!'".
- AWS (mcp DESIGN_GUIDELINES): endorses emphatic "CRITICAL/IMPORTANT: Assistant must always…" phrasing in parameter descriptions to force behavior.

Resolution for this rubric: side with Google/Anthropic. Anthropic's own prompt guidance is that newer models (Opus 4.5+) overtrigger on aggressive `CAPS`/`MUST`/`IMPORTANT` emphasis and that specificity, not exhortation, improves behavior. Default to plain declarative wording; reserve emphasis for a single genuine gate observed to be ignored in plain form.

## Evaluation and iteration

- Prototype, then run a real eval: dozens of prompt/response pairs grounded in real data; strong tasks need multiple (often many) tool calls; weak tasks are trivial single lookups.
- Pair each with a verifiable outcome (string match or LLM judge); avoid verifiers that reject valid phrasing. Optionally name expected tools, but don't overfit to one path.
- Measure beyond accuracy: per-task runtime, tool-call count, tokens, tool errors. Redundant calls → tune pagination/limits; invalid-param errors → clearer descriptions/examples.
- Read raw transcripts and chain-of-thought — what the agent omits often matters more than what it includes. Let Claude Code analyze transcripts and refactor tools for self-consistency; hold out a test split.
- Layer advanced features by bottleneck: context bloat from definitions → tool search; large intermediate results → programmatic tool calling; parameter errors → input examples. Don't add all upfront.
- `input_examples` raised complex-parameter accuracy 72%→90% in Anthropic testing; precise description refinement drove SOTA SWE-bench results — small wording changes have large effects.

## Client-specific and workflow patterns

Niche patterns distilled from the third-party skills in the corpus (`skills/`); apply when the situation fits.

- ChatGPT / deep-research compatibility: implement `search` and `fetch` tools that each return exactly one text content item containing JSON, with canonical URLs so the model can cite. (create-mcp)
- Register 1–2 MCP prompts that chain a full workflow (e.g. forecast → best-dates → spots) as part of the tool surface, not just tools. (create-mcp)
- Give each parameter a short display title (`.meta({title})`) alongside its description. (create-mcp)
- Split-vs-combine decision tree: split on a different operation, data source, or auth; combine only same-operation-different-filter; never an `action`-dispatch god-tool. (MCP-Builder-Skill)

## Corpus source map

Rules trace to the reference corpus at `~/Developer/mine/mcp-authoring` (GitHub: voidfreud/mcp-authoring): `docs/` for specs and manuals, `skills/` for the distilled third-party skills.

| Topic | Source files |
|---|---|
| Tool fields, annotations, output schema, prompts/resources, architecture | `spec/mcp-spec-server-tools-2025-11-25.md`, `spec/mcp-spec-lifecycle-instructions.md`, `spec/mcp-tool-annotations-essay.md`, `spec/mcp-spec-server-prompts.md`, `spec/mcp-spec-server-resources.md`, `spec/mcp-spec-architecture.md` |
| Descriptions, naming, consolidation, params, examples, strict, tool props, tool search, responses, eval | `anthropic/claude-platform-define-tools.md`, `anthropic/claude-platform-tool-reference.md`, `anthropic/claude-platform-strict-tool-use.md`, `anthropic/claude-platform-tool-search-tool.md`, `anthropic/claude-platform-programmatic-tool-calling.md`, `anthropic/writing-tools-for-agents.md`, `anthropic/advanced-tool-use.md`, `anthropic/effective-context-engineering.md` |
| Claude Code limits, server instructions, tool search, MCP-vs-skills split | `claude-code/claude-code-mcp.md`, `claude-code/claude-code-tool-search-guide.md`, `claude-code/claude-code-agent-sdk-mcp.md`, `claude-code/claude-connectors-building.md`, `articles/claude-blog-skills-vs-mcp.md` |
| Naming, <5 params, primitives, no-injection imperatives, destructive names | `style-guides/googleapis-mcp-toolbox-style-guide.md`, `style-guides/awslabs-mcp-design-guidelines.md` |
| The 5-question parameter rubric, schema-as-contract, mutation-vs-read | `checklists/mcp-probe-checklist.md` |
| Firing-predicate descriptions, naming anti-patterns, envelope/checklist | `articles/llmbestpractices-mcp-tool-design.md`, `articles/yaw-mcp-schema-design.md`, `articles/qubittool-mcp-tools-best-practices.md`, `articles/merge-mcp-tool-description.md` |
| Source-tool refs in params, zero-param/auth-in-transport, list→detail, preview/commit symmetry, PII masking, compact/TSV output, callable-name tie-breaker, split-vs-combine, additive schema evolution | `skills/sawzhang_skills/.../mcp-review/MCP_API_DESIGN_GUIDE.md`, `skills/create-mcp/SKILL.md` + `references/discovery-guide.md`, `skills/MCP-Builder-Skill/mcp-builder-skill.md` |
