# Levers and mechanics — what can change and how

The gateway rewrites broadcast text and forwards calls untouched. Every lever, its write path, and the verify loop. The lever set tracks the app: any app change to override schema or mechanics must update this file in the same change (CLAUDE.md rule); when unsure, the truth is `config_loader.py`'s models.

## The levers

| Lever | Config | Write path | Takes effect |
|---|---|---|---|
| Server instructions override | `Backend.instructions` | `PUT /admin/api/instructions` `{backend, value}` | hot-reload |
| Tool name / title / description | `ToolOverride.name/title/description` | `PUT /admin/api/override` `{backend, tool_original, override:{…}}` — merge semantics: an ABSENT key preserves the stored override field (#139); send a field's default value to reset it | hot-reload |
| Tool on/off | `ToolOverride.enabled` | same `override` payload | hot-reload |
| Pin tool upfront | `ToolOverride.always_load` | same `override` payload | hot-reload (meta only) |
| Pin whole backend | `Backend.always_load` | `POST /admin/api/backend/{name}/pin` `{value}` | hot-reload |
| Per-tool output cap (#162) | `ToolOverride.max_result_chars` (positive int) — broadcast as `_meta["anthropic/maxResultSizeChars"]`, which Claude Code honors over its global 25k-token `MAX_MCP_OUTPUT_TOKENS` cap (mechanics: references/discovery.md). Raise for bulk readers (e.g. `read_repo_wiki`), lower for chatty tools. Merges into the tool's captured `_meta` alongside the pin flag; unset = client default | same `override` payload (`max_result_chars`; `null` clears) | hot-reload (meta only; reconnect to re-broadcast) |
| Param description / hide / name | `ToolOverride.params[]` (`original`, `description`, `hide`, `name`) | inside the `override` payload | hot-reload |
| Injected param default (#35) | `ToolOverride.params[].default` (scalar: str/int/float/bool) — the gateway injects it on every call; setting one is the ONLY way to hide a *required* param (hide without a default is rejected for required params) | same `params[]` entry (`{original, hide, default}`) | hot-reload |
| Backend on/off | `Backend.enabled` | `POST /admin/api/backend/{name}/enabled` `{value}` | mounts/unmounts live |
| Reset a tool to captured defaults | — | `POST /admin/api/reset` `{backend, tool_original}` | hot-reload |
| Composite tool (#14) — name, description, FULL param schema (names, types, descriptions, required/default), member set, per-member arg mapping + injected `static_args`, per-member timeout, pin | `[[composites]]` in config.toml (models: `Composite`/`CompositeParam`/`CompositeMember`) — served together at `/composite/mcp`; member `tool` is the EXPOSED (post-rename) name, so backend overrides apply under it | hand-edit config.toml + restart (`POST /admin/api/restart`); enable/disable only: `POST /admin/api/composite/{name}/enabled` `{enabled}` (hot) | restart (toggle is hot) |
| Composite smart routing (#21) — `strategy` (`all`/`keyword`/`llm`), per-member `route_patterns` (keyword regexes vs the call's arg text) + `route_description` (the condition the LLM router reads), `[composites.router]` (model, `${ENV}` api_key, `conditions` policy text, timeout, `fallback`: `"all"` or a member label) | `Composite.strategy`/`CompositeRouter`/`CompositeMember.route_patterns/route_description` in config.toml | hand-edit config.toml + restart | restart |
| Resource / template name / title / description (#15) | `ResourceOverride.name/title/description` (keyed by `uri` — the identity, never rewritten) | `PUT /admin/api/resource-override` `{backend, uri, override:{…}}` — same #139 merge semantics | hot-reload |
| Resource on/off (#15) | `ResourceOverride.enabled` — off hides it from the listing AND blocks reads | same `override` payload | hot-reload |
| Prompt name / title / description (#15) | `PromptOverride.name/title/description` — renames are real (`prompts/get` reverse-maps to the backend original) | `PUT /admin/api/prompt-override` `{backend, prompt_original, override:{…}}` | hot-reload |
| Prompt argument description (#15) | `PromptOverride.args[]` (`original`, `description`) — argument NAMES are not renameable (the call forwards them verbatim) | `args` list inside the `override` payload | hot-reload |
| Prompt on/off (#15) | `PromptOverride.enabled` | same `override` payload | hot-reload |
| Reset a resource / prompt to captured defaults (#15) | — | `POST /admin/api/resource-reset` `{backend, uri}` / `POST /admin/api/prompt-reset` `{backend, prompt_original}` | hot-reload |
| Behavior hooks (#16) | `ToolOverride.validate` / `ToolOverride.post_process` — `"module:function"` in the hooks dir (`MCP_GATEWAY_HOOKS` > `./hooks/` > `~/.config/mcp-gateway/hooks/`). `validate(args)` raises `ValueError(msg)` to reject a call (msg -> the caller); `post_process(result)` reshapes the answer. Sync or async. Hooks see EXPOSED arg names (post-rename, hidden absent); renames/hidden-injection still apply on forward. USER-AUTHORED CODE running in the daemon (docs/security.md) | hand-edit `config.toml` (NOT admin-writable; UI saves preserve the specs, `POST /admin/api/reset` clears them with the rest of the override; state shows `validate`/`post_process`/`hook_error` read-only) | next transform build (hot-reload / boot); hook FILES re-read by mtime, so editing the .py needs no restart |

Composite text is fully AUTHORED surface, not a rewrite: there are no captured defaults behind a composite (nothing to diff against or reset to), so `surface.py`-style budget thinking applies but the reset/migrate machinery does not. `GET /admin/api/composites` lists them. Grade a composite's cold read like any tool, plus the merge behavior its description promises (labeled per-member sections; failed members reported inline).

Routing text (#21) is authored surface a MODEL reads, so tune it like a description: `route_description` should be a crisp one-line routing condition per member ("use for code and API questions"), and `router.conditions` the composite-level tie-break policy. Routing is best-effort — keyword no-match and every llm failure (timeout, HTTP error, garbage reply) fall back to `router.fallback` (default: all members), logged as `composite_route_fallback`; verify a routed composite live by watching that log line, not just the merge.

Deprecation heads-up: **param renaming (`params[].name`) is scheduled to stop being editable.** Don't build tuning on param renames — carry the fix in the param description instead. Tool broadcast names stay editable.

Not levers: schemas (types/enums/required), annotations, response shapes — those are the backend's. When their text under-informs, compensate in the descriptions we do own. Also not a tuning lever: hard-renaming a backend (#44, `POST /admin/api/backend/{name}/rename` `{value}`) is a topology op — the endpoint URL and the `gateway-<name>` Claude Code registration both change, the gateway restarts, and Claude Code must be re-registered (distinct from the cosmetic `display_name`, #42).

## Reading state

- Effective surface + byte budgets: `surface.py` (see SKILL.md step 1).
- Raw captured text as the backend shipped it: `~/.local/state/mcp-gateway/defaults/<backend>.json` (also `server_info`, `capabilities`, and — #15 — `resources`, `resource_templates`, `prompts`). The baseline auto-refreshes (#43: post-(re)connect, backend `tools/list_changed`, admin page load, optional interval) — overrides are diffs by original name, so a refresh never clobbers edits; new tools appear un-overridden.
- Per-backend liveness: `GET /admin/api/status` (#23) — `ok`/`error`/`unmounted`/`disabled` + latency, probed through the live proxy. A WARM backend that probes `error` also triggers a session recycle (#161, best-effort).
- Everything at once, as the admin UI sees it: `GET http://127.0.0.1:9100/admin/api/state` (also carries `bearer_token` — the `${ENV}` ref, never resolved — and `introspect_interval`).
- Gateway-wide settings: `GET/PUT /admin/api/settings` (#155) — the bearer-token `${ENV}` ref and `introspect_interval`. Both are read only at boot, so a PUT returns restart semantics; the token PUT rejects a raw (non-`${ENV}`) value.
- Warm-session recycling (#161): warm (`stateless=false`) backends now reuse ONE backend session across calls AND auto-recycle it (unmount + fresh re-mount) when it dies — a dead session is detected in the call-log middleware / status probe and heals without a daemon restart (cooldown: one recycle per backend per 30s). Import now defaults new backends to warm for every transport. Toggle per backend via `POST /admin/api/backend/{name}/stateless` `{value}` (saves + recycles; no restart).
- Live probe of a real tool through the gateway (research use, respect read-onlyness): `POST /admin/api/run` `{backend, tool, args}`.

## Guardrails the app enforces

- Prompt broadcast names follow the same identifier rule and uniqueness as tool names (within the backend's prompts); duplicate prompt TARGET names — enabled or disabled — are rejected by the same transform dry-build that guards tools (#15). Resource names are free-form display text (no rule, no collision check — the URI is the identity).
- Broadcast names: `[A-Za-z0-9_-]`, unique per backend (each backend is its own endpoint — cross-backend name reuse is fine). Collision-checked on save; opt-in escape hatch per save (#22): `"on_collision": "uniquify"` at the top level of the `PUT /admin/api/override` payload auto-suffixes a colliding name (`_2`, `_3`, …) instead of rejecting, and the response then carries the final `name` + `uniquified: true`. A deliberately-set description identical to a sibling's is always rejected (no uniquify for descriptions).
- Name uniqueness spans EVERY stored override entry, not just broadcast tools: a DISABLED tool's name and a *dangling* override (its `original` no longer in the captured baseline — the backend renamed the tool upstream) still occupy transform target names, and a save that would duplicate one is a 400 telling you to reset/rename the stale entry first.
- Repairing a dangling override (#153): when a baseline refresh (#43) leaves an override whose `original` no longer matches a captured tool, its tuned text silently stops applying (reconcile logs `override_no_match`). The admin UI surfaces these as an amber banner in the backend detail (and a ⚠ in the sidebar): per row, pick the tool's new name from the dropdown and one-click **migrate** the text across, or **discard** it. Scripted path: `POST /admin/api/backend/{name}/migrate-override` `{from, to}` carries the override's fields onto `to` (param overrides survive only where the param still exists in `to`'s captured schema — dropped ones are reported in the response) and then removes the old entry; `POST /admin/api/backend/{name}/discard-override` `{original}` just drops the stale entry. `GET /admin/api/state` exposes each backend's `dangling` list; a migrate target must be a captured tool with no stored override yet.
- Overrides are stored as diffs against captured defaults — saving the default value stores nothing.
- Hiding a param the backend marks *required* needs an injected `default` on that param (#35); without one the save is a 400. Non-required params hide freely.
- Behavior hooks fail CLOSED per tool (#16): a hook that can't load (missing file/function, import crash) never blocks the mount or other tools, but every call to that tool errors with the load failure until the hook file is fixed. A malformed spec *string* is rejected at config load. Watch `hook_load_error` in the log / `hook_error` in state.
- All admin writes are lock-wrapped and the config save is atomic; edit through the API, not by hand-editing `config.toml` under a running daemon.
- If the gateway has a `bearer_token` configured (#26), every `/admin/api/*` call in this pipeline needs `-H "Authorization: Bearer <token>"` — a 401 means the token, not the payload.

## The verify loop (not optional)

1. Apply via the API — the server side hot-reloads instantly.
2. **Reconnect the client.** An already-running Claude Code session keeps the old broadcast until session restart or a `/mcp` reconnect. Grade after reconnect or you grade stale text and conclude an edit didn't land.
3. Re-run `surface.py` — the effective text and byte budgets are what you drafted.
4. Re-read the live broadcast as a cold agent (the differentiation.md step-6 pass) — the graded text, not the intended text, is what ships.
5. `uv run verify_rename.py http://127.0.0.1:9100` for the per-endpoint receipt when the change was structural (renames, enable/disable).
