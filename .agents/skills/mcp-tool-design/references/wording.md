# Wording guidance

Read this with `generic-mcp.md` before drafting text.

## Write the smallest complete promise

- Name a tool with a concrete action and domain. Avoid vague verbs and nouns
  that overlap with an adjacent capability.
- Start the description with what the tool does and the task that should use
  it. State material input constraints, what the result contains, and a useful
  failure signal or next action when evidence establishes one.
- Describe each visible parameter as a value to supply, its format or limits,
  and an example only where ambiguity remains.
- Put cross-tool order, shared constraints, and routing guidance in server
  instructions. Do not duplicate every tool description there.
- Use direct, plain language. Remove marketing, non-actionable adjectives,
  stale claims, and unsupported guarantees.

## Preserve distinctions

- Write a positive trigger and a practical exclusion for tools with adjacent
  purposes.
- Use different task vocabulary for genuinely different tools. Do not create
  false differentiation; escalate an actual duplicate or missing capability.
- Keep descriptions proportional to the capability. Do not use wording to
  conceal a schema, authorization, output-size, or behavior limitation.

## Evidence discipline

Use the relevant primary source for factual wording. Use the
[corpus retention manifest](../../../../corpus/RETENTION.md) as the repository's
source catalog; retain URLs and provenance, not copied third-party material.
