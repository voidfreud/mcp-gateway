# 8. Typed runtime ownership and incremental Admin module boundaries

**Status:** accepted

## Context

The server previously maintained two independent mutable dictionaries for a
mounted backend: its live FastMCP proxy and the gateway transforms attached to
that proxy. Admin hot reload, status, lifecycle runners, and Virtual Tools all
shared those dictionaries by reference. A missed update could leave a proxy and
its transform holder out of sync, and plain `dict` annotations did not expose
the ownership rule.

`admin.py` also contains configuration services and every route group. It is a
large module, but its high-risk defaults capture and hot-reload paths have many
callers. Moving those first would create a large semantic refactor rather than
a useful boundary.

## Decision

- `runtime.BackendRuntime` owns the paired proxy and transform-holder maps. Its
  `mount`, `unmount`, and `replace_transforms` operations preserve their shared
  lifetime, while consumers receive a read-only proxy `Mapping` where mutation
  is unnecessary.
- Captured tool, metadata, and instruction inputs remain separate typed
  snapshots. They are reconstructed at boot/hot-add/recycle and are not live
  runtime state.
- Admin's legacy `(registry, holders)` registration form remains accepted by an
  adapter so existing integrations and tests do not break.
- Admin decomposition proceeds by low-risk cohesive route groups. Codex
  registration is the first extraction into `admin_routes_codex.py`, with a
  typed dependency object and context protocol. `admin.py` remains the public
  facade and resolves its live cache/CLI helpers per request, preserving its
  established monkeypatch and shared-reference behavior.
- The transform removal/addition and instruction-reset algorithm inside
  `hot_reload` is unchanged. Its critical call graph is contained, not rewritten.

## Consequences

- A backend cannot be mounted or unmounted through the runtime owner without its
  transform holder moving with it.
- Virtual Tools can look up proxies but cannot mutate lifecycle ownership.
- Route modules can be extracted incrementally without circular imports or a
  flag-day rewrite of the Admin API.
- The heterogeneous lifecycle/Virtual Tools hooks dictionary remains a later
  typed-boundary slice; it is deliberately outside this change.
