# context7 — research cache

**Researched:** 2026-07-03
**Endpoint:** `https://mcp.context7.com/mcp` (hosted; server_info name "Context7" v3.2.2)
**Tools seen (2):** `resolve-library-id`, `query-docs`
**Sources:** live probes through the gateway (`/admin/api/run`); captured defaults `~/.local/state/mcp-gateway/defaults/context7.json`; context7.com; github.com/upstash/context7 (npm README calls the doc tool `get-library-docs` — that name is STALE; the hosted server exposes it as `query-docs`).

> Corpus in one line: Context7 ingests a library's own docs + source (GitHub repos, doc sites, llms.txt) and stores them as parsed, versioned **code snippets** keyed by a `/org/project` (optionally `/org/project/version`) ID. You always resolve a name → ID first, then query that ID for snippets relevant to a task.

---

## resolve-library-id

**What it really does:** Fuzzy-searches Context7's library catalog for a package/product name and returns a ranked list of candidate libraries with their Context7 IDs and quality metadata. It is the mandatory first hop before `query-docs` — the doc tool needs an exact `/org/project` ID, and this is where you get it. (Exception: if the user already gave a literal `/org/project` ID, you can skip straight to `query-docs`.)

**Inputs (both required):**
- `libraryName` — the name to search for. Docs ask for official punctuation ("Next.js", not "nextjs"), but matching is fuzzy enough that it isn't strict.
- `query` — the task/question. Used server-side to *rank* results by relevance; it does not filter and is not the doc query itself.

**Output (observed):** plain text, `----------`-delimited blocks, one per candidate. Each block:
```
- Title: FastMCP
- Context7-compatible library ID: /prefecthq/fastmcp
- Description: ...
- Code Snippets: 4105
- Source Reputation: High
- Benchmark Score: 84.18
- Versions: v3.2.0, v3.2.4          (only when versioned docs exist)
```
FastMCP probe returned ~1.9KB / 5+ candidates; latency ~7.8s. Not every block carries a Versions line.

**Reading the metadata (for picking an ID):**
- **Code Snippets** — count of parsed examples for that library; higher = more coverage. Wildly variable (38 → 25,492 across FastMCP entries).
- **Source Reputation** — High / Medium / Low / Unknown authority signal.
- **Benchmark Score** — 0–100 quality/parse-fidelity indicator (100 best); mid-80s is a strong entry.
- Multiple entries commonly share the same name (official repo `/prefecthq/fastmcp`, a docs-site mirror `/websites/gofastmcp`, an `/llmstxt/...` variant, plus forks). Prefer the authoritative `/org/project` with High reputation and a healthy snippet count — but the highest snippet count may be an aggregate llms.txt dump, not the cleanest source.

**Quirks:**
- Self-imposed budget baked into the description: "do not call more than 3 times per question."
- The `query` param is easy to misuse as the doc question — it's only a ranking hint here.

**MISS-SIGNAL:** nonsense name → NOT an error object. `{ok:true, is_error:false}` with text:
> `No libraries found for "zzzqqxnonexistentlib99". Try a different search term.`
So a miss is a normal 200 with prose. Detect it by the "No libraries found" / "Try a different search term" text, not by a status flag.

---

## query-docs (npm README's stale `get-library-docs`)

**What it really does:** Given an exact Context7 ID and a task query, returns the most relevant parsed doc snippets for that library — title + source URL + code block per snippet, `--------------------------------`-delimited.

**Inputs (both required):**
- `libraryId` — exact `/org/project` or `/org/project/version` (from resolve, or supplied literally by the user).
- `query` — the actual documentation question; specificity matters ("How to set up JWT auth in Express.js", not "auth").

**Output (observed):** plain text, multiple snippet blocks. Each:
```
### Create Nested FastMCP Mounts in Starlette
Source: https://github.com/prefecthq/fastmcp/blob/main/docs/deployment/http.mdx
<prose>
```python
...
```
```
`/prefecthq/fastmcp` + a create_proxy/Starlette query returned ~3.4KB, ~5 snippets, ~6.2s. Snippets are real doc excerpts with upstream source links — good grounding, moderate size. Payload scales with how much matches; a broad query on a big library returns more.

**Quirks:**
- Snippets come straight from the library's own docs/source, so freshness tracks Context7's last ingest of that repo, not your model's training cutoff — this is the whole point of the backend.
- Same 3-calls-per-question budget note.
- No pagination/count knob observed on the hosted surface (npm variant historically had a `tokens`/topic param; not exposed here).

**MISS-SIGNAL:** bad/unknown ID → again `{ok:true, is_error:false}` with text:
> `Library "/org/doesnotexist-zzz" not found. Please check the library ID or your access permissions.`
Note "or your access permissions" — a valid-but-gated ID could surface the same message, so don't assume "not found" strictly means nonexistent.

---

## Auth / rate limits (partly unverified)
- Hosted endpoint works anonymously (all probes succeeded with no key). Context7 README: a **free API key** (context7.com/dashboard) is "recommended" and raises rate limits — anonymous use is rate-limited per IP. Exact anonymous ceiling not measured this session (unverified); if calls start returning throttle text, an API key in the gateway config is the fix.
- The gateway forwards whatever auth the backend config carries; none was needed for these reads.

## Corpus notes
- Indexed units are **code snippets** parsed from a library's GitHub repo, documentation site, and/or `llms.txt`. Version-specific docs exist for some libraries (the `/org/project/version` form and the Versions line).
- Catalog is large and community-submittable (libraries added via context7.com); expect duplicate/mirror entries per popular library — reputation + benchmark score are how you disambiguate.
- **Context7 DOES index Claude / Anthropic docs** (it indexes any doc site including Anthropic's). This is the key overlap boundary with cc-docs — see below.

---

## Overlap vs siblings (distinguishers)

- **vs cc-docs (Claude Code docs only):** Both can surface Claude/Anthropic material — context7 indexes Anthropic doc sites too. Boundary: **cc-docs is the narrow, authoritative, always-current mirror of the Claude Code / Anthropic docs**; context7 is the broad multi-library catalog where Anthropic docs are one entry among tens of thousands and freshness depends on last ingest. For a Claude Code / Anthropic-specific question, prefer cc-docs (canonical, no resolve step, no version ambiguity). Reach for context7 only when the question spans other libraries too.
- **vs deepwiki (a specific GitHub repo's internals):** context7 answers **"how do I USE this library"** — public API syntax, config, version-specific code snippets drawn from its docs. deepwiki answers **"how is this repo BUILT"** — architecture, internal design, why-it-works Q&A about one named GitHub repo. Same library can be in both: context7 for its documented API surface, deepwiki for its internal implementation. If the user wants working code against a published API → context7; if they want to understand a repo's structure/design → deepwiki.
- **vs Exa search_web / fetch_url:** Exa is open-web search + raw page extraction for anything (news, people, companies, arbitrary URLs). context7 is a curated, pre-parsed, snippet-structured docs corpus for *libraries/frameworks/SDKs*. For "current API of library X with copy-pasteable snippets," context7 beats Exa (structured, deduplicated, version-aware). For anything not a library doc — or a specific known URL — Exa.
- **vs Tavily:** same axis as Exa — general web search/answer vs. context7's structured library-docs specialization. Prefer context7 whenever the target is a programming library's usage/API/config; Tavily for general-purpose web Q&A.

**One-line boundary:** context7 = *how to USE a library* (versioned API usage + current code snippets, resolve-then-query) · deepwiki = *how a repo is built* · cc-docs = *canonical Claude/Anthropic docs* · Exa/Tavily = *the open web*.
