# research/gitnexus.md

**Researched:** 2026-07-03
**Backend:** LOCAL stdio MCP. Command `~/.local/bin/gitnexus` (`gitnexus mcp`, stdio by default). Server `gitnexus` v1.6.8 (matches the capture's `server_info`). Currently **disabled** in the gateway.
**Tools seen (17, matches capture):** `list_repos`, `query`, `cypher`, `context`, `detect_changes`, `check`, `rename`, `impact`, `explain`, `pdg_query`, `route_map`, `tool_map`, `shape_check`, `api_impact`, `group_list`, `group_sync`, `trace`. Server-level `instructions: null` (no server instructions broadcast).
**Sources (all free + local):** capture `~/.local/state/mcp-gateway/defaults/gitnexus.json`; installed source `~/Developer/tools/gitnexus/prefix/lib/node_modules/gitnexus/dist/mcp/tools.js` (+ `resources.js`, `local/`); `gitnexus --help` and subcommand help; the six `gitnexus-*` skills at `~/Developer/mine/mcp-gateway/.claude/skills/gitnexus/*/SKILL.md`; live CLI probes against the indexed `mcp-gateway` repo (query, context, impact, trace, check, cypher).

---

## What GitNexus is (backend-level)

A local code-intelligence engine (looptech-ai/understand-quickly family). `gitnexus analyze <path>` parses a **locally checked-out** repo into a knowledge graph stored under the repo's `.gitnexus/` (LadybugDB, a Kùzu-style embedded graph DB), registered in a global registry. The MCP server queries that persisted graph. Nodes: File, Folder, Function, Class, Interface, Method, Property, plus multi-language `Struct`/`Enum`/`Trait`/`Impl`, and derived `Community` (functional area, Leiden clustering), `Process` (execution flow, entry→terminal), `Route`, `Tool`. All edges live in one `CodeRelation` table keyed by `type` (CALLS, IMPORTS, EXTENDS, IMPLEMENTS, HAS_METHOD, HAS_PROPERTY, ACCESSES, METHOD_OVERRIDES, STEP_IN_PROCESS, HANDLES_ROUTE, HANDLES_TOOL, …) with `confidence` and `reason` props.

Core boundary: GitNexus answers **"how does THIS locally-checked-out repo actually wire together, right now"** — precise call-graph, blast-radius, execution flows, taint — computed from the working tree at index time. It is Alex's own tool; the gateway repo itself is indexed by it (see mcp-gateway `CLAUDE.md`).

**Two optional layers, off by default** — the biggest correctness caveat for tuning:
- **PDG/taint layer** (`analyze --pdg`): adds BasicBlock nodes + CFG / CDG / REACHING_DEF / TAINTED / TAINT_PATH / SANITIZES edges. Consumed by `explain` (taint) and `pdg_query` (control/data dependence). **Without it those two tools return a plain "no layer" note, not an error.**
- **Web/API layer** (`Route`/`Tool` nodes, extracted during indexing from JS/TS fetch calls, `.json({...})` responses, MCP/RPC tool defs): consumed by `route_map`, `tool_map`, `shape_check`, `api_impact`. **A repo with no such nodes (e.g. a plain Python or library repo) makes these four return empty** — they light up mainly on Next.js/Express-style web apps.

**Registry state at research time:** 4 repos indexed — `vesper`, `tweakcc-fixed`, `mcp-gateway`, `voidfreud-toolkit`. **Because more than one repo is indexed, the `repo` param is effectively REQUIRED on every tool** (the descriptions say "omit if only one repo is indexed" — that "if" is false here). Omitting it against a multi-repo registry is a live miss-signal (ambiguous/wrong-repo). `mcp-gateway`'s index was built **without `--pdg`** (probe: `MATCH (b:BasicBlock) RETURN count(b)` → 0) and has **0 Route / 0 Tool nodes** — so on the gateway's own repo, `explain`/`pdg_query` and all four web tools are inert.

---

## Per tool (what it really does, shapes, quirks)

### list_repos(limit?, offset?)
Enumerates the global registry, paginated (default 50, max 200; out-of-range **rejected, not capped**). Returns `{ repositories:[{name,path,indexedAt,lastCommit,stats}], pagination:{total,limit,offset,returned,hasMore,nextOffset} }`. Stable order (lower-cased name, then path) so paging is safe. **First step when multiple repos exist** — get the exact `name` to pass as `repo` everywhere else. `offset ≥ total` → empty page, `total` still reported.

### query(search_query, task_context?, goal?, limit?=5, max_symbols?=10, include_content?, repo?, service?, branch?)
Semantic + keyword (BM25) hybrid, RRF-ranked. Returns **execution flows**, not file matches: `{ processes:[{id,summary,priority,symbol_count,process_type,step_count}], process_symbols:[{id,name,filePath,startLine,endLine,module,process_id,step_index}], definitions:[…] }` (probe confirmed). The concept→flow entry point; the thing to reach for **instead of grep** when you want "how does X work". `task_context`/`goal` nudge ranking only. Group mode via `repo:"@group"`.

### context(name?|uid?, file_path?, kind?, include_content?, repo?, …)
360° view of ONE symbol: `{status, symbol:{uid,name,kind,filePath,startLine,endLine}, epistemic, incoming:{calls,imports,…}, outgoing:{…} }` (probe confirmed — categorized in/out refs, ACCESSES read/write, process participation). Ambiguous `name` → ranked candidates with scores; `uid` = zero-ambiguity. Step **after** `query` to drill into a symbol. `epistemic:"exact"` on a clean hit.

### impact(target, direction, mode?=callgraph, line?, maxDepth?=3, relationTypes?, summaryOnly?, limit?, minConfidence?, repo?, …)
Blast radius. `direction` is **required** (`upstream` = what depends on this / what breaks; `downstream` = what this needs). Returns `{target, direction, impactedCount, risk:LOW|MEDIUM|HIGH|CRITICAL|UNKNOWN, byDepthCounts:{1,2,3}, affected_processes, affected_modules, byDepth:[…]}` (probe: `load_secrets upstream` → risk HIGH, 4 impacted). Depth semantics: **d1 = WILL BREAK, d2 = LIKELY, d3 = MAY NEED TESTING.** For hub symbols use `summaryOnly:true` first (byDepth can explode; `limit`/`offset` are **per-depth**, not total). `mode:'pdg'` needs `--pdg` and returns statement-level `affectedStatements` + `pdgResultVersion:1`, always `risk:'UNKNOWN'`; incompatible with `@group`/crossDepth. This is the tool the gateway's own `CLAUDE.md` mandates before any edit.

### trace(from?, to?, from_uid?/to_uid?, from_file?/to_file?, maxDepth?=10, includeTests?, repo?, …)
Shortest directed path between two symbols over CALLS (+ HAS_METHOD, so a class root descends into methods). Returns `{status:ok|no_path|ambiguous|not_found|error, hops:[{name,filePath,startLine}], edges:[{relType,confidence}]}`. On no path: `{status:"no_path", furthest:{…,depth}, suggestion}` (probe confirmed — reports where the chain breaks, sets `truncated:true` if a cap hit first). Answers "how does A reach B?" in one call instead of 3–8 manual `context` hops. **Note:** it's directed — `load_secrets→expand_env` returned `no_path` because the real edge runs the other way.

### detect_changes(scope?=unstaged, base_ref?, worktree?, repo?, …)
Maps `git diff` hunks → indexed symbols → affected processes + risk summary. Reads the **working tree live** (auto-detects linked worktrees; pass `worktree` only if the server was launched elsewhere). `scope`: unstaged / staged / all / compare (`compare` needs `base_ref`, e.g. `main`). The pre-commit / PR-prep tool; gateway `CLAUDE.md` mandates it before every commit. **This is where "always current with the working tree" is literally true** — it diffs, it doesn't rely on the frozen index for the changed lines.

### check(cycles?=true, repo?, …)
Read-only structural invariant checks; today only **circular File→File IMPORTS cycles**. Deterministic cycle paths + count, CI-shaped. Narrow tool — cycles only.

### rename(symbol_name?|symbol_uid?, new_name, file_path?, dry_run?=true, repo?, …)
Graph-aware multi-file rename. Each edit tagged `confidence:"graph"` (high, from graph edges) or `"text_search"` (regex, review). **Preview by default** (`dry_run:true`). The safe alternative to find-and-replace; follow with `detect_changes` to confirm. **Only write-capable tool** besides `group_sync`.

### explain(target?, limit?=50, repo?, …)  — TAINT
Enumerates persisted **taint findings** (source→sink): intra-procedural `TAINTED` hops AND cross-function `TAINT_PATH` (marked `interprocedural:true`). Categories: command-/code-injection, path-traversal, sql-injection, xss. Anchorless = all findings (bounded, `totalFindings`/`truncated`); anchored `target` = file (suffix match) or function (resolved like `context`). **Requires `analyze --pdg`; without it → clear "no taint layer" note, NOT an error.** Big soundness caveats baked into its own description: closure/callback flows invisible, property/field flows untracked, guard sanitizers not modeled — **absence of a finding is not proof of safety.** Not part of `impact`'s traversal — dedicated taint consumer.

### pdg_query(mode, target, variable?, limit?=50, repo?, …)  — CONTROL/DATA DEPENDENCE
The control/data analog of `explain`. **Always anchored** (`target` required — no whole-repo scan; raw BasicBlock path scans are unbounded). `mode:"controls"` = CDG "under what condition does X run?" (predicate→dependent block, branch sense `'T'`/`'F'` in `reason`, early return/throw flagged `guard:true`). `mode:"flows"` = REACHING_DEF def→use within the function (`variable` filters one binding). **Requires `--pdg`; else "no PDG layer" note.** Intra-procedural only (cross-function = taint's job). Basic-block granular.

### route_map / tool_map / shape_check / api_impact  — WEB/API cluster (need Route/Tool nodes)
- **route_map(route?, repo?)** — API route→handler→consumer map + middleware wrapper chains (withAuth, withRateLimit). Finds orphaned routes.
- **tool_map(tool?, repo?)** — MCP/RPC tool defs: which tools exist, handler files, descriptions.
- **shape_check(route?, repo?)** — compares a route's response keys (from `.json({...})`) against consumers' property accesses; reports `MISMATCH` on shape drift. Needs routes with `responseKeys`.
- **api_impact(route?|file?, repo?)** — pre-change report for a route handler; combines route_map + shape_check + impact into consumers/fields/middleware/flows with LOW/MEDIUM/HIGH risk (by consumer count / mismatches). The one to reach for before editing a handler.

All four are **JS/TS-web-app oriented** and go **empty on repos with no Route/Tool nodes** (confirmed: `mcp-gateway`, a Python repo, has 0 of each). Not errors — just nothing to show.

### group_list / group_sync  — cross-repo contracts
- **group_list(name?)** — list configured repo groups (from `group.yaml`) or one group's config.
- **group_sync(name, skipEmbeddings?, exactOnly?)** — rebuild the Contract Registry (`contracts.json`): extract HTTP contracts across member repos, apply manifest links, cross-link. Run after editing `group.yaml` or re-indexing members. Enables `@group` / `@group/member` targeting on query/context/impact for **cross-service** blast radius. (Descriptions note these are the modern replacement for legacy `group_*` tools; prefer resources for contracts/status.)

### cypher(statement, params?, repo?, branch?)
Raw Cypher against the graph. Returns `{markdown, row_count}` — results as a **Markdown table string** (probe confirmed), not JSON rows. Escape hatch for structural questions the other tools can't phrase. READ `gitnexus://repo/{name}/schema` first. Single `CodeRelation` table → filter `{type:'CALLS'}` etc. PDG edges only exist with `--pdg`.

**Resources (not tools, but the intended orientation reads):** `gitnexus://repo/{name}/context` (stats + staleness — read FIRST), `/clusters`, `/cluster/{name}`, `/processes`, `/process/{name}`, `/schema`.

---

## Failure modes & MISS-SIGNALS

- **Stale index (the big one).** The graph is frozen at `analyze` time; the working tree moves on. `.../context` reports staleness (last commit vs HEAD). **Miss-signal:** answers reference symbols/lines that no longer match the file, or a just-added symbol is absent. Fix: re-run `gitnexus analyze` (or `node .gitnexus/run.cjs analyze`). `detect_changes` is the exception — it diffs live, so it stays current for the changed hunks even against a stale base.
- **Unindexed repo.** A path/name not in the registry → not-found. `list_repos` first to get valid names.
- **Wrong/omitted `repo` in a multi-repo registry.** 4 repos indexed now → omitting `repo` is ambiguous. Always pass the exact name from `list_repos`.
- **`--pdg` not built → `explain`/`pdg_query` return a "no taint/PDG layer" note (success, not error).** Must string-match the note, not trust the error flag. `mcp-gateway`'s index has 0 BasicBlocks — both are inert there until re-indexed with `--pdg`.
- **No Route/Tool nodes → the four web tools return empty (success, not error).** Normal for non-web / library / Python repos. Empty ≠ "no API problems"; it means "nothing extracted."
- **Taint soundness caveats** (per `explain`'s own contract): closure/callback, property/field, and guard-sanitizer flows are not modeled; cross-function matching is by callee name (context-insensitive over-report). **Absence of a finding is not proof of safety.**
- **Empty `query`/`trace` results** mean "no ranked flow / no directed path found," not "nothing there" — `trace` is directed (order matters) and reports `furthest`+`suggestion` so you can see where it broke.
- **impact on hub symbols** can blow the token budget; `byDepth` `limit`/`offset` are per-depth. Use `summaryOnly:true` first.

---

## Overlap vs siblings (distinguishers → differentiation.md)

- **vs deepwiki (the sharpest, most-confusable boundary).** Both "understand a repo." **GitNexus = a repo we have checked out LOCALLY**, precise call-graph / impact / taint computed from the working tree, **current** (modulo re-index), exact `file:line` symbols. **deepwiki = any popular PUBLIC repo we do NOT have locally**, AI-generated architectural wiki, higher altitude, possibly stale, no blast-radius/impact. Rule: **local checkout → gitnexus; external/unfamiliar public dependency → deepwiki.** deepwiki explains *how it's built* in prose; gitnexus computes *what breaks if you change it* and *how A reaches B*.
- **vs the github backend.** github = **remote repo METADATA** (PRs, issues, reviews, CI, file contents via API) — no code-understanding, no graph, no impact. gitnexus = the code's structure/semantics on disk. "What does PR #42 change / is CI green" → github; "what's the blast radius of this symbol / where does this taint flow" → gitnexus. Adjacent tools: gitnexus `detect_changes` (local diff → affected flows) is the pre-commit analog of a github PR view — one is semantic-local, the other metadata-remote.
- **vs plain grep / read (and `rg`).** grep finds literal string matches; gitnexus returns **relationships** — callers, execution flows, blast radius, shortest paths. Alex's `~/Developer/CLAUDE.md` routes "understanding a codebase (architecture, call-graph, impact)" to gitnexus explicitly, grep only for literal finds. Use grep to find a token; gitnexus to understand what it connects to.

**Intra-backend ordering / when each (this matters for the differentiator):**
1. **Orient:** read `gitnexus://repo/{name}/context` (staleness) → `list_repos` if unsure of the name.
2. **Discover a flow by concept:** `query("concept")` → process-grouped hits. (Not grep.)
3. **Understand one symbol:** `context(name|uid)` → callers/callees/processes. Drill from a `query` hit.
4. **Connect two symbols:** `trace(from,to)` → shortest path in one call (vs manual `context` chaining).
5. **Before changing code:** `impact(target, direction:"upstream")` → blast radius + risk. (`api_impact` if it's a web route handler.)
6. **Before committing:** `detect_changes()` → what your live diff touches.
7. **Refactor a name:** `rename(dry_run:true)` → then `detect_changes` to confirm.
8. **Security review:** `explain` (taint) / `pdg_query` (control/data dependence) — **only if `--pdg`**.
9. **Anything structural the above can't phrase:** `cypher` (read `/schema` first).

**One-line intuitions for the differentiator:**
- gitnexus = "for a repo I have CHECKED OUT locally: exact call-graph, blast-radius before I edit, execution flows, shortest path A→B, live-diff impact, and (with --pdg) taint."
- Not for: a public repo I don't have locally (→deepwiki), remote PR/issue/CI metadata (→github), or a literal string find (→rg/grep).
- Watch: **stale index** (re-analyze), **multi-repo → `repo` required**, and the two silent "no layer / no nodes" successes (`explain`/`pdg_query` need `--pdg`; the four web tools need Route/Tool nodes).
