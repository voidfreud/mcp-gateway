# Architecture decision records

GitHub Issues are the canonical place to raise, discuss, defer, park, and
resolve decisions. Use the [decision proposal form](../../.github/ISSUE_TEMPLATE/proposal.yml)
when it fits; ordinary Issues are equally valid when a decision arises from a
bug, feature, or operational concern. Keep discussion, alternatives, and
unresolved follow-ups in linked Issues. Never include credentials, personal
paths, or captured live-service data.

An Architecture Decision Record (ADR) is the durable summary of an **accepted**
decision, not a parallel backlog. When acceptance is recorded, add the next
available four-digit ADR here or update the accepted ADR it changes. Do not
reuse a number or fill a historical gap. Each ADR must contain:

- `Status:` — `Accepted` or `Superseded`.
- `Decision date:` and `Deciding issue/PR:`.
- `Supersedes:` or `Superseded by:` when applicable.
- `Context`, `Decision`, and `Consequences` sections.

ADR history is immutable: do not renumber records or rewrite their rationale.
Later accepted decisions supersede or amend an earlier ADR through explicit
links. Open questions, alternatives that need more evidence, and deferred work
remain linked GitHub Issues rather than new ADRs.

## Index

| ADR | Title | Status | Relationship |
| --- | --- | --- | --- |
| [0001](0001-no-github-ci.md) | No hosted CI for the full live gate | Superseded | Superseded by [ADR-0003](0003-check-only-ci.md) |
| [0002](0002-per-backend-endpoints.md) | One gateway MCP endpoint per backend | Accepted | — |
| [0003](0003-check-only-ci.md) | Check-only CI and a local live gate | Accepted | Supersedes [ADR-0001](0001-no-github-ci.md) |
| [0004](0004-per-session-isolation.md) | Per-session isolation is a per-backend lever, not a gateway mode | Accepted | — |
| [0005](0005-virtual-tools.md) | First-class Virtual Tools | Accepted | — |
| [0006](0006-hermetic-mcp-contract-ci.md) | Hermetic MCP protocol contract CI | Accepted | Refines [ADR-0003](0003-check-only-ci.md) |
| [0007](0007-catalog-pagination-and-virtual-results.md) | Gateway-owned catalog pagination and Virtual Tool result schemas | Accepted | Refines [ADR-0005](0005-virtual-tools.md) |
| [0008](0008-runtime-ownership-and-admin-boundaries.md) | Typed runtime ownership and incremental Admin module boundaries | Accepted | — |
| [0009](0009-process-isolation-boundary.md) | Process isolation is conditional, not a default gateway mode | Accepted | Related to [ADR-0004](0004-per-session-isolation.md) |
