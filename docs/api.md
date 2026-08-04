# Admin HTTP API

The admin UI is a thin front end over an HTTP API served by the same daemon.
The default address is `http://127.0.0.1:9100`; configured deployments use
their configured host and port. This page documents that API for scripting and
automation. Most people never need it — the [admin UI](admin-guide.md) does all
of this — but it is here when you want it.

## Conventions

- **Base URL:** the default is `http://127.0.0.1:9100`. The configured
  `host` and `port` determine the actual URL. A non-loopback bind is supported
  only with bearer or OAuth authentication; see
  [security.md](security.md#binding-beyond-loopback).
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
  In `[oauth]` mode, `/admin/api/*` uses the separate
  `oauth.admin_bearer_token`; MCP endpoint authentication is handled by
  FastMCP's per-resource OAuth provider.
- **Error shape:** validation and not-found errors return
  `{"ok": false, "error": "<message>"}` with a `4xx` status.
- **`reloaded` field:** routes that change live or boot-time state report how
  the change applied. `"in-process"` is an immediate backend-proxy update,
  `"hot"` is an immediate Virtual Tools endpoint update, and `"recycled"`
  rebuilds one backend session. `"hot-add"` and `"hot-rename"` mounted the new
  backend route live. `"mount-failed"` means an imported backend was saved but
  could not be mounted; repair it and restart or re-enable it. A failed rename
  returns HTTP 500 with `"mount-failed-rolled-back"` after restoring its prior
  configuration. `"restarting"` means a launchd-managed daemon has been asked
  to restart; `"dev-no-restart"` means the configuration was saved in a
  foreground/development process and applies on its next real restart.

## Liveness (top-level, always open)

| Method | Path | Response |
|--------|------|----------|
| GET | `/health` | `text/plain`: `ok mcp-gateway <version> @ <resolved code path>`. Names the directory the daemon actually runs from. |
| GET | `/ready` | JSON `{ready, mounted, enabled, missing, virtual}`. Status `200` when every enabled backend and the permanent `/virtual/mcp` endpoint are mounted, `503` otherwise. `virtual` carries `{mounted, endpoint, error}`. |

## General

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | `/admin` | — | The admin UI page (HTML). |
| GET | `/admin/api/state` | — | Full UI state: gateway version/update-check status plus every backend, its captured default tools/params, and overrides. `update` is a five-field snapshot (`current_version`, `latest_version`, `available`, `checked_at`, `error`) and this route performs no network I/O. Each tool also carries its behavior-hook specs read-only — `validate`, `post_process` (`module:function` or `null`) and `hook_error` (`null` when absent/loading fine, else the current load failure). Hooks are hand-authored in `config.toml`, not writable via the API; `PUT /admin/api/override` preserves them. |
| GET | `/admin/api/export` | — (query `?full=true` adds captured defaults) | The complete stored settings bundle as JSON — every override, instruction, pin, and display name. Behavior hooks are excluded (machine-local code references); merge-mode imports preserve stored hooks, replace-mode imports clear them with the rest of the backend's overrides. |

## Settings (text overrides)

| Method | Path | Body | Response |
|--------|------|------|----------|
| PUT | `/admin/api/override` | `{backend, tool_original, override: {name?, title?, description?, enabled?, always_load?, max_result_chars?, params?}, on_collision?}` | `{ok, reloaded: "in-process"}`. `max_result_chars` (a positive integer, or `null` to clear) sets the tool's `_meta["anthropic/maxResultSizeChars"]` output budget; anything else is a 400. If `on_collision: "uniquify"` was set and a name collided, also `{name: "<final>", uniquified: true}`. A collision without uniquify, or an invalid field, returns `{ok:false, error}`. Merge semantics apply inside `override`: an omitted key preserves its stored value rather than clearing it. |
| POST | `/admin/api/reset` | `{backend, tool_original}` | `{ok}`. Clears every override for that one tool (reverts to the backend default). |
| PUT | `/admin/api/resource-override` | `{backend, uri, override: {name?, title?, description?, enabled?}}` | `{ok, reloaded: "in-process"}`. Rewrites a resource's (or resource template's) display text; `uri` is the identity and is never rewritten. Same merge semantics as tool overrides. |
| POST | `/admin/api/resource-reset` | `{backend, uri}` | `{ok}`. Clears every override for that one resource. |
| PUT | `/admin/api/prompt-override` | `{backend, prompt_original, override: {name?, title?, description?, enabled?, args?}}` | `{ok, reloaded: "in-process"}`. Rewrites a prompt (renames reverse-map on `prompts/get`); `args` is a list of `{original, description}` — argument names are not renameable. Invalid name or a name collision returns `{ok:false, error}`. |
| POST | `/admin/api/prompt-reset` | `{backend, prompt_original}` | `{ok}`. Clears every override for that one prompt. |
| PUT | `/admin/api/instructions` | `{backend, value}` | `{ok}`. Sets the backend's server-instructions override (`value` empty inherits the original). Admin/API writes are rejected above 2,048 UTF-8 bytes; a directly authored TOML `Backend.instructions` value is not schema-capped. |
| POST | `/admin/api/import` | `{mode: "merge"\|"replace", settings: {...}}` | `{ok, backends, mode}` on success; `400 {ok:false, errors, applied:false}` if any item is invalid (all-or-nothing). Backend topology is never imported. |
| POST | `/admin/api/backend/{name}/migrate-override` | `{from, to}` | Carries a stale override (its `from` tool no longer exists upstream) onto the tool's new name `to`; params that no longer exist are dropped and reported. `{ok, dropped_params}`. `400` if `to` is unknown or already overridden. |
| POST | `/admin/api/backend/{name}/discard-override` | `{original}` | Drops a stale override entry. `{ok}`; `400` when no such entry. |

## Backends (topology)

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/admin/api/backend` | `{name, transport, url?/command?/args?, auth_header?, auth_value?, headers?, auth?, headers_helper?, stateless?}` | Validates, connects, captures a baseline, and saves a new backend. When lifecycle mount hooks are available, it mounts live with `reloaded: "hot-add"`; a mount failure leaves the saved backend and returns `"mount-failed"` for repair/restart. Without those hooks it returns normal restart semantics (`"restarting"` when launchd-managed, otherwise `"dev-no-restart"`). `400` on a name clash, invalid fields, or a failed initial connection. |
| DELETE | `/admin/api/backend/{name}` | — | Removes the backend and prunes its captured defaults. Returns normal restart semantics: `"restarting"` when launchd-managed, otherwise `"dev-no-restart"`. |
| POST | `/admin/api/backend/{name}/rename` | `{value: "<new name>"}` | Hard rename (endpoint, config key, defaults, and registration name all move). With live mount hooks, it mounts the new route as `"hot-rename"`; a failed mount returns HTTP 500 as `"mount-failed-rolled-back"` after restoring the old configuration. Without those hooks it returns normal restart semantics. External MCP-client registrations still need updating. Response includes `old_endpoint`, `new_endpoint`, `old_registration`, `new_registration`. |
| POST | `/admin/api/backend/{name}/display-name` | `{value: "<label>"}` | Sets the cosmetic display label (empty clears it). `{ok}`. No restart. |
| POST | `/admin/api/backend/{name}/enabled` | `{value: bool}` | Enable (mount live) or disable (unmount) the backend. `{ok, reloaded: "in-process"}`. |
| POST | `/admin/api/enabled` | `{value: bool}` | Master switch: enable/disable every backend, mounting or unmounting each. `{ok, reloaded: "in-process"}`. |
| POST | `/admin/api/backend/{name}/pin` | `{value: bool}` | Toggle per-backend eager loading (pin all its tools). `{ok, reloaded: "in-process"}`. |
| POST | `/admin/api/backend/{name}/stateless` | `{value: bool}` | Session strategy: `false` = warm (one persistent connection, auto-repaired if it dies), `true` = fresh session per call. Saves and recycles the backend live — no restart. `{ok, reloaded: "recycled", stateless}`. |
| GET | `/admin/api/settings` | — | The gateway-wide settings: `{bearer_token, introspect_interval, update_check, log_level, log_max_bytes, log_backup_count}`. `bearer_token` is the stored `${ENV_VAR}` reference, never a resolved secret. OAuth deployments additionally return read-only `auth_mode` and public `oauth` metadata. |
| PUT | `/admin/api/settings` | Any subset of the settings keys below. | Validates and persists boot-time settings. A launchd-managed daemon is asked to restart (`reloaded: "restarting"`); a foreground/development process returns `"dev-no-restart"`, leaving the saved values for its next real restart. In OAuth mode, changing `bearer_token` is rejected. |

For backend creation, `auth_value` must contain an `${ENV_VAR}` reference; a
literal credential is rejected with `400`.

### Gateway settings payload

`PUT /admin/api/settings` accepts any subset of these keys:

| Key | JSON type | Meaning and validation |
|-----|-----------|------------------------|
| `bearer_token` | string or `null` | Optional static-bearer token reference. Use one `${ENV_VAR}` reference; `""` or `null` clears it. Raw secrets are rejected. This key cannot change while `[oauth]` is configured. |
| `introspect_interval` | integer | Seconds between scheduled backend re-inspection sweeps; `0` disables the scheduled sweep. Must be `0` or greater. Event-driven refresh remains available when it is `0`. |
| `update_check` | boolean | Enables one startup-and-daily fixed-endpoint PyPI version check. Must be a JSON boolean; `false` disables the monitor after restart. The check never applies updates. |
| `log_level` | string | Structured-log verbosity: `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL` (normalized to uppercase). |
| `log_max_bytes` | integer | Maximum size of the active log before rotation, from `65536` to `1073741824` bytes. |
| `log_backup_count` | integer | Number of rotated log files to retain, from `1` to `100`. |

The Gateway page writes `bearer_token`, `introspect_interval`, `update_check`,
and `log_level`. It displays the two retention values read-only; set
`log_max_bytes` and `log_backup_count` through this API or
[configuration.md](configuration.md). All six values take effect after the
managed restart, or after the next real restart in foreground/development mode.

## Client registration

The gateway does not register endpoints in MCP clients; registration happens in
each client using its supported configuration or CLI. Every backend is served
at its own `/<backend>/mcp` endpoint and the shared `/virtual/mcp` endpoint is
independent of them. See the [admin guide](admin-guide.md).

## Virtual Tools

Virtual Tools are gateway-owned tools on the always-mounted `/virtual/mcp`
endpoint. Definitions bind to stable backend IDs and original source identities;
current effective names are resolved from the live transformed proxies.

| Method | Path | Body | Response |
|--------|------|------|----------|
| GET | `/admin/api/virtual-tools` | — | `{mounted, endpoint, tools}` with full definitions, live member resolution, and last test/dispatch status. Router API keys are `${ENV}` references, never resolved values. |
| GET | `/admin/api/virtual-catalog` | — | Picker catalog: backend IDs plus original/effective backend, tool, and parameter names. |
| POST | `/admin/api/virtual-tools` | Full definition | Creates a disabled draft after model validation and dry-build. `{ok, tool, lifecycle:"draft"}` with HTTP `201`; invalid, duplicate, or unbuildable definitions return `400`. |
| PUT | `/admin/api/virtual-tools/{name}` | Full definition | Atomically saves a disabled draft, including when editing an active definition. Submitted consent fingerprints are ignored; activation binds consent to the resulting definition. `404` if the name is unknown; `400` for an invalid, colliding, or unbuildable definition; `500` if replacing an active definition cannot hot-reload and is restored. |
| DELETE | `/admin/api/virtual-tools/{name}` | — | Deletes the definition and hot-reloads `/virtual/mcp`; `404` if unknown and `500` if reload fails and the definition is restored. |
| POST | `/admin/api/virtual-tools/{name}/validate` | — | Live resolution receipt `{ok, members, errors}` without member calls. `404` if unknown; unresolved members return that receipt with `400`. |
| POST | `/admin/api/virtual-tools/{name}/test` | `{arguments}` | Calls the saved definition without changing activation and returns its fidelity-preserving MCP result receipt. `404` if unknown; invalid or unresolved input returns `400`. A completed tool-level error remains a `200` receipt with `ok:false`. |
| POST | `/admin/api/virtual-tools/{name}/activate` | — | Live-resolves, dry-builds, persists `enabled=true`, then hot-reloads atomically. `404` if unknown; unresolved or unbuildable definitions return `400`; a failed hot reload returns `500` after restoring the draft. |
| POST | `/admin/api/virtual-tools/{name}/disable` | — | Persists `enabled=false` and removes the tool from the shared endpoint without unmounting it. `404` if unknown; a failed hot reload returns `500` after restoring the active definition. |

Backend removal is rejected while a Virtual Tool references its stable ID.
Backend rename preserves the ID and proves the new effective route can mount
before removing the old route. Mutations that would make an active Virtual Tool
unresolved are rejected before persistence.

## Operations

| Method | Path | Body | Response |
|--------|------|------|----------|
| POST | `/admin/api/run` | `{backend, tool, args}` | Executes one tool through the live proxy (the same path MCP clients use). `{ok, is_error, ms, content, structured}`. `400` if the backend isn't mounted or the input is invalid; `502` if the call itself raised. |
| POST | `/admin/api/restart` | — | Restarts the daemon on demand (honest no-op in foreground/dev). |
| POST | `/admin/api/introspect/{name}` | — | Forces a re-capture of the backend's live tool list (bypasses the throttle) and hot-reloads. `{ok, ...}` with the tool delta; `502` if introspection failed. |
| GET | `/admin/api/status` | — | Per-backend liveness, one concurrent probe each. `{backends: {<name>: {state, ms?, tools?, error?}}}` where `state` is `ok`, `error`, `disabled`, or `unmounted`. |
| POST | `/admin/api/refresh` | — | Throttled re-introspect of every enabled, mounted backend (the admin-page-load sweep). `{ok, backends: {...}}`. |

## Related

- [admin-guide.md](admin-guide.md) — the UI these routes back.
- [security.md](security.md) — the bearer token and Origin guard in depth.
