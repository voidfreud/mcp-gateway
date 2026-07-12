# Admin HTTP API

The admin UI is a thin front end over an HTTP API served by the same daemon at
`http://127.0.0.1:9100`. This page documents that API for scripting and
automation. Most people never need it — the [admin UI](admin-guide.md) does all
of this — but it is here when you want it.

## Conventions

- **Base URL:** `http://127.0.0.1:9100` (loopback only).
- **Content type:** every mutating route (POST/PUT/DELETE with a body) expects a
  JSON request body and `Content-Type: application/json`; a non-JSON body is
  rejected with `400`.
- **Origin guard:** every route rejects a browser request whose `Origin` is not
  the gateway's own, with `403` (DNS-rebinding protection). Non-browser clients
  send no `Origin` and pass. See [security.md](security.md#the-origin-guard-built-in-always-on).
- **Auth:** if `bearer_token` is set, **every** `/admin/api/*` route requires
  `Authorization: Bearer <token>`; a missing or wrong token gets `401`. Only
  `/health`, `/ready`, and the bare `GET /admin` page are exempt. See
  [security.md](security.md#the-optional-bearer-token).
- **Error shape:** validation and not-found errors return
  `{"ok": false, "error": "<message>"}` with a `4xx` status.
- **`reloaded` field:** mutating responses report how the change applied —
  `"in-process"` (hot-reloaded, no restart), `"restarting"` (a topology change
  restarting the daemon), or `"dev-no-restart"` (topology change while running in
  the foreground, where there is no service to restart; config is written and
  takes effect on the next real restart).

## Liveness (top-level, always open)

| Method | Path | Response |
|--------|------|----------|
| GET | `/health` | `text/plain`: `ok mcp-gateway <version> @ <resolved code path>`. Names the directory the daemon actually runs from. |
| GET | `/ready` | JSON `{ready, mounted, enabled, missing}`. Status `200` when every enabled backend is mounted, `503` otherwise. |

## General

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | `/admin` | — | The admin UI page (HTML). |
| GET | `/admin/api/state` | — | Full UI state: every backend, its captured default tools/params, and your overrides. |
| GET | `/admin/api/export` | — (query `?full=true` adds captured defaults) | The complete stored settings bundle as JSON — every override, instruction, pin, and display name. |

## Settings (text overrides)

| Method | Path | Body | Response |
|--------|------|------|----------|
| PUT | `/admin/api/override` | `{backend, original, name?, title?, description?, enabled?, always_load?, params?, on_collision?}` | `{ok, reloaded: "in-process"}`. If `on_collision: "uniquify"` was set and a name collided, also `{name: "<final>", uniquified: true}`. A collision without uniquify, or an invalid field, returns `{ok:false, error}`. Merge semantics: a key absent from the body preserves the stored value rather than clearing it. |
| POST | `/admin/api/reset` | `{backend, tool_original}` | `{ok}`. Clears every override for that one tool (reverts to the backend default). |
| PUT | `/admin/api/instructions` | `{backend, value}` | `{ok}`. Sets the backend's server-instructions override (`value` empty inherits the original). Rejected if it exceeds the ~2KB budget. |
| POST | `/admin/api/import` | `{mode: "merge"\|"replace", settings: {...}}` | `{ok, backends, mode}` on success; `400 {ok:false, errors, applied:false}` if any item is invalid (all-or-nothing). Backend topology is never imported. |
| POST | `/admin/api/backend/{name}/migrate-override` | `{from, to}` | Carries a stale override (its `from` tool no longer exists upstream) onto the tool's new name `to`; params that no longer exist are dropped and reported. `{ok, dropped_params}`. `400` if `to` is unknown or already overridden. |
| POST | `/admin/api/backend/{name}/discard-override` | `{original}` | Drops a stale override entry. `{ok}`; `400` when no such entry. |

## Backends (topology)

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/admin/api/backend` | `{name, transport, url?/command?/args?, auth_header?, auth_value?, headers?, auth?, headers_helper?, stateless?}` | Imports a new backend: validates, connects and captures its baseline, then restarts. `400` on a name clash, invalid fields, or a failed connection. |
| DELETE | `/admin/api/backend/{name}` | — | Removes the backend and prunes its captured defaults; restarts. `{ok, reloaded}`. |
| POST | `/admin/api/backend/{name}/rename` | `{value: "<new name>"}` | Hard rename (endpoint, config key, defaults, registration all move); restarts. Response includes `old_endpoint`, `new_endpoint`, `old_registration`, `new_registration`. |
| POST | `/admin/api/backend/{name}/display-name` | `{value: "<label>"}` | Sets the cosmetic display label (empty clears it). `{ok}`. No restart. |
| POST | `/admin/api/backend/{name}/enabled` | `{value: bool}` | Enable (mount live) or disable (unmount) the backend. `{ok, reloaded: "in-process"}`. |
| POST | `/admin/api/enabled` | `{value: bool}` | Master switch: enable/disable every backend, mounting or unmounting each. `{ok, reloaded: "in-process"}`. |
| POST | `/admin/api/backend/{name}/pin` | `{value: bool}` | Toggle per-backend eager loading (pin all its tools). `{ok, reloaded: "in-process"}`. |
| POST | `/admin/api/backend/{name}/stateless` | `{value: bool}` | Session strategy: `false` = warm (one persistent connection, auto-repaired if it dies), `true` = fresh session per call. Saves and recycles the backend live — no restart. `{ok, reloaded: "recycled", stateless}`. |
| GET | `/admin/api/settings` | — | The gateway-wide settings: `{bearer_token, introspect_interval}` (the token is the `${ENV}` reference, never a resolved secret). |
| PUT | `/admin/api/settings` | `{bearer_token?, introspect_interval?}` | Validates (`bearer_token` empty or containing `${...}`; interval ≥ 0) and saves. Both are read at daemon start, so the response carries restart semantics. |

## Claude Code registration

These shell out to the `claude` CLI. A CLI *failure* is reported as `{ok:false}`
at HTTP `200` (the HTTP call itself succeeded) with the command output; a missing
`claude` binary or bad scope returns `400`. The bearer token, when present, is
added to the registration and redacted from the response.

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/admin/api/backend/{name}/register` | `{scope: "local"\|"user"\|"project"}` (default `local`) | Runs `claude mcp add` for `gateway-<name>` pointing at the backend's endpoint. `{ok, exit, stdout, stderr, command, note}`. |
| POST | `/admin/api/backend/{name}/deregister` | `{scope}` | Runs `claude mcp remove gateway-<name>`. Works even if the backend is already gone (post-remove cleanup). Same response shape. |
| GET | `/admin/api/cc-registrations` | — (`?fresh=1` busts the 60s cache) | Which configured backends are registered in Claude Code, parsed from `claude mcp list`. `{available, registered: {<backend>: bool}}`; `{available:false}` without the CLI. |
| POST | `/admin/api/cc-reregister-all` | `{scope}` | Deregister + register every **enabled** backend, sequentially; one failure doesn't stop the rest. `{ok, count, ok_count, backends: [...]}`. |

## Operations

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/admin/api/run` | `{backend, tool, args}` | Executes one tool through the live proxy (the same path Claude uses). `{ok, is_error, ms, content, structured}`. `400` if the backend isn't mounted or the input is invalid; `502` if the call itself raised. |
| POST | `/admin/api/restart` | — | Restarts the daemon on demand (honest no-op in foreground/dev). |
| POST | `/admin/api/introspect/{name}` | — | Forces a re-capture of the backend's live tool list (bypasses the throttle) and hot-reloads. `{ok, ...}` with the tool delta; `502` if introspection failed. |
| GET | `/admin/api/status` | — | Per-backend liveness, one concurrent probe each. `{backends: {<name>: {state, ms?, tools?, error?}}}` where `state` is `ok`, `error`, `disabled`, or `unmounted`. |
| POST | `/admin/api/refresh` | — | Throttled re-introspect of every enabled, mounted backend (the admin-page-load sweep). `{ok, backends: {...}}`. |

## Related

- [admin-guide.md](admin-guide.md) — the UI these routes back.
- [security.md](security.md) — the bearer token and Origin guard in depth.
