# research/cc-docs.md

**Researched:** 2026-07-03
**Endpoint:** `https://code.claude.com/docs/mcp` (Streamable HTTP). Public, no-auth, free. The **official** Claude Code Docs MCP server, run by Anthropic. Server `Claude Code Docs` v1.0.0. Currently ENABLED and mounted at the gateway (`/cc-docs/mcp`).
**Tools seen (this tier):** `search_claude_code_docs`, `query_docs_filesystem_claude_code_docs`, `submit_feedback`. (Provider-native names already carry the `_claude_code_docs` suffix — see naming problem below.)
**Sources:** raw capture `~/.local/state/mcp-gateway/defaults/cc-docs.json`; live probes through the gateway `/admin/api/run`; the docs describe this very server as the canonical first-server example (`code.claude.com/docs/en/mcp-quickstart`, `.../agent-sdk/mcp`). No standalone press release — it is documented *inside* the docs it serves ("a hosted server with full-text search over the Claude Code docs").

---

## What cc-docs is (backend-level)
Anthropic's own documentation MCP for the Claude Code docs site (`code.claude.com/docs`). Two ways in: a semantic **search** and a **virtualized read-only filesystem** of the doc pages; plus a **feedback** write-back. It is canonical and current (regenerated with the site), with no resolve/index step and no auth — which is exactly why the quickstart uses it as everyone's first test server.

**Corpus (probed, important):** the filesystem root `/` holds one dir per language (`de en es fr id it ja ko pt ru zh-CN zh-TW`). `/en` = **118 entries**: the full Claude Code product docs **plus the Agent SDK** (`/en/agent-sdk/` — agent-loop, hooks, hosting, session-storage, slash-commands, streaming, etc.) and a weekly changelog (`/en/whats-new/2026-w13.mdx` … `2026-w26.mdx`). Pages are `.mdx`.
**Boundary confirmed:** it is Claude Code + Agent SDK **product** docs. It does NOT contain the Anthropic platform/Messages API reference — `find` for `*api-reference*` / `*messages*` / `*.json` (OpenAPI) returned nothing. So questions about the Claude API (models, `/v1/messages`, token counting) are OUT of scope here even though the tool's own description generically mentions "OpenAPI specs" (that phrasing is boilerplate from a shared template — no OpenAPI file exists in this corpus).

---

## Per tool

### search_claude_code_docs(query, language?)
**Does:** semantic/full-text search over the docs; returns ranked chunks, each with Title, Link (canonical `code.claude.com/docs/...` URL), Page path, and the Content excerpt. The conceptual entry point.
**Inputs:** `query` (required); `language` (optional, e.g. `zh`/`es`, defaults `en`).
**Output (observed):** `{ok, is_error, ms, content:[{type:"text", text}]}` — one text block per hit. `query="hooks"` returned multiple well-formed chunks (glossary Hook, `/hooks` menu, HTTP hooks with a JSON code sample), ~16.5KB total, ~6.1s. Rich, link-anchored, ready to cite.
**Use:** broad/conceptual questions ("how to authenticate", "what is a hook"). Its own description tells the agent to follow up with the filesystem tool (`head`/`cat` the `.mdx` path, appending `.mdx`) for a full page.

### query_docs_filesystem_claude_code_docs(command)
**Does:** runs a read-only shell-like command against an **in-memory virtualized filesystem** of the doc pages (NOT a real shell — no host, no network, no writes). This is the "get page" mechanism: `head`/`cat` an `.mdx` path to read it; `rg`/`grep` for exact keyword/regex; `tree`/`ls` for structure. Supported: rg, grep, find, tree, ls, cat, head, tail, stat, wc, sort, uniq, cut, sed, awk, jq + basic text utils.
**Input:** `command` (a single shell string).
**Output (observed):** `{content:[{text:"exit: <code>\n--- stdout ---\n<...>"}]}`. `tree / -L 1` → the 12 language dirs, ~4.7s. `rg -il "subagent" /en` → clean path list. Per-call output truncated to **30KB**.
**STATELESS cwd (verified by design + probes):** every call resets working dir to `/`; no vars/aliases/history carry between calls. Chain with `&&` or use absolute paths in ONE call. An agent that does `cd /en` then `ls` in a *separate* call gets the root, not `/en` — a real miss-signal for multi-step exploration.

### submit_feedback(path, feedback)
**Does:** the only write tool — reports a doc problem (wrong/outdated/confusing/incomplete) to the docs team for a given page path. `readOnlyHint:false, openWorldHint:true`. **Not probed** (would spam Anthropic's real docs team — don't fire it in verification). Narrow, human-in-the-loop; low value for an agent answering questions, and arguably noise in a docs-*lookup* backend.

---

## THE TRUNCATION FINDING (known defect)
`query_docs_filesystem_claude_code_docs`'s description is **2608 bytes** — 560 bytes OVER the gateway's 2KB (2048B) broadcast cap. Truncation is from the END, so the agent never sees the last 560 bytes, which contain THREE distinct pieces of guidance:

1. The tail of the final example line — `...ys'` — list OpenAPI endpoints` (the `cat /openapi/spec.json | jq '.paths | keys'` example is cut mid-word; harmless since no OpenAPI file exists anyway).
2. The **entire output-budget paragraph**: "Output is truncated to 30KB per call. Prefer targeted `rg -C` or `head -N` over broad `cat`… To read only the relevant sections of a large file, use `rg -C 3 "pattern"`… Batch multiple file reads into a single `head`/`cat` call." — i.e. all the efficiency guidance is lost.
3. The **entire URL-conversion instruction**: "When referencing pages in your response to the user, convert filesystem paths to URL paths by removing the `.mdx` extension… `/quickstart.mdx` becomes `/quickstart`." — so a cold agent reading only the broadcast won't know to strip `.mdx` when citing.

The other two descriptions are safely under cap (search 605B, submit_feedback 315B; instructions 1279B). **Fix options for a tuning pass:** trim the filesystem-tool description under 2048B by (a) dropping the now-irrelevant OpenAPI line, (b) compressing the examples block, and (c) preserving the two lost paragraphs (30KB/efficiency + `.mdx`→URL) because those are the load-bearing behaviors. Net: it's not a hard failure — the tool works — but the agent loses cost-discipline and citation-hygiene guidance it was meant to have.

---

## The naming problem (rename candidates)
Provider-native names already bake the corpus into the tool name: `search_claude_code_docs`, `query_docs_filesystem_claude_code_docs`. On the gateway the full callable is `mcp__gateway-cc-docs__query_docs_filesystem_claude_code_docs` — **absurdly long**, and the `-cc-docs-` endpoint segment already says "claude code docs," so the `_claude_code_docs` suffix is pure redundancy that costs tokens on every tool-list and every call.

Name-only guessability for a cold agent: `search_claude_code_docs` is fine (obvious). `query_docs_filesystem_claude_code_docs` is **poor** — "query_docs_filesystem" doesn't signal "this is how you READ a page / grep the docs"; an agent may not realize it's the get-page + rg tool, and the length invites truncation and mis-typing.

**Rename candidates** (broadcast name only; each backend is its own endpoint so bare names are safe and unique-within-backend):
- `search_claude_code_docs` → `search_claude_docs` (drop `_code`; still unambiguous under the `cc-docs` endpoint).
- `query_docs_filesystem_claude_code_docs` → `read_claude_docs_fs` (or `grep_claude_docs`) — surfaces the two real jobs (read a page / regex-search) and cuts ~18 chars.
- `submit_feedback` → keep (already bare and clear).
NOTE per repo convention: GitHub-issue-tracked names are frozen; treat these as proposals for the drafting step, not applied here.

---

## Overlap vs siblings (distinguishers → differentiation.md)

- **vs context7 `query-docs` (sharpest live boundary).** context7 ALSO indexes Anthropic/Claude docs, so this is a genuine two-sibling overlap. **cc-docs = canonical, always-current, no resolve step, Claude-Code-+-Agent-SDK-only.** context7 = a *library* docs index spanning thousands of projects, needs a `resolve-library-id` step, and is best when the question crosses into other libraries (Next.js + Claude, LangChain + Claude, etc.) or wants version-pinned snippets. Rule: a question purely about Claude Code / the Agent SDK → cc-docs (fresher, authoritative, one hop). A question that spans Claude *plus other libraries*, or needs a specific library version → context7. Do not send Claude-Code config questions to context7.
- **vs the `notes/web-docs-markdown.md` trick (fetch_url on `code.claude.com/docs/*.md`).** Same corpus, different access. The `.md`/`.mdx` fetch trick is free and gives a clean full page **when you already know the URL** — no search, no discovery. cc-docs adds semantic search over the whole corpus AND regex/structure exploration of every page in one backend. Use the fetch trick for a known page you can name; use cc-docs when you must *find* the right page or grep across pages.
- **Is the filesystem tool a better WebFetch for these docs?** For Claude Code docs, **yes** — `head`/`cat` on an `.mdx` path returns the raw doc without HTML/nav boilerplate, `rg` finds exact strings across the whole set, and it's free/no-auth. WebFetch/fetch_url still win for *arbitrary* web pages and for a single known URL you don't need to search for; cc-docs only knows this one corpus.
- **vs deepwiki.** No real overlap: deepwiki = how a specific GitHub *repo* is built internally (Claude Code is not open-source, so its repo isn't there). cc-docs = the product docs. "How do I configure a hook" → cc-docs; "how does repo X implement its scheduler" → deepwiki.
- **vs Exa / Tavily.** Those are the open web. cc-docs is the single authoritative Claude-Code corpus. Reach for Exa/Tavily only when the answer isn't in the docs (blog posts, announcements, third-party tutorials).

**One-line intuitions for the differentiator:**
- cc-docs `search_claude_code_docs` = "find the answer in the official Claude Code / Agent SDK docs (current, no auth, cite the link)."
- cc-docs filesystem tool = "read or regex a specific Claude Code doc page — a boilerplate-free WebFetch scoped to these docs (stateless cwd; chain with `&&`)."
- Not for: the Claude *API/Messages* reference (not in corpus → context7 or the web), other libraries (→context7), repo internals (→deepwiki), the open web (→Exa/Tavily).
