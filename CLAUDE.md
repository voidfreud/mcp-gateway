# Claude Code adapter

Start with [AGENTS.md](AGENTS.md) and the
[project policy](docs/project-policy.md). They are canonical for product
context, repository workflow, validation, security, and release work.

Claude Code is one supported MCP client, not the definition of the protocol.
When changing Claude-specific registration or user guidance, keep it clearly
labeled and preserve separate Codex and generic-client behavior. Verify
client-facing metadata in a fresh Claude Code connection when the affected
scenario requires live validation; a passing local or CI check does not prove
the installed client's state.

For product behavior and setup, use the maintained user documentation linked
from [AGENTS.md](AGENTS.md#user-documentation).
