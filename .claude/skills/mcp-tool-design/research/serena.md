# research/serena.md

**Researched:** 2026-07-14
**Backend:** LOCAL stdio MCP, launched via `uvx --from git+https://github.com/oraios/serena serena start-mcp-server`. Server `Serena` v1.27.0 (matches capture's `server_info`). Currently **disabled/untuned** in the gateway.
**Tools seen (30, matches capture):** `create_text_file`, `replace_content`, `replace_in_files`, `replace_symbol_body`, `insert_after_symbol`, `insert_before_symbol`, `read_file`, `list_dir`, `find_file`, `search_for_pattern`, `get_symbols_overview`, `find_symbol`, `find_referencing_symbols`, `find_implementations`, `find_declaration`, `get_diagnostics_for_file`, `rename_symbol`, `safe_delete_symbol`, `write_memory`, `read_memory`, `list_memories`, `delete_memory`, `rename_memory`, `edit_memory`, `execute_shell_command`, `open_dashboard`, `activate_project`, `get_current_config`, `onboarding`, `initial_instructions`. Server instructions broadcast: "CRITICAL: Before starting to work on a coding task, call the `initial_instructions` tool to read the 'Serena Instructions Manual'."
**Sources:** captured baseline `~/.local/state/mcp-gateway/defaults/serena.json`; deepwiki `oraios/serena` (5 targeted questions: contexts/modes gating, `activate_project` internals, LSP warm-up + `execute_shell_command` safety, memory storage + onboarding injection, common footguns); live read-only probes via `POST /admin/api/run` against this gateway instance (`get_current_config`, `list_memories`, `list_dir` — no project registered yet, so all three returned the "No active project" gate error, which is itself the key finding, not a probe failure).

---

## The one fact that governs everything: the project-activation gate

**Every tool except a tiny allowlist requires an active project.** `Tool.apply_ex` checks for one unless the tool class is marked `ToolMarkerDoesNotRequireActiveProject`. Deepwiki names only `open_dashboard` and `remove_project` (not in this broadcast) as exempt; `get_current_config` is NOT exempt — confirmed live: calling it fresh returned:

```
No active project. Ask the user to provide the project path or to select a project from this list of known projects: []
```

`list_memories` and (per its own error shape) `list_dir` behave the same way. **Miss-signal: this exact error string, and an empty `[]` project list means none has EVER been activated in this session** — the agent must call `activate_project` with a path before anything else works (except `initial_instructions`/`onboarding`, which are meta tools that don't touch project state).

`activate_project` internals (from `SerenaAgent._activate_project`): resolves a path or a registered name → `ProjectNotFoundError` if neither; no-ops if already active; raises `ValueError` on a language-backend mismatch; shuts down the previous project's language server(s) if switching; sets the new active project; updates active tools/modes per project config; **kicks off language server init as a background task** (`_init_active_project_language_backend`) — it does NOT block the `activate_project` call on full indexing.

## LSP warm-up: not a hard gate, but a real race

Symbolic tools (`find_symbol`, `find_referencing_symbols`, `get_diagnostics_for_file`, etc.) do NOT error or hang if called before the language server finishes indexing — per deepwiki, empty/null LSP responses during warm-up are deliberately **not cached**, so early calls tend to return **sparse or empty results**, not errors, and later calls (after warm-up) return correct data. This is a silent miss-signal: an empty `find_symbol` hit right after `activate_project` may mean "not indexed yet," not "symbol doesn't exist." Indexing time is language-server-dependent — some (Kotlin, TypeScript) explicitly wait on an indexing-complete signal with a timeout (Kotlin: 120s); no universal number. For a fresh large project, expect first symbolic calls right after activation to be unreliable for a few seconds to ~2 minutes depending on language.

## execute_shell_command — the one that needs a hard instruction-level warning

Runs arbitrary shell commands with the user's full privileges. Per deepwiki: **no sandbox, no default confirmation prompt, and not always a timeout** (some subprocess calls set one, many don't). Serena's own safety story rests on `allowed_hosts` whitelisting + checksum verification for things IT downloads (language server binaries) — that protection does NOT extend to arbitrary commands the AGENT chooses to run through this tool. The tool's own broadcast description already says "Never execute unsafe shell commands!" but that's a soft ask, not an enforced boundary. **Tuning implication: this is the highest-risk tool in the whole backend and duplicates Bash exactly — no LSP value, pure liability.** Strong candidate to hide entirely (the gateway's hide+inject pattern) rather than retext, unless a specific workflow needs it.

## Memories: what/where/format

- Stored as plain Markdown, two scopes: **project-specific** at `<project>/.serena/memories/*.md` (versionable, can be committed with the repo), and **global**, shared across all projects, at `~/.serena/memories/global/` by default.
- Cross-references use a `` `mem:NAME` `` convention inside memory bodies; `rename_memory` rewrites those references automatically.
- `.serena/` project folder also holds `project.yml` (name, languages, ignore rules, initial prompt, tool/mode selection) and its own `.gitignore` (keeps Serena's cache/local config out of the user's git history).
- `onboarding` tool: fires automatically the first time a project with no existing memories is activated. It doesn't just describe — it **injects a structured prompt** instructing the agent to read a `memory_maintenance` memory (seeded from a template) then gather project structure/build/test info and WRITE it into `mem:core`, `mem:tech_stack`, `mem:suggested_commands`, `mem:conventions`, `mem:task_completion`. This is a real side-effecting workflow, not passive documentation — calling it starts a multi-file write sequence.
- `initial_instructions` tool: returns the "Serena Instructions Manual" (`SerenaAgent.create_system_prompt`) — purely informational, no side effects, safe to call anytime, exempt from the project-active gate implicitly since it needs no project.
- Miss-signal: writing too many/too granular memories bloats context on every reactivation (the activation message lists available memories) — no hard cap observed, but per deepwiki this is a known friction point across sessions.

## Contexts and modes — why 30 tools may not mean 30 tools in practice

Serena gates its own toolset by **context** (fixed at server startup, e.g. via `--context`) and **mode** (can toggle at runtime). This gateway captured the **default/full context** (`desktop-app`-like — nothing excluded), so all 30 tools showed up. But if invoked with a different context flag the visible set shrinks:
- `agent` context excludes `initial_instructions`.
- `ide` / `claude-code` contexts exclude `create_text_file`, `read_file`, `execute_shell_command`, `find_file`, `list_dir` (and `claude-code` also excludes `search_for_pattern`) — the idea being the HOST already provides those, so Serena defers to Claude Code's native Read/Bash/Glob/Grep and keeps only the LSP-unique tools.
- `planning` mode excludes write tools (`replace_symbol_body`, `replace_content`, etc.); `editing` mode excludes line-based edit tools in favor of symbolic ones.

**This is directly relevant to tuning**: the upstream project's own `claude-code` context ships a config that already hides the exact tools that duplicate Claude Code's native ones. The gateway's hide+inject lever can replicate that curation manually since this backend was launched with the default/unrestricted context (no `--context` flag passed), not `claude-code`. Two paths: (a) relaunch with `--context ide-assistant` (deprecated name) / `--context claude-code` at the command level so serena itself does the trimming, or (b) keep the current launch and use gateway-side hide on the same tool list. (a) is architecturally cleaner if the launch command is easy to edit; worth flagging to the tuner.

## Duplicate-vs-unique split (the core tuning decision)

**Pure duplicates of Claude Code's native tools, zero added value, prime hide candidates:**
- `read_file` → Read
- `create_text_file` → Write
- `list_dir` → (no direct native equivalent but overlaps Glob/`ls` via Bash)
- `find_file` → Glob
- `search_for_pattern` → Grep
- `execute_shell_command` → Bash (and carries the extra liability noted above)
- `replace_content` / `replace_in_files` → Edit (Serena's own description even says "preferred way... whenever symbol-level tools are not appropriate," i.e. it's explicitly the fallback path, not the differentiator)

**LSP-unique, the actual reason to enable this backend — symbol-aware, not text-aware:**
- `get_symbols_overview` — structural map of a file by symbol kind (first-call orientation tool)
- `find_symbol` (name-path pattern matching, e.g. `MyClass/my_method`, with depth/kind filters and optional body/hover info)
- `find_referencing_symbols` — LSP references, with code-snippet context, not text grep
- `find_implementations` — LSP interface/abstract implementations
- `find_declaration` — LSP go-to-declaration
- `get_diagnostics_for_file` — live LSP diagnostics (errors/warnings/hints) grouped by symbol — genuinely nothing else in this gateway's backend set provides live compiler/linter diagnostics
- `rename_symbol` — LSP-driven cross-file rename (semantically aware, unlike text search-replace)
- `safe_delete_symbol` — checks for zero references before deleting; refuses (returns the reference list) if unsafe
- `replace_symbol_body` / `insert_after_symbol` / `insert_before_symbol` — symbol-anchored edits; require a prior `include_body=True` read for `replace_symbol_body` specifically (its own description warns of this)

**State/project-management tools** (necessary scaffolding, not code-intel per se): `activate_project`, `get_current_config`, `onboarding`, `initial_instructions`, and the memory sextet (`write_memory`/`read_memory`/`list_memories`/`delete_memory`/`rename_memory`/`edit_memory`) plus `open_dashboard` (opens a local web UI — read-only/informational, no code side effect).

## Overlap vs gitnexus (the sharpest adjacent boundary)

Both look like "code intelligence," but they're built on fundamentally different data and answer different question shapes:

- **gitnexus = a persisted, offline GRAPH INDEX** built once via `analyze`, frozen until re-indexed, computed on the whole repo. Its strengths are relational/aggregate: blast-radius (`impact`), shortest path between two symbols (`trace`), execution-flow discovery by concept (`query`), taint/data-flow (`explain`, needs `--pdg`), pre-commit diff-to-impact (`detect_changes`). It answers **"what depends on this / what breaks if I change it / how does A reach B across the whole call graph."**
- **serena = a LIVE LANGUAGE SERVER session**, always current with the file on disk (no re-index step, no staleness window), but narrowly scoped per-call: one symbol's definition/references/implementations/diagnostics at a time, plus the actual ability to EDIT via symbol-anchored operations (`replace_symbol_body`, `rename_symbol`, `safe_delete_symbol`) and get live compiler diagnostics. gitnexus cannot edit code or report live diagnostics; serena cannot compute aggregate blast-radius/risk scores or execution-flow clusters across the whole repo in one call.
- **Practical rule for co-enabling both:** reach for gitnexus first to understand *shape and risk* ("what's the blast radius of this function, what execution flows touch it"), then serena to *act precisely* ("show me this exact symbol's live references/diagnostics, then edit its body via LSP-aware rename/replace"). gitnexus `impact`+`rename` (graph-aware, dry-run, multi-file, but text-search fallback tagged `confidence:"text_search"` when the graph edge is missing) vs serena `rename_symbol` (LSP-precise but no dry-run, no risk score, and requires the language server to be warmed up) is the single most-confusable pair — the differentiator write-up should be explicit that gitnexus's rename is graph+regex hybrid with preview, serena's is LSP-only with no preview but stronger cross-reference precision when the LSP fully understands the language.
- Neither replaces `get_diagnostics_for_file` — gitnexus has no compiler/linter diagnostic capability at all; that's purely serena's.

## Footguns / miss-signals for the tuning instructions to warn about

1. **The project-activation gate blocks nearly everything**, including read-only tools like `list_memories`, until `activate_project` is called with a valid path — confirmed live (empty `[]` known-projects list on a fresh gateway session). Instructions must tell the agent to activate first, always, before any other serena call.
2. **LSP warm-up race**: symbolic tools return silently sparse/empty results (not errors) right after activation, before language-server indexing finishes. An empty `find_symbol` immediately post-activation is a false negative, not "symbol doesn't exist" — worth an explicit warning since it's a silent miss, the worst kind.
3. **Claude Code's own tool-choice bias**: per deepwiki, Claude Code agents strongly favor their own built-in Read/Edit/Grep over serena's symbolic tools because CC's native tool descriptions are long and detailed, creating gravitational pull. Upstream's own fix is a system-prompt override (`serena prompts print-cc-system-prompt-override`) — not applicable here since the gateway controls broadcast text, not the system prompt, but it means the retuned descriptions need to be UNUSUALLY assertive about "use this instead of Read/Edit for symbol-level work" or the tools will sit unused even when correctly tuned.
4. **`execute_shell_command` has no sandbox and no enforced timeout** — treat as equivalent-risk to raw Bash access, and it adds zero capability Bash doesn't already have. Best candidate for hiding outright given it's also a duplicate.
5. **`replace_symbol_body` requires a prior `include_body=True` read of the same symbol** — the tool's own description states this as a precondition; skipping it is a documented way to corrupt a symbol body blindly.
6. **Onboarding is a real side-effecting workflow, not documentation** — calling `onboarding` (or activating a project with zero existing memories, which auto-triggers it) kicks off a multi-file memory-writing sequence. Don't let differentiation drafts treat it as a cheap "read this" call.
7. **rename_symbol / safe_delete_symbol have no dry-run** (unlike gitnexus's `rename`) — there's no preview step; `safe_delete_symbol` at least refuses and reports references if unsafe, but `rename_symbol` just executes.

## Sync note

If the gateway ever launches serena with an explicit `--context` flag (e.g. `claude-code`) instead of the default unrestricted context, the actual visible tool count will drop below 30 (removing the native-duplicate set) — re-capture the baseline and refresh this file if that launch command changes; the "duplicate vs unique" section above assumes the current default/full-context launch is what's captured.
