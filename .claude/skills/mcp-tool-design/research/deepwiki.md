# research/deepwiki.md

**Researched:** 2026-07-03
**Endpoint:** `https://mcp.deepwiki.com/mcp` (Streamable HTTP). Public, no-auth, free tier. Server `DeepWiki` v2.14.3 (FastMCP-wrapped).
**Tools seen (this tier):** `read_wiki_structure`, `read_wiki_contents`, `ask_question` (gateway rebroadcasts `ask_question` → `ask_repo_questions`). All 3 take `repoName` in `owner/repo` form.
**Sources:** raw capture `~/.local/state/mcp-gateway/defaults/deepwiki.json`; live probes through the gateway `/admin/api/run`; docs.devin.ai/work-with-devin/deepwiki(-mcp); cognition.ai/blog/deepwiki; codersera DeepWiki guide 2026.

---

## What DeepWiki is (backend-level)
Cognition's (Devin's makers) "AI docs you can talk to, for every repo." It ingests a public GitHub repo's code + READMEs + configs and generates a structured wiki (architecture, per-subsystem pages, diagrams) plus a grounded Q&A layer ("Deep Research for GitHub"). 50,000+ of the most-starred public repos are **pre-indexed** (React, LangChain, Next.js, TensorFlow, MCP, vscode…). Any other public repo can be indexed on demand for free by visiting its deepwiki.com page and clicking "index this repo" (first-gen 30s–few min, then permanent). Private repos require a paid Devin account + the *private-mode* server — not this tier.

Core boundary: DeepWiki answers **"how does THIS specific GitHub repo work internally"** — architecture, subsystem design, where a behavior lives, why. It is repo-scoped understanding, not API-usage lookup.

### The 13 private-mode tools — irrelevant, ignore
The server's `instructions` advertise 10 extra tools (`list_available_repos`, `generate_wiki`, `devin_automation_manage`, `devin_knowledge_manage`, `devin_playbook_manage`, `devin_schedule_manage`, `devin_session_create/interact/events/search`, `list_integrations`) all flagged "(private mode only)". This no-auth tier **can never call them** — only the 3 above are wired as callable tools in the capture. They exist only as noise in the broadcast instructions; a tuning pass should consider trimming that list from the rebroadcast instructions so a cold agent isn't tempted.

---

## Per tool

### read_wiki_structure(repoName)
**Does:** returns the wiki's table of contents for the repo — a numbered/nested outline of documentation topics (e.g. "2 Core Reconciler Architecture → 2.1 Fiber Work Loop and Scheduling"). Cheap orientation: use it to see what pages exist before pulling contents or asking.
**Input:** `repoName` only, `owner/repo`.
**Output (observed):** plain-text nested bullet outline. facebook/react probe: ~8 top sections, ~35 sub-topics, ~1.3KB. Wrapped as `{result: "<string>"}` (FastMCP `x-fastmcp-wrap-result`). Latency ~5–7s.
**Quirk:** no page IDs returned — just human titles; you can't address a single page, `read_wiki_contents` pulls the whole wiki.

### read_wiki_contents(repoName)
**Does:** returns the **full** rendered wiki documentation text for the repo (all pages), not a single section. Heavy. Use when you want the whole architecture narrative; for a targeted question prefer `ask_question`.
**Input:** `repoName` only.
**Output:** large markdown-ish document (string). Not probed to completion (bulk) — expect multi-KB to tens-of-KB depending on repo size.
**Quirk:** no way to request one sub-page; it's all-or-nothing. Token-heavy — the expensive tool of the three.

### ask_question(repoName, question) — broadcast as ask_repo_questions
**Does:** natural-language Q&A grounded in the repo's indexed code. Returns a synthesized, **citation-marked** answer that names actual files/functions (e.g. `shouldYieldToHost()` in `packages/scheduler/src/forks/Scheduler.js`). This is the high-value tool — precise, source-anchored answers about internals.
**Inputs:** `repoName` (or a list, **max 10** repos, per the description) + `question`.
**Output (observed):** structured markdown answer with headings and bullet points, inline source-citation glyphs. facebook/react "when does the Fiber loop yield" probe returned a correct, detailed multi-paragraph answer citing exact symbols. Latency high: ~16s. Wrapped `{result: <string>}`.
**Quirk:** slowest tool (does retrieval + generation). The multi-repo `repoName` list is a real feature — can compare/ask across up to 10 repos in one call.

---

## Failure modes & MISS-SIGNALS

- **Unindexed / nonexistent repo (the big one):** the call **succeeds at the transport level** — `ok:true, is_error:false` — but the `content` text is an error string:
  `Error fetching wiki for <owner>/<repo>: Repository not found. Visit https://deepwiki.com/<owner>/<repo> to index it.`
  Observed for both a fabricated repo and a real-but-unindexed public repo (`voidfreud/mcp-gateway`). **MISS-SIGNAL for the agent:** the tool "works" but the answer is literally a "Repository not found … Visit … to index it" sentence. An agent must string-match this, not trust `is_error`. There is **no auto-indexing** via MCP — indexing only happens through the deepwiki.com web prompt, so on this signal the tool is a dead end for that repo until a human indexes it.
- **Wrong tool name:** `ask_question` via the gateway run endpoint returned `is_error:true, "Unknown tool: 'ask_question'"` because the live gateway config **renames** it to `ask_repo_questions` (the run endpoint dispatches on the *broadcast* name, not the original). Note for tuning/verify: probe with the effective name.
- **Latency, not error:** ask_question ~16s, reads ~5–7s. No error, just slow — an agent on a tight budget may perceive a hang.
- Not probed: rate limits (docs state none publicly), stale-index behavior (indexed wiki reflects the branch/time it was generated, so answers can lag the repo's HEAD — a correctness caveat, not an error).

---

## Overlap vs siblings (distinguishers → differentiation.md)

- **vs context7** — the sharpest boundary. **DeepWiki = understand a specific repo's internals/architecture** ("how does React's Fiber scheduler decide to yield", "where is auth handled in this repo"). **context7 = a library's public API usage + current code snippets** ("show me the current `useQuery` signature / a working Next.js middleware snippet"). DeepWiki answers *why/how it's built*; context7 answers *how do I call it*. If the agent wants to consume a library, context7; if it wants to comprehend or contribute to a codebase, DeepWiki.
- **vs cc-docs** — cc-docs is Claude Code product docs only; no overlap except that a question about Claude Code's own repo internals would go to DeepWiki (repo) while "how do I configure a hook" goes to cc-docs.
- **vs Exa search_web / fetch_url** — Exa is general open web (news, people, companies, arbitrary page text). DeepWiki is repo-scoped and pre-digested. Use Exa to *find* a repo or read a blog; DeepWiki to *understand* a known repo's code. Exa fetch_url on a GitHub file gives raw text; DeepWiki gives synthesized, cited architecture.
- **vs Tavily search/extract/research** — same axis as Exa: Tavily is general web retrieval/research. DeepWiki is not a web searcher; it only knows indexed GitHub repos.
- **vs gitnexus (local code tools)** — gitnexus indexes repos **we have checked out locally** (call-graph, impact, taint, exact symbols, always current with the working tree). DeepWiki covers **any popular public repo we do NOT have locally**, at a higher/architectural altitude, possibly stale. Rule: local checkout → gitnexus; external/unfamiliar public dependency → DeepWiki. gitnexus is precise and current; DeepWiki is broad and pre-generated.

**One-line intuitions for the differentiator:**
- DeepWiki `ask_question` = "explain how <public GitHub repo> works internally, with file/function citations."
- Not for: calling a library's API (→context7), searching the web (→Exa/Tavily), or a repo we have checked out (→gitnexus).
- Watch the unindexed miss-signal: a `Repository not found … Visit … to index it` string comes back as a *successful* call.
