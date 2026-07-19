# Codex profile

Use only when Codex is the selected target.

**Verified on:** 2026-07-19
**Confidence:** high for the documented instruction guidance; unknown for the
explicit unknowns below.

## Documented behavior

- Use initialization `instructions` for cross-tool workflows, constraints, and
  rate limits. Keep the first 512 characters self-contained, as the official
  Codex manual recommends.
- Treat Codex as an MCP host; keep the advertised surface protocol-valid and
  validate it with the generic contract.

## Known and unknown boundaries

- Search behavior, ranking, injection details, title visibility, result
  handling, and reload behavior are not established by the sources used here.
  A fresh task or restart may be chosen as conservative validation setup, but
  is not required Codex behavior; record it as test setup or an observation.
- Do not optimize a name, title, description, prompt, resource, or instruction
  around any of those claims. Obtain current primary evidence or an authorized
  local receipt first. Any configuration write or live validation requires
  explicit user authorization for its target.

## Primary sources

- [Codex manual](https://developers.openai.com/codex/codex-manual.md)
- [Codex MCP documentation](https://learn.chatgpt.com/docs/extend/mcp)
