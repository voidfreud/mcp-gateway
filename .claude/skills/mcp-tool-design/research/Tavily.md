# Tavily — research cache

**Researched:** 2026-07-03
**Server:** `tavily-mcp` v3.3.1 (FastMCP-based)
**Tools seen (5, all enabled):** `tavily_search`, `tavily_extract`, `tavily_crawl`, `tavily_map`, `tavily_research`
**Sources:** docs.tavily.com API reference (search / extract / crawl / map / research endpoints, `.md` variants) + `documentation/api-credits`; WebSearch for pricing corroboration. No live probe (docs closed every gap).

**Server instructions:** provider ships NONE. Captured `instructions` is `null` (not empty string) — tavily-mcp v3.3.1 sends no server-level `initialize.instructions` at all. So the gateway's per-endpoint instruction budget is ours to fill from scratch; there's no original to preserve.

**Billing frame (important for the whole file):** every call costs credits (PAYG $0.008/credit; 1,000 free/month). Cost per tool varies by ~2 orders of magnitude — search is 1–2 credits, research is 4–250. This drives the disable/keep math more than capability overlap does.

---

## tavily_search

**What it really does:** one web-search call against Tavily's own retrieval index, returns ranked results with a short relevance-scored `content` snippet per URL (NOT full page text unless `include_raw_content` set). Optionally an LLM-generated `answer` field summarizing across results.

**Key inputs (broadcast subset + doc-only):**
- `query` (req).
- `max_results` — 0–20, default 5.
- `search_depth` — the credit/latency dial:
  - `basic` (1 credit) — balanced; one NLP summary per URL.
  - `advanced` (2 credits) — highest relevance, higher latency; returns 1–3 semantic snippets/source (`chunks_per_source`, ≤500 chars each).
  - `fast` (1 credit) — low latency, multiple snippets/URL.
  - `ultra-fast` (1 credit) — latency above all; one summary/URL.
  - Note: `safe_search` is unavailable on fast/ultra-fast.
- `topic` — **LOCKED to `general` on this MCP surface** (verified 2026-07-03 against tavily-mcp source via deepwiki: schema is `enum: ["general"]`; the handler even forces `general` when `country` is set). `news`/`finance` exist only in the REST API. The gateway hides this param. Docs describe the REST behavior: `news` favors real-time current events; `finance` financial sources — irrelevant here until upstream widens the enum. `news` favors real-time politics/sports/current-events; `finance` financial sources. `country` boost only works when `topic=general`.
- Time filters: `time_range` (day/week/month/year or d/w/m/y) OR `start_date`/`end_date` (YYYY-MM-DD).
- `include_domains` (up to 300, prioritize) / `exclude_domains` (up to 150, block).
- `country` — full country NAME only ("United States", not "us"); general topic only.
- `exact_match` — enforce quoted-phrase matching.
- `include_raw_content` (bool or "markdown"/"text") — attaches parsed page HTML to each result; text may add latency.
- `include_images` / `include_image_descriptions` / `include_favicon`.

**Output shape:** `results[]` each `{title, url, content (snippet), score (float), raw_content?, favicon?, images?}` + top-level `query`, `answer?`, `images?`, `response_time`, `usage` (credits), `request_id`.

**Quirks / gotchas:**
- `auto` search_depth silently bills 2 credits if it picks advanced; set `basic` explicitly to cap cost.
- `country` rejects ISO codes silently-ish — must be full name.
- `content` is a snippet, not the page. For full text either set `include_raw_content` or use `tavily_extract`.

**MISS-SIGNALS:** empty `results[]` (query too niche / over-filtered by domains/dates) · `score` values all low (weak match) · `answer` present but hedgy → thin sourcing. Over-restrictive `include_domains`/date range is the usual cause of zero results.

---

## tavily_extract

**What it really does:** URL(s) → clean parsed content (markdown default, or text). Batch: up to 20 URLs/call. This is the "I have the link, give me the readable body" tool.

**Inputs:** `urls` (req, string or array ≤20) · `extract_depth` `basic`|`advanced` · `format` markdown|text · `query` (rerank chunks by relevance; when set, `raw_content` becomes top chunks joined by `[...]`) · `chunks_per_source` 1–5 (with query) · `include_images` · `include_favicon` · `timeout` (basic 10s, advanced 30s default).

**extract_depth:** `advanced` explicitly for LinkedIn, protected sites, tables/embedded content — "more data, higher success, higher latency." This is Tavily's headline differentiator over naive fetch: it gets through sites a plain HTTP GET can't (JS-heavy, soft-blocked, login-walled-ish).

**Pricing:** basic 1 credit / 5 successful URLs; advanced 2 credits / 5. Failed extractions not billed.

**Output:** `results[]` `{url, raw_content, images?, favicon?}` + `failed_results[]` `{url, error}` + `response_time`, `usage`, `request_id`.

**MISS-SIGNALS:** URL lands in `failed_results` (blocked/404/timeout) — retry with `extract_depth=advanced` before giving up · `raw_content` truncated or mostly nav chrome → try advanced or the `query` rerank · timeout on advanced for very large pages.

---

## tavily_crawl

**What it really does:** breadth/depth-bounded site walk from a root URL, extracting content from every page it visits. = map + extract combined. Returns page bodies, not just URLs. Expensive and slow.

**Inputs:** `url` (req) · `max_depth` 1–5 (default 1) · `max_breadth` 1–500 (default 20, links/page) · `limit` total pages (default 50) · `instructions` (NL: which pages to return; also enables `chunks_per_source` reranking) · `select_paths`/`select_domains` (regex include) · `exclude_paths`/`exclude_domains` · `allow_external` (default TRUE — will wander off-domain unless restricted) · `extract_depth` basic|advanced · `format` · `include_images` · `timeout` 10–150s (default 150).

**Pricing (stacks — this is the expensive one):** map cost + extract cost. Basic: 1 cr/10 pages map + 2 cr/10 extract = ~3 cr/10 pages. Advanced extract: ~5 cr/10 pages. With `limit=50` advanced that's ~25 credits/call, seconds-to-minutes latency.

**Output:** `results[]` `{url, raw_content, favicon?}` (raw_content chunked by `[...]` when instructions given) + `base_url`, `response_time`, `usage`, `request_id`.

**Quirks:** `allow_external=true` default is a footgun — can balloon cost/scope; set `select_domains` to fence it. `instructions` bumps map portion to 2 cr/10.

**MISS-SIGNALS:** hits `limit`/`timeout` before covering the site (raise limit or narrow with select_paths) · returns mostly off-domain junk (allow_external not fenced) · near-empty raw_content across pages → site needs `extract_depth=advanced`.

---

## tavily_map

**What it really does:** same graph traversal as crawl but returns URLs ONLY — no content extraction. Fast, cheap reconnaissance of a site's structure. "Explore hundreds of paths in parallel."

**Inputs:** identical control surface to crawl minus the extract knobs: `url`, `max_depth` 1–5, `max_breadth` 1–500, `limit` (default 50), `instructions`, `select_paths`/`select_domains`, `exclude_*`, `allow_external` (default true), `timeout`.

**Pricing:** 1 cr/10 pages (2 cr/10 with instructions). Cheapest of the crawl-family since no extraction.

**Output:** `base_url`, `results[]` = discovered URLs (strings), `response_time`, `usage`, `request_id`.

**Distinction from crawl:** map = "what pages exist" (URL list); crawl = "what pages exist AND their content." Common pattern: map first to scope, then extract/crawl only the URLs you want (cheaper than a blind crawl).

**MISS-SIGNALS:** short/empty `results[]` (SPA with no crawlable links, or robots-blocked) · flooded with off-domain URLs (allow_external).

---

## tavily_research

**What it really does:** agentic deep-research pipeline — takes a task description, runs many searches + extractions across sources, and returns a SYNTHESIZED cited report. This is the closest MCP analog to Claude Code's own deep-research skill, executed server-side by Tavily.

**Inputs:** `input` (req, describe the task richly) · `model` `mini`|`pro`|`auto` (default auto — broadcast only exposes mini/pro). Doc-only params the MCP tool likely still passes through: `output_length` short/standard/long, `citation_format` numbered/mla/apa/chicago, `include_domains` (≤20 soft) / `exclude_domains` (≤20 hard), `output_schema` (structured JSON out), `files` (≤5 attachments, ≤80K words each), `stream` (SSE).
  - `mini` — targeted, narrow, few subtopics.
  - `pro` — comprehensive, multi-angle, many subtopics.

**Pricing (dynamic, per request — the expensive tool):**
| model | min | max |
|-------|-----|-----|
| mini | 4 credits | 110 credits |
| pro | 15 credits | 250 credits |

At PAYG $0.008/cr, a pro run can hit ~$2/call. Latency: not published, but this is a multi-search+synthesis agent → expect tens of seconds to minutes (blocking unless `stream`).

**Rate limit:** tool description states **20 requests/minute** (docs page didn't restate it, but the server broadcasts it — treat as real). Bursting will 429.

**Output:** synthesized report / detailed answer with citations + `request_id`, `created_at`, `status`, `input`, `model`, `response_time`. Structured JSON if `output_schema` given.

**MISS-SIGNALS:** 429 (hit 20/min) · `status` not success · thin report despite pro (topic too obscure / over-restricted domains) · very long latency with no stream → looks hung but is working. Because a single miss can burn 100+ credits, this tool warrants the most caution.

---

## Overlap notes — distinguishers vs siblings

Cluster with **Exa** (`web_search_exa` = semantic search over Exa's neural index → clean markdown; `web_fetch_exa` = URL → markdown) and **builtin WebSearch/WebFetch**.

**tavily_search vs Exa web_search_exa — GENUINE distinguishers, keep both:**
- Query style: Exa is neural/semantic (describe what you want, embedding match over a curated index); Tavily is keyword-ish web retrieval with an optional LLM `answer`. Different recall profiles.
- Filters are Tavily-ONLY and load-bearing: `time_range`/`start_date`/`end_date`, `include_domains`(300)/`exclude_domains`(150), `country` boost, `exact_match`, `topic=news/finance`. If a query needs date-bounding, exact phrases, per-country bias, or a finance/news agent → Tavily is the only option here.
- Output: both give snippets; Tavily's is a scored summary + optional cross-result `answer`; Exa returns clean markdown highlights. For "give me a synthesized answer inline," Tavily's `answer` beats raw Exa snippets.
- Verdict: NOT duplicates. Route filtered/dated/exact/news queries to Tavily; open-ended semantic discovery to Exa.

**tavily_search vs builtin WebSearch:** Tavily wins on filters, `answer`, domain control, and structured output; builtin WebSearch is free (no credits) and US-only. For a plain "look this up" with no filter needs, builtin is cheaper. Tavily earns its credits on the filter/answer surface.

**tavily_extract vs Exa web_fetch_exa vs builtin WebFetch — THIN overlap, one real distinguisher:**
- All three: URL → clean text/markdown. Base case is duplicative.
- Tavily's ONLY genuine edge: `extract_depth=advanced` for LinkedIn / protected / JS-heavy / table-heavy pages that plain fetchers fail on, plus batch (20 URLs/call) and `query`-rerank. If Exa/builtin fetch fails or a page is protected, Tavily advanced extract is the fallback.
- Verdict: overlapping for easy pages (builtin WebFetch is free → prefer it). Tavily extract justified as the protected-site/batch/table fallback, NOT as the default fetcher. Flag for a possible narrow-scope description rather than blanket disable.

**tavily_crawl / tavily_map — Tavily-ONLY, no sibling here.** Neither Exa nor builtins do multi-page site traversal. Distinguisher vs doing it manually (map → loop extract): the tools parallelize and bound it in one call. Keep — they're unique capabilities. Map before crawl to control cost.

**tavily_research vs Claude Code's own deep-research skill:** genuine tension. Both produce cited multi-source reports. Distinguishers: Tavily research is server-side, one call, blocking, and BILLED (4–250 credits, up to ~$2), rate-limited 20/min; the local deep-research skill is agent-driven (uses these same search/fetch tools as primitives), free of Tavily research credits, inspectable, and steerable mid-flight. Tavily research is worth it when you want a hands-off synthesized answer without spending local agent turns; the local skill wins when you want control, transparency, or to avoid the big per-call credit hit. This is a real keep-or-disable judgment call for the differentiation step — not an obvious duplicate, but the most expensive tool in the backend and the one most redundant with native agent capability.
