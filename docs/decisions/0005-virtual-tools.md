# ADR-0005: First-class Virtual Tools

**Status:** Accepted

**Decision date:** 2026-07-19

**Deciding issue / PR:** [#190](https://github.com/voidfreud/mcp-gateway/pull/190)

## Context

The composite-tools and smart-routing experiments established that one
gateway-owned tool could compose or route calls to several backend tools. They
also exposed why independent experimental mounts, transient display names, and
text-only aggregation were not a durable product surface.

## Decision

Virtual Tools are a first-class gateway surface. They are managed separately
from backend endpoints and are served from the permanent `/virtual/mcp`
endpoint, including when no Virtual Tools are active. Definitions use a
draft/validate/test/activate lifecycle so an edit does not publish until the
resulting definition is valid and activated.

Definitions bind a member to a stable backend ID and the source tool's original
name, rather than to mutable displayed names. For a legacy configuration that
has no stored ID, the gateway derives a deterministic ID and materializes it
before the first stable reference is persisted. Backend renames therefore
preserve bindings; removed or missing source identities are reported as
unresolved rather than silently remapped.

Active tools dispatch through the gateway's transformed backend surface and
preserve MCP result content in an aggregate result. The aggregate result has a
default `max_result_bytes` of `262144`; enforcement counts the serialized MCP
result and records bounded omission or truncation metadata instead of silently
discarding content.

## Consequences

- Virtual Tools have one product-owned mount and lifecycle rather than separate
  composite or routing endpoints.
- Stable source identity makes backend renames safe but prevents automatic
  rebinding when a source tool or parameter disappears; the definition must be
  repaired before activation.
- The byte limit makes large aggregate responses predictable. Content or
  metadata that does not fit is explicitly represented in the result receipt.
- The stored output envelope and upstream `_meta` handling are further refined
  by [ADR-0007](0007-catalog-pagination-and-virtual-results.md).

## Dispositions

- Code mode is not part of the product. Its experiment is tracked by
  [#13](https://github.com/voidfreud/mcp-gateway/issues/13), closed as not
  planned.
- The output envelope and `_meta` details are not open questions in this ADR;
  [ADR-0007](0007-catalog-pagination-and-virtual-results.md) records their
  shipped refinement.

## Evidence

- `src/mcp_gateway/virtual_tools.py` implements stable backend-ID assignment,
  identity resolution, dispatch, and aggregate-result budgeting.
- `docs/configuration.md` documents the `262144` default and serialized-result
  accounting.
- `tests/test_virtual_tools.py` and `tests/live/run_virtual_tools.py` cover the
  durable lifecycle, identity, dispatch, and aggregate-result contracts.
