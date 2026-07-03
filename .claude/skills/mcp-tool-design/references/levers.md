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
| Param description / hide / name | `ToolOverride.params[]` (`original`, `description`, `hide`, `name`) | inside the `override` payload | hot-reload |
| Backend on/off | `Backend.enabled` | `POST /admin/api/backend/{name}/enabled` `{value}` | mounts/unmounts live |
| Reset a tool to captured defaults | — | `POST /admin/api/reset` `{backend, tool_original}` | hot-reload |

Deprecation heads-up: **param renaming (`params[].name`) is scheduled to stop being editable.** Don't build tuning on param renames — carry the fix in the param description instead. Tool broadcast names stay editable.

Not levers: schemas (types/enums/required), annotations, response shapes — those are the backend's. When their text under-informs, compensate in the descriptions we do own.

## Reading state

- Effective surface + byte budgets: `surface.py` (see SKILL.md step 1).
- Raw captured text as the backend shipped it: `~/.local/state/mcp-gateway/defaults/<backend>.json` (also `server_info`, `capabilities`).
- Everything at once, as the admin UI sees it: `GET http://127.0.0.1:9100/admin/api/state`.
- Live probe of a real tool through the gateway (research use, respect read-onlyness): `POST /admin/api/run` `{backend, tool, args}`.

## Guardrails the app enforces

- Broadcast names: `[A-Za-z0-9_-]`, unique per backend (each backend is its own endpoint — cross-backend name reuse is fine). Collision-checked on save; a deliberately-set description identical to a sibling's is also rejected.
- Overrides are stored as diffs against captured defaults — saving the default value stores nothing.
- All admin writes are lock-wrapped and the config save is atomic; edit through the API, not by hand-editing `config.toml` under a running daemon.

## The verify loop (not optional)

1. Apply via the API — the server side hot-reloads instantly.
2. **Reconnect the client.** An already-running Claude Code session keeps the old broadcast until session restart or a `/mcp` reconnect. Grade after reconnect or you grade stale text and conclude an edit didn't land.
3. Re-run `surface.py` — the effective text and byte budgets are what you drafted.
4. Re-read the live broadcast as a cold agent (the differentiation.md step-6 pass) — the graded text, not the intended text, is what ships.
5. `uv run verify_rename.py http://127.0.0.1:9100` for the per-endpoint receipt when the change was structural (renames, enable/disable).
