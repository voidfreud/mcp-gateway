# Corpus retention manifest

This catalog records the primary evidence cited by the canonical
`mcp-tool-design` skill. It retains URLs and provenance only: no third-party
document body, clone, local receipt, or private backend data belongs here.

| Stable ID | Publisher / source | URL | Revision / retrieved | Usage / license constraint | Purpose | Canonical skill consumer path |
| --- | --- | --- | --- | --- | --- | --- |
| MCP-TOOLS-2025-11-25 | Model Context Protocol | https://modelcontextprotocol.io/specification/2025-11-25/server/tools | Revision 2025-11-25; retrieved 2026-07-19 | URL pointer only; verify terms and record a revision before vendoring. | Normative tool contract. | `.agents/skills/mcp-tool-design/references/generic-mcp.md` |
| MCP-PRIMITIVES-2025-11-25 | Model Context Protocol | https://modelcontextprotocol.io/specification/2025-11-25/server | Revision 2025-11-25; retrieved 2026-07-19 | URL pointer only; verify terms and record a revision before vendoring. | Normative server primitives. | `.agents/skills/mcp-tool-design/references/generic-mcp.md` |
| CLAUDE-CODE-MCP | Anthropic Claude Code | https://code.claude.com/docs/en/mcp | Current documentation; retrieved 2026-07-19 | URL pointer only; verify terms and record a revision before vendoring. | Claude Code MCP behavior. | `.agents/skills/mcp-tool-design/references/clients/claude-code.md` |
| ANTHROPIC-TOOL-SEARCH | Anthropic Claude Platform | https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool | Current documentation; retrieved 2026-07-19 | URL pointer only; verify terms and record a revision before vendoring. | Claude Code tool-search evidence. | `.agents/skills/mcp-tool-design/references/clients/claude-code.md` |
| CODEX-MANUAL | OpenAI Developers | https://developers.openai.com/codex/codex-manual.md | Current documentation; retrieved 2026-07-19 | URL pointer only; verify terms and record a revision before vendoring. | Codex instruction guidance. | `.agents/skills/mcp-tool-design/references/clients/codex.md` |
| CODEX-MCP | OpenAI ChatGPT Learn | https://learn.chatgpt.com/docs/extend/mcp | Current documentation; retrieved 2026-07-19 | URL pointer only; verify terms and record a revision before vendoring. | Codex MCP-host evidence. | `.agents/skills/mcp-tool-design/references/clients/codex.md` |

## Removed material and reacquisition

Phase 4 removed copied source pointers, archived research, third-party clone
categories, skill-script artifacts, and empty corpus category trees. Reacquire
only the minimal primary source needed for an active claim, after recording its
stable URL, retrieved date, fixed revision when available, terms or license,
purpose, and canonical consumer here. Keep all research receipts local and
untracked.
