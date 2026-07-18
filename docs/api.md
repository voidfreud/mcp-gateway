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
| GET | `/ready` | JSON `{ready, mounted, enabled, missing, virtual}`. Status `200` when every enabled backend and the permanent `/virtual/mcp` endpoint are mounted, `503` otherwise. `virtual` carries `{mounted, endpoint, error}`. |

## General

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | `/admin` | — | The admin UI page (HTML). |
| GET | `/admin/api/state` | — | Full UI state: every backend, its captured default tools/params, and your overrides. Each tool also carries its behavior-hook specs read-only — `validate`, `post_process` (`module:function` or `null`) and `hook_error` (`null` when absent/loading fine, else the current load failure). Hooks are hand-authored in `config.toml`, not writable via the API; `PUT /admin/api/override` preserves them. |
| GET | `/admin/api/export` | — (query `?full=true` adds captured defaults) | The complete stored settings bundle as JSON — every override, instruction, pin, and display name. Behavior hooks are excluded (machine-local code references); merge-mode imports preserve stored hooks, replace-mode imports clear them with the rest of the backend's overrides. |

## Settings (text overrides)

| Method | Path | Body | Response |
|--------|------|------|----------|
| PUT | `/admin/api/override` | `{backend, original, name?, title?, description?, enabled?, always_load?, max_result_chars?, params?, on_collision?}` | `{ok, reloaded: "in-process"}`. `max_result_chars` (a positive integer, or `null` to clear) sets the tool's `_meta["anthropic/maxResultSizeChars"]` output budget; anything else is a 400. If `on_collision: "uniquify"` was set and a name collided, also `{name: "<final>", uniquified: true}`. A collision without uniquify, or an invalid field, returns `{ok:false, error}`. Merge semantics: a key absent from the body preserves the stored value rather than clearing it. |
| POST | `/admin/api/reset` | `{backend, tool_original}` | `{ok}`. Clears every override for that one tool (reverts to the backend default). |
| PUT | `/admin/api/resource-override` | `{backend, uri, override: {name?, title?, description?, enabled?}}` | `{ok, reloaded: "in-process"}`. Rewrites a resource's (or resource template's) display text; `uri` is the identity and is never rewritten. Same merge semantics as tool overrides. |
| POST | `/admin/api/resource-reset` | `{backend, uri}` | `{ok}`. Clears every override for that one resource. |
| PUT | `/admin/api/prompt-override` | `{backend, prompt_original, override: {name?, title?, description?, enabled?, args?}}` | `{ok, reloaded: "in-process"}`. Rewrites a prompt (renames reverse-map on `prompts/get`); `args` is a list of `{original, description}` — argument names are not renameable. Invalid name or a name collision returns `{ok:false, error}`. |
| POST | `/admin/api/prompt-reset` | `{backend, prompt_original}` | `{ok}`. Clears every override for that one prompt. |
| PUT | `/admin/api/instructions` | `{backend, value}` | `{ok}`. Sets the backend's server-instructions override (`value` empty inherits the original). Rejected if it exceeds the ~2KB budget. |
| POST | `/admin/api/import` | `{mode: "merge"\|"replace", settings: {...}}` | `{ok, backends, mode}` on success; `400 {ok:false, errors, applied:false}` if any item is invalid (all-or-nothing). Backend topology is never imported. |
| POST | `/admin/api/backend/{name}/migrate-override` | `{from, to}` | Carries a stale override (its `from` tool no longer exists upstream) onto the tool's new name `to`; params that no longer exist are dropped and reported. `{ok, dropped_params}`. `400` if `to` is unknown or already overridden. |
| POST | `/admin/api/backend/{name}/discard-override` | `{original}` | Drops a stale override entry. `{ok}`; `400` when no such entry. |

## Backends (topology)

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/admin/api/backend` | `{name, transport, url?/command?/args?, auth_header?, auth_value?, headers?, auth?, headers_helper?, stateless?}` | Imports a new backend: validates, connects and captures its baseline, then restarts. `400` on a name clash, invalid fields, or a failed connection. |
| DELETE | `/admin/api/backend/{name}` | — | Removes the backend and prunes its captured defaults; restarts. `{ok, reloaded}`. |
| POST | `/admin/api/backend/{name}/rename` | `{value: "<new name>"}` | Hard rename (endpoint, config key, defaults, registration all move). A live daemon hot-mounts the new route; external Claude Code registration still needs updating. Response includes `old_endpoint`, `new_endpoint`, `old_registration`, `new_registration`. |
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

## Codex registration

These routes use the `codex` CLI and keep every backend as a separate MCP
server. Codex has no Claude-style registration scope. When gateway bearer auth
is enabled, the configured token must be a single `${ENV_VAR}` reference; only
the variable name is stored in Codex configuration.

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/admin/api/backend/{name}/codex/register` | `{}` | Runs `codex mcp add gateway-<name> --url <backend endpoint>`, with `--bearer-token-env-var` when applicable. `{ok, exit, stdout, stderr, command, note}`. |
| POST | `/admin/api/backend/{name}/codex/deregister` | `{}` | Runs `codex mcp remove gateway-<name>`. Works after the backend has been removed so cleanup remains possible. |
| GET | `/admin/api/codex-registrations` | — (`?fresh=1` busts the 60s cache) | Exact registration state parsed from `codex mcp list --json`. `{available, ok, registered: {<backend>: bool}}`; `{available:false}` when the CLI is unavailable. |

## Virtual Tools

Virtual Tools are gateway-owned tools on the always-mounted `/virtual/mcp`
endpoint. Definitions bind to stable backend IDs and original source identities;
current effective names are resolved from the live transformed proxies.

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | `/admin/api/virtual-tools` | — | `{mounted, endpoint, tools}` with full definitions, live member resolution, and last test/dispatch status. Router API keys are `${ENV}` references, never resolved values. |
| GET | `/admin/api/virtual-catalog` | — | Picker catalog: backend IDs plus original/effective backend, tool, and parameter names. |
| POST | `/admin/api/virtual-tools` | Full definition | Creates a disabled draft after model validation and dry-build. `{ok, tool, lifecycle:"draft"}`. |
| PUT | `/admin/api/virtual-tools/{name}` | Full definition | Atomically saves a disabled draft, including when editing an active definition. Submitted consent fingerprints are ignored; activation binds consent to the resulting definition. |
| DELETE | `/admin/api/virtual-tools/{name}` | — | Deletes the definition and hot-reloads `/virtual/mcp`; rolls back on reload failure. |
| POST | `/admin/api/virtual-tools/{name}/validate` | — | Live resolution receipt `{ok, members, errors}` without member calls. |
| POST | `/admin/api/virtual-tools/{name}/test` | `{arguments}` | Calls the saved definition without changing activation and returns its fidelity-preserving MCP result receipt. |
| POST | `/admin/api/virtual-tools/{name}/activate` | — | Live-resolves, dry-builds, persists `enabled=true`, then hot-reloads atomically. |
| POST | `/admin/api/virtual-tools/{name}/disable` | — | Persists `enabled=false` and removes the tool from the shared endpoint without unmounting it. |

Backend removal is rejected while a Virtual Tool references its stable ID.
Backend rename preserves the ID and proves the new effective route can mount
before removing the old route. Mutations that would make an active Virtual Tool
unresolved are rejected before persistence.

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
