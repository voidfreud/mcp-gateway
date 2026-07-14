# 4. Per-session isolation is a per-backend lever, not a gateway mode

Status: Accepted (2026-07-14) — issue
[#25](https://github.com/voidfreud/mcp-gateway/issues/25)

## Context

FastMCP logs a benign `reusing existing session … context mixing` INFO line
when a warm (`stateless = false`) backend serves multiple callers over one
persistent client session. The gateway quiets it to WARNING (`server.py`,
logging setup) and `docs/operations.md` documents it as expected.

Issue #25 parked the question of whether that quiet rule hides a real risk:
if a backend ever forwards **per-user auth taken from the incoming request**,
sharing one session across callers would mix identities, and per-session
isolation would become mandatory.

## Decision

Isolation stays a **per-backend choice via the existing `stateless` flag**,
not a new gateway-wide mode:

- `stateless = false` (warm): one persistent session shared by all local
  callers. Correct for every backend the gateway carries today — credentials
  are configured per backend at boot (`${ENV}` references), identical for all
  callers, and never taken from the incoming request.
- `stateless = true`: a fresh backend session per call. This IS per-session
  isolation, available today, toggleable live via
  `POST /admin/api/backend/{name}/stateless`.

The quiet rule stays: with boot-time per-backend credentials, session reuse
mixes no user state, and the INFO line is noise.

## Consequences

- If a backend is ever added that forwards caller-supplied auth, the rule is:
  set `stateless = true` for that backend. No code change is needed — the
  lever already exists (#161 gave warm backends recycle-on-death; stateless
  backends never needed it).
- The gateway itself never forwards incoming `Authorization` headers to
  backends; its optional bearer token gates access to the gateway and is
  consumed there, which keeps the warm default safe.
- `docs/security.md` ("Session isolation between callers") is the user-facing
  statement of this decision.
