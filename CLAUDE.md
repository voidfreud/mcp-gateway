# Claude Code adapter

Read and follow the canonical repository manual in [AGENTS.md](AGENTS.md)
before taking any action. It owns all project policy and workflow.

For Claude Code-specific MCP discovery or registration, use the locally
available `claude` CLI and its help output rather than assuming a CLI version
or configuration format. The gateway recognizes registration status through
`claude mcp list`; its supported registration scopes are documented by
`claude mcp add --help` and in [docs/api.md](docs/api.md).
