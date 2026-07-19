# Claude Code profile

Use only when Claude Code is the selected target.

**Verified on:** 2026-07-19
**Installed client version:** not inspected by this skill
**Confidence:** high for the documented behavior below; low for behavior not
documented by the cited sources.

## Documented behavior

- Treat MCP tool search as enabled by default. Claude Code defers MCP tools and
  initially loads server instructions and tool names; it discovers relevant
  tools on demand.
- Write server instructions as the route into the server: task category, when
  to search, and key cross-tool constraints. Keep critical text first; Claude
  Code documents a 2 KB truncation limit for both instructions and tool
  descriptions.
- Use names, descriptions, argument names, and argument descriptions as
  discoverability text. Anthropic's tool-search documentation says its search
  covers all four.
- Recheck advertised capabilities after an MCP `list_changed` notification;
  Claude Code documents that it refreshes tools, prompts, and resources then.

## Gateway-specific levers

- When Claude Code is the target, the gateway's `always_load` override emits
  the per-tool `_meta["anthropic/alwaysLoad"]` annotation. Use it only for
  tools that must be visible before a search; it consumes context up front.
- The gateway's `max_result_chars` override emits
  `_meta["anthropic/maxResultSizeChars"]` for that tool. Claude Code uses this
  value for text-content results independently of its global output limit; it
  does not affect image content. The official MCP documentation establishes a
  hard maximum of 500,000 characters for this per-tool annotation.
- The documented global controls are separate: Claude Code warns when MCP
  output exceeds 10,000 tokens and `MAX_MCP_OUTPUT_TOKENS` defaults to 25,000
  tokens. Do not conflate those documented limits with persistence behavior.
- For a pin or output-limit request without an observed or documented need,
  leave existing configuration unchanged and a new override unset. Never emit
  placeholder `true` or numeric configuration for `always_load` or
  `max_result_chars`. If either lever was not individually approved, verify it
  remained unchanged or unset; verify eager-loading or output-budget behavior
  only for the approved tool and value actually applied.

## Known and unknown boundaries

- Official ToolSearch evidence establishes names, descriptions, argument names,
  and argument descriptions as search inputs; it does not establish MCP
  `title` as a search or ranking input. Treat titles as human-display metadata
  only; do not rely on them for discovery or ranking.
- Do not rely on an old reconnect or compaction claim. Re-read the current
  documentation or test an authorized local session when such behavior matters.
- Claude Code documents that results over a default persistence threshold can
  be replaced with a file reference, but does not publish that threshold's
  value. Never assume a default threshold. Any threshold used for validation
  must be measured in an authorized session and recorded as an observation,
  not presented as a documented default.

## Primary sources

- [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp)
- [Anthropic tool search documentation](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool)
