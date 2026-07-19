# Generic MCP surface contract

Read this for every change. It states protocol-level facts only; load a client
profile separately before making a client-behavior claim.

## Contract

- Model a tool with a unique `name`, optional human-readable `title`,
  `description`, and a valid JSON Schema `inputSchema`.
- Keep names unique within a server. Prefer the specification's portable name
  character set: ASCII letters, digits, `_`, `-`, and `.`; avoid spaces and
  punctuation. Check the gateway's narrower constraints before editing.
- Describe the capability and make every required argument understandable from
  the schema. A result may include `structuredContent` whether or not the tool
  declares `outputSchema`; when it does declare one, its structured result MUST
  conform to that schema. For backwards compatibility, a structured result
  SHOULD also include its serialized JSON in a `TextContent` block.
- Treat tools as model-controlled, resources as application-controlled, and
  prompts as user-controlled primitives. Do not substitute one primitive for
  another merely to change wording.
- Treat annotations as untrusted unless the server itself is trusted. Keep
  authorization and confirmation at the appropriate security boundary.

## Design implications

- Give each tool one primary job and an observable result.
- Put the action, domain, constraints, and useful failure signal in the text
  the protocol exposes.
- Keep instructions cross-tool; keep tool descriptions tool-specific; keep
  parameter descriptions focused on valid input construction.
- Preserve valid schema and result behavior while changing text. Escalate a
  schema or behavior defect instead of masking it with prose.

## Primary source

- [MCP specification 2025-11-25: tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [MCP specification 2025-11-25: server primitives](https://modelcontextprotocol.io/specification/2025-11-25/server)

Verified: 2026-07-19. Confidence: high for protocol facts; none implied for
host-specific presentation or discovery behavior.
