# Exa — backend research

**Researched:** 2026-07-03
**Server:** `exa-search-server` v3.2.1 (MCP)
**Tools seen (capture):** `web_search_exa`, `web_fetch_exa` — both ENABLED. No other tools in the capture (the MCP server ships only these two; the full Exa REST API has more — /answer, /research, /findsimilar, websets — but this MCP surface does not expose them).
**Gateway rebroadcast names:** `web_search_exa` → `search_web`, `web_fetch_exa` → `fetch_url`.
**Sources:** exa.ai/docs (reference/search, rate-limits), exa.ai/pricing (via search + live fetch), exa.ai/versus/tavily (Exa's own marketing framing — treated skeptically), captured defaults `~/.local/state/mcp-gateway/defaults/Exa.json`. **2 live probes run through this session** (see "Live observations") — one `search_web` + one `fetch_url`, minimal args.

---

## What Exa really is

A **neural / embeddings search engine over Exa's own crawled index**, not a keyword SERP wrapper. This is the core distinction from every keyword-based search API: Exa embeds both the query and the pages in its index and does semantic nearest-neighbour retrieval. Consequence for how it's driven: queries should be phrased as **a natural-language description of the ideal page** ("blog post comparing React and Vue performance"), not keyword soup ("React vs Vue"). Keyword-style queries actively underperform on the neural path.

Exa maintains specialised sub-indexes it markets heavily: **1B+ LinkedIn people profiles** and a company index, reachable via `category:` prefixes in the query. That's the one genuinely hard-to-replicate asset here.

The MCP server is a **thin, opinionated wrapper** over Exa's `/search` and `/contents` REST endpoints. It hides almost all of the REST knobs (search type, livecrawl/maxAgeHours, summary, structured output, domain/date filters) and picks defaults. So the tool surface is much simpler — and less controllable — than the raw API. Don't promise capabilities in the override text that the MCP tool can't actually reach (e.g. domain filtering, date ranges, structured JSON output are NOT exposed).

---

## Tool: `web_search_exa` (broadcast: `search_web`)

**What it does:** Neural web search over Exa's index; returns top results as clean text/highlights, dated, with metadata. Under the hood hits `/search` with contents included.

**Inputs (MCP surface):**
- `query` (required) — natural-language description of the ideal page. Supports inline `category:` prefixes: `category:people`, `category:company` (docs also list `research paper`, `news`, `personal site`, `financial report`; other strings act as soft hints). `category:people John Doe software engineer` routes to the LinkedIn index.
- `numResults` (optional, default 10) — REST allows 1–100; each result past the first 10 costs extra.

**Outputs:** Clean markdown text per result plus URL/title/published-date metadata. By default returns **query-dependent highlights** (LLM-selected relevant passages), not full page text — Exa claims this cuts tokens 50–75% vs dumping whole pages. If highlights are too thin, the documented workflow is to follow up with `web_fetch_exa` on the best URL(s).

**Quirks:**
- Query *style* is load-bearing — this is not a keyword engine. A user pasting keywords gets worse results than a described-page query. Worth surfacing in the description.
- `category:people`/`category:company` unlock the LinkedIn/company indexes — the standout capability.
- Freshness: the MCP wrapper does not expose `livecrawl`/`maxAgeHours`, so freshness is whatever Exa's default serves (index-cached, may lag for very fresh pages). Deprecated `livecrawl` and its replacement `maxAgeHours` (0 = force fresh crawl, range −1…720h) exist in the REST API but are **not reachable through this tool**.

**Failure modes / MISS-SIGNALS:**
- **Zero results:** `results: []` (with `requestId`, `costDollars`). Empty array = miss. Common cause: keyword-style or over-constrained query on the neural path, or a niche/very-recent topic not yet indexed.
- **Stale/missing very-recent content:** because freshness knobs are hidden, breaking-news-of-the-last-hour queries can return nothing or a cached older version. If freshness matters, that's a miss the tool can't self-correct.
- **Thin highlights:** results present but highlights don't contain the answer → not a hard failure, the signal to chain into `fetch_url`.
- Cost still accrues on a zero-result search (`costDollars` is returned regardless).

---

## Tool: `web_fetch_exa` (broadcast: `fetch_url`)

**What it does:** Fetches one or more known URLs and returns page content as **clean markdown** + metadata. Wraps Exa's `/contents` endpoint.

**Inputs (MCP surface):**
- `urls` (required) — accepts a batch; multiple URLs in one call is the intended, cheaper pattern.
- `maxCharacters` (optional, default **3000**) — REST caps text at 1–10,000 chars. The 3000 default is small: long articles are truncated silently. Raise it when the whole document matters.

**Outputs:** Clean markdown text + page metadata per URL.

**Quirks:**
- Default 3000 chars truncates aggressively — a frequent silent-miss source. The answer may be past char 3000.
- Batching multiple URLs in one call is both the ergonomic and the cost-efficient path (billed per page).
- This is `/contents` extraction, distinct from the LinkedIn *index* — fetching an arbitrary protected/paywalled URL is not guaranteed; Exa returns what it can extract.

**Failure modes / MISS-SIGNALS:**
- **Paywalled / JS-heavy / bot-blocked page:** returns empty, truncated, or boilerplate (cookie-wall / "enable JavaScript") text instead of the article. Signal: content present but semantically empty or clearly not the body.
- **Truncation masquerading as a miss:** default 3000 chars — if the extract cuts off mid-topic, re-fetch with a higher `maxCharacters` before concluding the page lacks the info.
- **Dead/redirecting URL:** extraction fails or returns metadata with no body.

---

## Live observations (probed 2026-07-03 through this gateway session)

Two real calls, as evidence:

- **`search_web(query="category:company Exa AI ...", numResults=1)`** → returned a **single richly-structured company profile**, far beyond a plain search snippet: firmographics (industry, HQ, employee count + YoY growth), full **funding history** (rounds, dates, lead investors, valuation), **tech stack list**, web-traffic stats, LinkedIn follower counts, aliases, contact emails. This confirms `category:company` is a genuine enrichment/firmographic surface, not just "search that finds a company's homepage" — this is the standout, non-duplicable capability in practice. Latency felt in the ~1–2s range (consistent with Exa's speed claims; not precisely timed).
- **`fetch_url(urls=["https://exa.ai/pricing"], maxCharacters=1200)`** → clean markdown with `# Title`, `URL:`, `Author:` header lines, but the **first several hundred chars were nav-menu chrome** (Products / Resources / links) before real page content. Direct evidence of the boilerplate-eats-the-budget quirk: a small `maxCharacters` can be consumed by nav/header before reaching the body. Raise `maxCharacters` and/or expect leading chrome.

## Pricing / cost traits (billed API — matters for disable/steer decisions)

- **Free tier: up to 20,000 requests/month at no cost** (observed live on the pricing page). This materially changes the "prefer the free builtin" calculus — for normal personal usage Exa search is effectively free until that ceiling, so cost is a weak reason to disable it. Beyond the free tier:
- **Search with contents:** ~$7 per 1,000 requests, bundling text+highlights for the **first 10 results free**; +$1 per 1,000 additional results beyond 10.
- **Contents / fetch:** ~$1 per 1,000 pages per content type.
- **Summaries:** +$1 per 1,000 (not exposed via MCP anyway).
- **Deep search modes:** $12 (deep) / $15 (deep-reasoning) per 1,000 — not reachable via this MCP tool.
- **Rate limits:** `/search` 10 QPS, `/contents` 100 QPS default; higher via enterprise.
- **Cost trait:** every call bills, including zero-result searches. `numResults > 10` and high `maxCharacters` both add cost. Cheapest usage = default numResults ≤10, batch fetches, modest maxCharacters. This is real money per call — prefer it over the free builtin WebSearch only when Exa's semantic index or LinkedIn coverage actually earns the spend.

---

## Overlap notes

### vs Tavily (the critical one — `tavily_search` / `extract` / `crawl` / `map` / `research`)

Same top-level intent (search the web, extract a page's content), so `search_web`/`fetch_url` collide head-on with `tavily_search`/`tavily_extract`. Genuine distinguishers below (Exa's /versus page is one-sided marketing — I've kept only defensible differences):

| Dimension | Exa (`search_web`/`fetch_url`) | Tavily (`tavily_*`) | Genuine distinguisher? |
|---|---|---|---|
| Retrieval model | Neural/semantic over **own index** | Keyword/aggregation-oriented, LLM-tuned | **Yes** — different engines; Exa rewards described-page queries, Tavily tolerates keywords |
| Query style | Natural-language "ideal page" | Keyword/question works fine | **Yes** — real behavioral difference |
| People / company | `category:people`/`company` → LinkedIn + company index | No dedicated people/company index | **Yes — Exa-only, the strongest reason to keep Exa** |
| Output form | Query-dependent highlights (token-lean) + clean markdown | Configurable; snippets + optional raw content | Partial — both give clean-ish content; Exa's highlights are more targeted |
| Protected/paywalled extraction | Best-effort via `/contents` | Claims LinkedIn/protected via `extract_depth=advanced` | **Yes — Tavily's advantage** on arbitrary protected URLs |
| Crawl / site map | **Not available** on this MCP surface | `tavily_crawl`, `tavily_map` | **Tavily-only** |
| Research agent | Not exposed (Exa has /research, but not via MCP) | `tavily_research` | **Tavily-only** on this surface |
| Filters (domain/date/time_range) | **Not exposed** through MCP tool | `time_range`, `include/exclude_domains` exposed | **Tavily-only** on this surface |
| Cost | ~$7/1k search (10 results free contents); billed per call | Credit-based ~$0.008/credit, raw content bundled | Roughly comparable; neither clearly cheaper |
| Speed | Exa claims p95 ~1.4–1.7s (self-reported) | Exa claims Tavily ~3.8–4.5s (self-reported) | Unverified — Exa's own numbers, do not treat as fact |

**Bottom line for differentiation:** On generic web search and single-URL extraction, Exa and Tavily **substantially duplicate** — a plain "search the web" or "read this URL" intent is served about equally, and Tavily is the richer toolbox (crawl, map, research, exposed filters, advanced extract). Exa's **non-duplicated** value is: (1) **neural semantic search** for describe-the-page queries where recall of conceptually-similar pages beats keyword match, and (2) **`category:people`/`category:company` → LinkedIn/company index**, which Tavily has no equivalent for. If a disable decision is forced, steer generic search/extract toward one backend and keep Exa specifically for semantic-recall and people/company lookups; keep Tavily for crawl/map/research/filtered extraction. The two are NOT redundant overall, but they ARE redundant on the plain search+fetch intents — say so.

### vs builtin `WebSearch` / `WebFetch` (Claude Code)

- Builtin `WebSearch`/`WebFetch` are free and unmetered; Exa is free up to 20k req/month then bills. So cost is a weak tiebreaker at normal volume — choose on capability, not price.
- Exa earns its slot when: you need **semantic/neural recall** (find pages *about* a concept, not containing keywords), **LinkedIn people/company** data, or **cleaner token-lean markdown extraction** than the builtin fetch. For a plain "what's the URL / summarize this page" task, the builtins are the right default and Exa is wasted spend.
- `WebSearch` is also US-region-biased; Exa's index is global — a minor edge for non-US topics.
