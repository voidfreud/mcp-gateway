# Wording the broadcast surface

The craft rules for every string the gateway broadcasts. Distilled from the MCP spec, Anthropic's tool-use and tool-search guidance, the Google/AWS style guides, and the reference skills — all in `corpus/` (map at the bottom). Where the corpus is thin, research wider and extend the corpus rather than guessing.

## Cross-cutting laws

- **Write for a first-time agent: actionable beats accurate.** A term can be technically correct yet unusable by an agent seeing the tool cold — that is a defect, not a style issue. "Works on public repos" is true and useless: the agent cannot evaluate queryability a priori (queryability doesn't track fame). Replace every unevaluable qualifier with the real input path, an instruction to attempt rather than pre-filter, and the miss-signal + fallback ("if the repo isn't indexed you get X — then do Y").
- **Strip to tier.** Reference only what this endpoint, at this auth level, actually exposes. A capability gated behind an auth mode we are not in (DeepWiki's raw instructions advertise 13 private-mode `devin_*` tools our no-auth endpoint can never call) is pure byte-budget noise a cold agent still pays to read.
- **One known client — be opinionated.** Public style guides assume an unknown consumer; we broadcast to exactly one client (Claude Code through this gateway) and own both the server text and our skills. Encode our actual workflows, name our sibling servers in boundary clauses, tailor vocabulary to how we work. The "generic server text / process lives in a skill" boundary is a deliberate per-case choice here, not a law.
- **Plain declarative voice.** No `IMPORTANT`/`MUST`/caps shouting — newer models overtrigger on emphasis and it does not improve selection; specificity does. De-shout upstream text that arrives shouting.
- **Byte budgets are real.** 2 KB per description and per server instructions, truncated from the end — front-load what selection depends on. `surface.py` prints the counts.

## Server instructions — the routing surface

Always visible from turn 0; the server's firing predicate and the highest-leverage string we control.

- State precisely what should pull an agent here: the concrete task categories and situations, in the vocabulary an agent's task would use — the bait, not a capability inventory.
- Carry the boundary clause: when to use this server vs each named sibling that serves adjacent intents (see `differentiation.md`). This is the one place an undecided agent is guaranteed to look.
- Carry only knowledge that spans tools or isn't per-tool: cross-tool order ("resolve the id first, then query"), result caps, freshness/coverage traits, auth quirks, cost characteristics.
- Do not list the tools (deferred names are already visible; a list adds bytes and no routing), do not restate tool descriptions, do not describe the server generically ("provides tools for…" says nothing an agent can act on).

## Tool names

The only per-tool text a cold agent sees before searching — the name alone must telegraph the action and domain, and pull search matches.

- `verb_noun`, action first: `search_web`, `fetch_url`, `resolve_library_id`. Nouns-only and vague verbs (`process`, `get_data`) route nothing.
- Use the words a task would use — a name is also a search key. `search_slack_messages` surfaces for more queries than `query_slack`.
- Distinct from siblings at a glance: two near-identical names are a top selection-error source. `[A-Za-z0-9_-]`, ≤64 chars, no version suffixes.
- Renaming is cheap for us (the gateway rebroadcasts) but treat a settled name as an interface — saved workflows and habits reference it.

## Tool descriptions

Dual role: the corpus ToolSearch matches against (stage 1) and the selection contract once loaded (stage 2).

- Answer four things, front-loaded: what it does, when to reach for it, when NOT to (naming the better sibling), what it returns — especially any non-obvious shape or cap. Add one concrete example invocation; that line disambiguates more than prose.
- Seed the search: include the concrete nouns and verbs of the tasks it should surface for. A description that only makes sense after the tool is found is half a description.
- The actionability law bites hardest here: every qualifier evaluable, attempt-over-prefilter, miss-signal named.
- No marketing, no restating the name, no parameter laundry lists (params carry their own text).

## Parameters

Read at argument-construction time; argument names and descriptions are also searched.

- Every visible param description answers: what value, what constraints, what not to pass, one example when ambiguity is likely ("owner/repo, e.g. `voidfreud/mcp-gateway`").
- We rewrite text, not schemas — types, enums, and required-ness are the backend's and cannot be changed. When the schema under-explains (an enum whose values carry meaning, a default the schema doesn't state), the description must carry it.
- Hide (`hide: true`) any param an agent should never touch — the backend's default then applies. A hidden footgun beats a warned-about one.
- Param renaming is being retired as a lever (`levers.md`) — fix confusing params with descriptions, not renames.

## Corpus map

| Topic | Source |
|---|---|
| Spec: tool fields, annotations, instructions lifecycle | `corpus/docs/spec/` |
| Anthropic: descriptions, tool search, writing tools for agents, context engineering | `corpus/docs/anthropic/` |
| Claude Code: MCP mechanics, tool-search guide, limits | `corpus/docs/claude-code/` |
| Style guides (Google, AWS) and checklists | `corpus/docs/style-guides/`, `corpus/docs/checklists/`, `corpus/docs/articles/` |
| Reference skills (inspiration, patterns) | `corpus/skills/` |

The corpus is example material, not a boundary — when a judgment needs grounding it lacks, research the live docs and add what you learned to `corpus/`.
