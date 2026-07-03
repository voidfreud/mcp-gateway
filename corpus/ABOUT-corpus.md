# mcp-authoring
A reference corpus of third-party skills + authoritative docs on writing MCP servers well
(tool names, descriptions, parameter schemas, server `instructions`). Source material for an
in-house MCP-authoring rubric/skill — see README.md for the full inventory.

## Conventions
- branch per change off main; merge via the git-plugin flow; keep the tree clean
- `skills/` = cloned third-party repos, `.git` stripped — treat as read-only reference, do not edit
- `docs/` = downloaded sources; every file keeps a `> Source: <url>` header — preserve it
- the distilled rubric/skill this feeds lives in `~/.claude/`, NOT in this repo
- to refresh a source, re-download and note the date; don't hand-edit third-party content

## Curating this file
Only what every session in this repo must know: conventions, commands, structure pointers.
Durable identity belongs in README.md; session state in the memory handoff. Update at
milestones; delete what stops being true.
