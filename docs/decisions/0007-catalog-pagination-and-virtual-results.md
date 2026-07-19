# 7. Gateway-owned catalog pagination and Virtual Tool result schemas

**Status:** accepted

## Context

MCP permits `tools/list` to return a complete catalog or multiple pages. The
gateway must first collect a backend's complete source catalog so transforms,
collisions, hiding, and renames operate on one coherent public surface. Passing
an upstream cursor directly to a downstream client would couple that client to
the source catalog before gateway transforms and could create gaps or duplicates.

Virtual Tools already return a stable structured envelope, but did not
advertise `outputSchema`. They also preserved member content and structured
results while dropping the member's upstream `_meta`, which limited fidelity
for schema- and metadata-aware clients.

## Decision

- FastMCP's client consumes every upstream `tools/list` page before the gateway
  applies transforms.
- Each independent backend endpoint and `/virtual/mcp` paginates its final
  public catalog at 50 tools per response. `nextCursor` values are opaque and
  owned by the gateway/FastMCP boundary; source cursors are never exposed.
- The page size is one internal compatibility constant, not a user setting.
  It can become configurable if real client or payload measurements justify it.
- Virtual Tools advertise a JSON Schema 2020-12 envelope covering their stable
  top-level result fields. Arbitrary member structured results and metadata
  remain permissive because the gateway cannot safely constrain third-party
  schemas.
- Each upstream result `_meta` is retained inside its member record, avoiding
  collisions between multiple sources. It participates in the same exact
  serialized-byte budget as member content and structured results; an omitted
  metadata block is reported explicitly in the budget receipt.

## Consequences

- Clients that manually call `tools/list` must follow `nextCursor`; normal MCP
  SDK clients already auto-paginate.
- A catalog mutation between offset-based page requests can change later pages.
  The gateway continues to advertise `listChanged=false`, so clients should
  reconnect or begin a fresh list after an admin catalog change.
- Schema-aware clients can now discover and validate Virtual Tool envelopes,
  while older clients still receive the same content blocks and JSON-compatible
  structured data.
