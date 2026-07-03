---
name: mcp-tool-design
description: "Rubric for designing/reviewing an MCP server's tool surface: names, descriptions, input schemas, server instructions, annotations, response shape. Use when writing or auditing an MCP tool, or wording a tool description or server instructions. Not for installing or configuring MCP servers."
---

# MCP tool design

The surface an LLM reads before calling a tool — name, description, input schema, annotations — is the whole contract. This rubric is what to write and what to check. Rules are distilled from the MCP spec, Anthropic's tool-use docs, Claude Code docs, and the Google/AWS/mcp-probe style guides; full tables and the source map are in [references/reference.md](references/reference.md).

Two modes:
- Authoring a tool or server → apply the six sections below, then run the Authoring checklist.
- Reviewing an existing server → run the Review checklist; cite the section each finding violates.

A tool must be self-contained: the server never sees the conversation or other servers, so name + description + schema alone must make correct use unambiguous.

## Names

- `snake_case`, verb first: `<action>_<resource>` — `create_instance`, `search_issues`, `list_orders`. The verb is the highest-value token; the noun narrows the domain.
- No redundant product prefix — the server name already disambiguates. `create_instance`, not `cloud_sql_create_instance`.
- Split read from write in the name: `list_*`/`get_*`/`search_*` for reads, `create_*`/`update_*`/`delete_*` for writes. This lets a client auto-approve reads and confirm writes.
- Regex `^[a-zA-Z0-9_-]{1,64}$` (spec allows to 128, but Anthropic caps at 64 — stay ≤64). Unique within the server; case-sensitive.
- Treat a tool name like a CLI flag: renaming it later is a breaking change for saved workflows. Pick well once.
- Avoid: noun-only (`orders`), vague verbs (`process`, `do_thing`, `get_data`), version suffixes (`list_orders_v2` — the model reads v2/v1 as different concepts), hyphens, camelCase.
- Two tools with near-identical names (`notification_send_user` vs `notification_send_channel`) are a top selection-error source — make the distinction obvious or merge them.
- Prefer a name the target client can reliably call over one that merely scores well in a directory; use dot-notation only if the client is verified to support it.

## Descriptions

The description is the firing predicate, read at selection time. Answer three things: what it does, when to use it (and when not), what it returns — especially any non-obvious return shape. Add one concrete example invocation; that line disambiguates more than prose.

- Aim for 3–4 sentences (more if complex); "extremely detailed descriptions are by far the most important factor in tool performance." A one-liner like "Gets the stock price for a ticker" underperforms.
- Front-load the critical content — in Claude Code the description is truncated at 2KB and agents may not read to the end.
- Do not restate parameter descriptions here; those are injected from the schema. Describe behavior, return shape, and formatting instead.
- Add a negative-scope clause when another tool can serve the same intent: name what NOT to use it for and point to the right tool. A missing "do not use for" is implicit permission to misfire.
- Prefer plain declarative wording. Reserve emphasis; newer models overtrigger on `IMPORTANT`/`MUST` and it does not improve selection. (Contested: AWS's guide endorses imperative "the assistant must…" phrasing; Anthropic and Google advise against — see reference. Default to plain.)
- Embed verbatim user trigger phrases the tool should fire on ("use when the user says 'grab me a coupon'").
- When tools overlap, steer priority explicitly ("prefer this over the coordinate-based version").
- No marketing tone, no apologies, no restating the name.

## Parameters and input schema

Every property carries a `description` answering all five (the mcp-probe rubric):
1. what kind of value it expects
2. what constraints apply
3. what NOT to pass
4. whether the value mutates data or only narrows a read
5. one example when ambiguity is likely

- Enums live in the schema (`"enum": [...]`), never buried in prose — many callers won't parse allowed values out of text. Use `minimum`/`maximum`, `pattern`, `minLength`/`maxLength` for the rest.
- `required` lists only fields the tool cannot infer or default. Over-listing forces hallucinated args; under-listing causes runtime errors. Optional params state their default ("Optional. Defaults to 30.") or set `default:` in the schema.
- Set `additionalProperties: false` unless arbitrary keys are genuinely accepted. For strict mode this plus `required` is mandatory.
- Prefer primitives (string/int/bool) over nested objects; aim for fewer than 5 parameters per tool.
- Use consistent names across tools (`project_id` everywhere, never mixing `project_name`). Name unambiguously: `user_id`, not `user`.
- Path-shaped params state file-vs-directory and absolute-vs-relative.
- For destructive scope, use an explicit-consequence param name (`acknowledge_permanent_deletion: true`) and repeat the warning at the param that controls blast radius (a `where` clause, a `--force`).
- For complex/nested/format-sensitive inputs, add `input_examples` (1–5, realistic data — real city names, not "string"; show minimal/partial/full). Skip them where the schema already makes usage obvious. Client tools only, not server tools.
- Name the source tool a value comes from (`store_code: from list_addresses`) so the schema doubles as a call-chain map — no external doc needed.
- Aim for zero parameters where the value can be inferred; take identity/context from the auth token, not a param. Auth and signatures live in the transport layer, never as tool params.
- Flatten tightly-correlated values into one field (`"lng,lat"`) rather than separate numbers the model can swap.
- Evolve schemas additively — add optional params, never rename an existing one (a rename is a breaking change, like renaming the tool).

## Server instructions

The server's `instructions` string (returned in the `initialize` result) is server-wide guidance the client may inject. In Claude Code with tool search on (the default), only tool names + server instructions load at session start — so this is the primary discovery surface, like a skill's description.

- Cover: what category of tasks the tools handle, when the model should reach for them, and key capabilities. Also cross-tool order and constraints ("call list_tables before querying; results cap at 1000 rows").
- Keep it generic — how to use the server correctly. Process/workflow/presentation logic belongs in a skill, not here. Avoid instructions that conflict with a skill (server says JSON, skill says markdown → the model guesses).
- Do not duplicate what's already in tool descriptions.
- Claude Code truncates instructions at 2KB; put critical details first.

## Annotations

Optional behavioral hints on a tool (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`). Defaults are pessimistic — an un-annotated tool is assumed writing, destructive, non-idempotent, open-world. Set them accurately: `readOnlyHint: true` on reads, `destructiveHint: false` on additive writes, `idempotentHint: true` where retry is safe, `openWorldHint: false` on closed domains.

- Hints are advisory, not enforcement, and untrusted from untrusted servers — never a security boundary. Mutation-vs-read must also be legible from the description and schema, not the annotation alone.
- Full semantics + default values: [references/reference.md](references/reference.md).

## Response shape

Return only high-signal fields the model needs for its next step; bloated responses waste context and bury the signal.

- Use natural, stable identifiers (`name`, `file_type`) over opaque low-level ones (`uuid`, `mime_type`, `256px_url`); resolving cryptic IDs to readable ones cuts hallucination.
- Consistent envelope every time (`status` + `data` + `metadata`). Paginate large results with `has_more`/`next_offset`/`total_count` and honor a `limit`.
- Errors are actionable: set `isError: true` with a machine code, a human message, and next-step guidance ("No orders for 123 — verify the ID with get_customer"). Never a bare `404`, empty `[]`, stack trace, or leaked internal path.
- Output IDs in the same format the input params expect, so results compose across tools.
- Consider a `response_format` enum (`concise`/`detailed`) for verbosity control. Claude Code caps tool output at 25,000 tokens by default (`MAX_MCP_OUTPUT_TOKENS`); paginate or write large output to a file and return the path.
- List→detail: list/search tools return summary fields plus an ID; a separate detail tool returns the rest.
- For large result sets prefer a compact tabular/TSV form over verbose JSON. Use Markdown for display-oriented tools, structured JSON for data/compute tools.
- Keep units consistent across tools, or label the unit in the value. Mask PII in anything the model may surface (`152****6666`).
- Return `isError: true` from a handler; never throw — an error is a response flag, not an exception.

## Whole-server rules

- Consolidate related operations toward workflows the agent actually runs (`schedule_event`, `get_customer_context`) rather than thin wrappers over every API endpoint. Fewer, more capable tools reduce selection ambiguity — but keep read and write as separate tools.
- Split vs combine: split on a different operation, data source, or auth; combine only same-operation-different-filter. Reject the `action`-dispatch god-tool (`manage_issue(action, ...)`).
- Offer high-level tools that compose low-level ones (accept an address, geocode internally) to cut the steps the model must chain.
- For preview→confirm→commit flows, give the preview and commit tools an identical parameter structure so confirmed args pass straight through.
- Provide a current-time tool — the model does not know "now".
- Selection accuracy degrades past ~30–50 loaded tools; a large server should lean on tool search (`defer_loading`) and keep only its 3–5 most-used tools loaded.
- Write tools idempotent (accept an `idempotency_key` or use natural keys); return the existing resource on retry, not a blocking error.
- Validate every input server-side; wrap handlers so failures return `isError: true` rather than crashing.

## Authoring checklist

- [ ] Names: `snake_case` verb_resource, no redundant prefix, read/write split, ≤64 chars, no version suffix
- [ ] Each description: what + when + returns + one example; negative-scope clause where a sibling overlaps; front-loaded; ≤2KB
- [ ] Every param `description` answers the five questions; enums in schema; `required` minimal; `additionalProperties: false`; <5 primitive params; no auth/signature params (identity from token); schema evolves additively
- [ ] `input_examples` on complex/format-sensitive tools only (1–5, realistic)
- [ ] Annotations set accurately (readOnly/destructive/idempotent/openWorld)
- [ ] Server `instructions`: task category + when-to-use + capabilities; generic; ≤2KB, critical-first; no skill/description duplication
- [ ] Responses: high-signal fields, stable readable IDs, consistent envelope, pagination metadata, actionable errors
- [ ] Server: consolidated to real workflows, write tools idempotent, inputs validated, tool count budgeted

## Review checklist

Read each tool as the model sees it (name + description + schema only) and flag:
- [ ] Name that hides its action or its read/write nature, or collides with a sibling
- [ ] Description missing when-to-use, return shape, or the negative-scope clause; or padded with restated params / marketing
- [ ] Any param with no `description`, or with allowed values in prose instead of an `enum`
- [ ] `required` over- or under-specified; missing defaults; missing `additionalProperties: false`
- [ ] Destructive tool whose danger isn't legible from name + description + schema (not just an annotation)
- [ ] Annotations absent or inaccurate on a clearly read-only or clearly destructive tool
- [ ] Server instructions missing, or leaking workflow/presentation that belongs in a skill, or >2KB
- [ ] Responses dumping low-signal fields, opaque IDs, unbounded lists, or bare/opaque errors
- [ ] Overlapping tools the model can't tell apart; thin endpoint-wrappers; >30–50 tools with no tool-search plan
- [ ] `action`-dispatch god-tool; auth/signature exposed as params; PII unmasked or units inconsistent in responses

Then verify behavior with a real LLM in the loop (selection, arg construction, error recovery) — schema-clean is not the same as usable. Iteration and eval guidance: [references/reference.md](references/reference.md).
