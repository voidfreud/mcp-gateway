# 5. Virtual Tools are a first-class, always-available gateway surface

Status: Proposed (2026-07-19)

## Context

The composite-tools, smart-routing, and code-mode experiments demonstrate
useful building blocks: one user-facing tool can dispatch to several backend
tools; dispatch can be all/keyword/LLM; and a small meta API can discover and
call the live, rewritten catalog. They must not become three independent
product surfaces with separate mounts, name rules, and lifecycle behaviour.

This ADR defines the product contract before implementation. It supersedes no
shipped behaviour. The experimental branches remain design and test evidence,
not an API to merge as-is.

## Decision

### Product and endpoint

- **Virtual Tools** are a first-class category in the Admin UI, beside **MCP
  Servers**. They have their own list, health, CRUD, test, and activation
  controls; they are not hand-authored hidden TOML appendices.
- The gateway always mounts one authenticated, Origin-guarded endpoint at
  `/virtual/mcp`. It is present even with zero active virtual tools and returns
  an empty tool list in that case. The `virtual` route is permanently reserved
  from backend names.
- `/ready` must report an unavailable virtual endpoint as not ready. Per-tool
  target failures make the virtual-tool status degraded, not the endpoint
  disappear. Admin status exposes endpoint state and each tool's resolution,
  activation, last test, and last dispatch outcome. Test and dispatch status is
  runtime telemetry and resets when the gateway process restarts.

### Identity, editing, and lifecycle

A definition stores stable source identities, never a currently displayed
name:

- a stable backend identity (introduced separately from its mutable display
  name);
- the backend tool's **original** name; and
- each target parameter's **original** name.

At list, edit, test, and call time the gateway resolves those identities to
the current effective (post-override) backend/tool/parameter names. The UI
shows both: effective names are what callers see, original identities are the
read-only binding that survives a rename. A source tool or parameter that
disappears is an explicit unresolved reference, never silently remapped.

The UI lifecycle is draft -> validate/resolve -> optionally test against the
live rewritten proxy -> activate. Testing is strongly recommended but cannot
be mandatory because a member tool may have cost or side effects. Create,
edit, duplicate, disable, delete, and test are explicit operations protected by
the gateway's existing admin security. Editing an active definition first
returns it to draft, so activation is the only operation that can publish a
changed definition. Save/activate is atomic: validate the complete resulting
configuration and dry-build the virtual tool before persisting or changing the
live server. A failed test does not alter the last known active definition.

Backend rename preserves the stable backend identity and immediately refreshes
the displayed effective names. Backend removal is rejected while a virtual
tool references it; the UI must offer an explicit transaction to delete or
disable/update the affected definitions first. Tool/parameter disappearance
marks the reference unresolved and blocks activation until repaired.

### Dispatch and results

Every active virtual tool declares one dispatch mode:

- `all`: dispatch concurrently to every eligible member;
- `keyword`: deterministic, locally evaluated rules select members, with an
  explicit validated fallback; or
- `llm`: a configured router selects members, with a bounded deadline and the
  same explicit fallback.

All member calls use the live transformed backend path after runtime identity
resolution. Member timeout, failure, selection, latency, and fallback are
observable per call. A partial failure is a labeled result; a call fails only
when no selected member succeeds, unless the virtual tool declares a stricter
policy.

Virtual Tools preserve MCP result fidelity: text, images, audio, embedded
resources, resource links, and structured content must remain representable in
the aggregate response. They must not silently flatten or discard non-text
content. Each tool has a declared aggregate output budget. Budget enforcement
is deterministic, records which member/content was truncated or omitted, and
returns an explicit truncation marker/metadata; it never reports an empty
successful result after dropping content.

### Security and data egress

The endpoint and CRUD/test APIs use the gateway's existing bearer and Origin
protections. `all` and `keyword` make no new external request. Enabling `llm`
requires an explicit administrator acknowledgement that the selected routing
inputs, member descriptions, and routing policy may be sent to the named
external provider. The UI shows the provider, model, fields sent, timeout,
fallback, and estimated cost before activation; the API key remains a `${ENV}`
reference and is never returned or logged. A routing failure fails closed to
the configured local fallback, never to an unannounced provider.

## Consequences

- Existing backend rename/remove/import code must become reference-aware and
  revalidate the complete configuration before persistence.
- Virtual-tool schema generation must not depend on Python parameter-name
  legality; public schema names and implementation argument aliases are
  separate concerns.
- A single lifecycle owner mounts `/virtual/mcp`; mount failure is a startup
  or readiness failure, never a warning that leaves a healthy-looking daemon.
- Code-mode discovery/execution becomes an optional capability *of* the
  virtual-tool catalog, rather than another competing endpoint, if retained.

## Acceptance and merge criteria

Do not merge an implementation until it provides all of the following:

1. UI/API CRUD plus the draft, validation, live-test, activation, disable, and
   delete lifecycle; definitions survive save/restart and are visible as a
   separate Admin category.
2. Socket-level receipts prove `/virtual/mcp` is mounted with zero and with
   active tools, and that health/readiness/status cannot disagree about a
   failed mount.
3. Atomic referential-integrity tests cover backend add/rename/remove, source
   tool/parameter rename or disappearance, invalid schemas, and a failed
   dry-build. No successful admin response may persist an unloadable config.
4. Dispatch receipts cover all/keyword/LLM, fallback, timeout, partial and
   total failure, concurrent calls, warm and stateless backends, and router
   data-egress acknowledgement.
5. Result receipts cover text, structured values, image/audio/resource
   content, errors, and aggregate-budget truncation without silent loss.
6. `just check`, docs/config/API/security updates, and a fresh-context agent
   usability evaluation pass. The live gate uses isolated HOME/config/state,
   disposable loopback fixtures, spare ports, and no installed daemon.

## Open decisions

- Whether a virtual tool's aggregate result should be a standard multi-content
  MCP result, a typed envelope plus original blocks, or both for compatibility.
- The exact aggregate budget unit and default (characters, serialized bytes,
  tokens, and treatment of binary content).
- Whether code-mode remains a virtual-tool capability or is retired in favour
  of normal tool search.
- The stable backend identity migration for existing TOML configurations and
  exported settings.

## Evidence considered

- `feat/14-composite-tools`: concurrent aggregation and live proxy dispatch,
  but its separate `/composite/mcp` lifecycle exposed mount, identity, and
  non-text-result gaps.
- `feat/21-smart-routing`: `all`, keyword, and OpenRouter-backed LLM routing
  with fallback; this ADR makes its external data flow a product-level consent
  requirement.
- `feat/13-code-mode`: live catalog search/schema/execute; this ADR keeps its
  live effective-name insight while avoiding a second permanent public mount.
