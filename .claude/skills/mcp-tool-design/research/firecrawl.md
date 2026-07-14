# Firecrawl — research cache

**Researched:** 2026-07-03
**Server:** `firecrawl-fastmcp` v3.22.3 (hosted, `https://mcp.firecrawl.dev/.../v2/mcp`) — **enabled in the gateway; 18 tools broadcast** (scrape/search/interact + monitor_* CRUD + research_* corpus; the rest of the 26 captured tools stay disabled — map, feedback, crawl, check_crawl_status, extract, agent, agent_status, parse).
**Tools seen (26):** `scrape`, `map`, `search`, `search_feedback`, `feedback`, `crawl`, `check_crawl_status`, `extract`, `agent`, `agent_status`, `interact`, `interact_stop`, `parse`, `monitor_create`, `monitor_list`, `monitor_get`, `monitor_update`, `monitor_delete`, `monitor_run`, `monitor_checks`, `monitor_check`, `research_search_papers`, `research_inspect_paper`, `research_related_papers`, `research_read_paper`, `research_search_github`.
**Sources:** docs.firecrawl.dev (pricing/billing, scrape, crawl, extract, agent, monitoring, research), firecrawl.dev/pricing, plus four independent third-party comparisons/benchmarks: apigene.ai (800-pt 8-category benchmark, 2026-03), riccardogiorato/web-scrapers-evals (speed + success table, Nov 2025), pondero.ai (pricing/architecture, 2026-06), sagentum.com (MCP-quality assessment). **No live probe — every Firecrawl call bills credits.**

**2026-07-15 param-doc pass:** every visible param on all 18 broadcast tools now carries a written description (previously most were "(no description)" — the broadcast schema types/enums/defaults, confirmed live via the gateway's own tool schemas). Also de-shouted six monitor_* CRUD tools (`list/get/update/delete/run/checks`) that were still carrying raw upstream `**Usage Example:** \`\`\`json` blocks.

**Server instructions (provider-shipped, present):** tells Claude to prefer `firecrawl_search` over built-in web search, then call `firecrawl_search_feedback` to refund 1 credit. This is self-promoting default text we'd overwrite or drop; it exists (unlike Tavily's `null`).

---

## Billing frame — the whole decision turns on this

Firecrawl bills **credits**, like Tavily, but the per-credit economics are ~10x cheaper and the modifiers are where cost hides:

| Operation | Base cost | Notes |
|-----------|-----------|-------|
| Scrape | 1 cr/page | the cheap default |
| + JSON format (LLM extraction) | **+4 cr/page** | schema/`jsonOptions` extraction |
| + Enhanced mode (hard-to-access) | **+4 cr/page** | the JS/anti-bot proxy tier |
| + PII redaction / `question` / `highlights` | +4 cr/page each | LLM-backed formats |
| + PDF parsing | +1 cr/PDF page | |
| + Zero-data-retention | +1 cr/page | |
| Lockdown mode (cache-only) | 5 cr | errors on cache miss |
| Map | 1 cr/call | flat |
| Search | 2 cr/10 results | + scrape costs per result if `scrapeOptions` set |
| Crawl | 1 cr/page | **pre-flight reserves the full `limit`** — default limit 10,000 → 402 unless you pass a real limit |
| Interact | **2 cr/browser MINUTE** | time-metered, not per-call |
| Agent | dynamic | 5 free runs/day, then ~100–500 cr per complex run |
| Extract | token-based (1 cr = 15 tokens) | deprecated → agent |
| Monitor | 1 cr/page/check + judging | recurring — cost accrues on a schedule |

Modifiers **stack**: a JSON + enhanced scrape = 1+4+4 = 9 cr/page. At list PAYG (~$0.00083/cr on Standard — pondero: Firecrawl ~$83 per 100k page fetches vs Tavily ~$800 per 100k searches), a plain scrape is roughly an order of magnitude cheaper per page than a Tavily extract. **The expensive/risky ones are: agent (100s of credits), the crawl pre-flight 10k-credit reservation footgun, and monitors (recurring, unattended spend).**

---

## The verdict up front

Firecrawl's five headline tools (scrape/map/search/crawl/extract) collide almost entirely with the just-tuned Exa+Tavily field — they are a **second, differently-priced copy of the same web-fetch/search/crawl surface**, not new capability. Three clusters are genuinely NEW to our field and are the only reasons to keep any of firecrawl on: **interact** (live browser actions — click/fill/execute-JS), **monitor_*** (scheduled change-watching with diff + goal-judge), and **research_*** (a dedicated 3M-paper arXiv + research-GitHub corpus). Everything else is duplicate, a poller/feedback satellite of a duplicate, or deprecated.

Independent benchmarks back the "duplicate, and not the winner" read on the search/fetch core:
- **web-scrapers-evals (Nov 2025, 25 real sites):** avg latency Exa **0.3s** / Tavily 0.8s / Firecrawl **1.8s** (Tesla store a 23.5s outlier); success 20/25 Exa, 20/25 Firecrawl, 15/25 Tavily. Firecrawl ties Exa on *coverage* but is ~6x slower; all three fail on Instagram/X profiles.
- **apigene 800-pt benchmark:** Exa 648 / Tavily 637 / Firecrawl lowest — but explicitly *only `firecrawl_search` (metadata-only) was tested*; its scrape/extract/agent weren't. Firecrawl's measured strengths were source diversity and raw speed of the search call, not result quality.
- **sagentum MCP-quality:** Exa 80.6 / Tavily 69.4 / Firecrawl 50.0 — the Firecrawl 50 is a *coverage gap* (free tier blocked live testing), not confirmed failure. Their qualitative note: Firecrawl's real edge is "full page extraction and site crawling not available from Exa or Tavily" — i.e. the crawl/extract surface, which we already have via Tavily.

---

## Core web surface — DUPLICATE cluster

### firecrawl_scrape — DUPLICATE (of Exa `fetch_url` + Tavily `extract_protected_pages`)
URL → clean markdown / structured JSON. The most feature-loaded tool in the backend: `formats` include markdown, json (schema extraction), branding (brand-identity colors/fonts — a genuine novelty but niche), question, highlights; `maxAge` cache for "500% faster"; `lockdown` cache-only mode; `waitFor` for SPAs; safe-mode disables interactive actions. **Overlap:** basic URL→markdown is exactly `fetch_url` (free-ish, 0.3s) and `extract_protected_pages` (Tavily's advanced tier for JS/protected). Firecrawl's distinguishers are (a) one-call JSON-schema extraction and (b) cheaper per-page at scale — but Exa fetch wins on latency for easy pages and Tavily advanced already covers hostile DOMs. **Winner per intent:** easy page → Exa fetch_url; protected/JS → Tavily extract (already tuned); schema-extraction-in-one-shot is the only thing neither sibling does inline, and `firecrawl_extract`/`agent` also cover that. Duplicate.

### firecrawl_map — DUPLICATE (of Tavily `map_site_urls`)
Site → list of indexed URLs, with an optional `search` param to locate a page semantically. Functionally identical to Tavily map (which has `instructions` for the same job). No content extraction. Tavily's sibling is already tuned and in the field. Duplicate; `search` is a mild convenience, not a distinguisher.

### firecrawl_search — DUPLICATE (of Exa `search_web` + Tavily `search_web_filtered`)
Web search returning metadata + optional inline scrape. Carries Google-style operators (`site:`, `intitle:`, `inurl:`, `related:`, `imagesize:`), `categories` (github/research/pdf), domain include/exclude, and `sources` (web/news/images). That operator/source surface partly overlaps Tavily's structured filters and Exa's semantic recall. Benchmarks put its *result quality* below both siblings (metadata-only body; lowest apigene score). The one thing it has that neither sibling exposes cleanly is **images as a search source** and Google operators — minor. Duplicate; Tavily (filters/news) or Exa (semantic) win.

### firecrawl_crawl — DUPLICATE (of Tavily `crawl_site`)
Blocking multi-page site walk that returns page bodies. Same job as `crawl_site`. Extra footgun: the **pre-flight credit check reserves the full `limit`** (default 10,000) → a bare call 402s unless you cap `limit`. Tavily's crawl is already tuned and doesn't have the 10k-reservation trap. Duplicate.

### firecrawl_extract — DUPLICATE / DEPRECATED
LLM structured extraction across URLs or wildcard domains. **Firecrawl's own docs mark it superseded by `/agent`** ("Use /agent instead"). Overlaps `firecrawl_scrape` JSON mode and Tavily extract. Token-billed. Duplicate and deprecated — no reason to surface it.

### firecrawl_agent — UNCERTAIN (overlaps Tavily `research_report`)
Async autonomous research agent: describe a goal (+ optional URLs/schema), get a job ID, poll `agent_status` 1–5 min. Returns structured data, not a cited prose report. **Overlap:** Tavily `research_report` is also a hosted, blocking-ish research agent — same "hands-off, spend-credits-not-turns" intent. Distinguishers vs research_report: agent yields schema-structured JSON and is built to browse JS-heavy SPAs; research_report yields a cited narrative. Both also compete with Claude Code's own deep-research skill. Cost is high and unpredictable (~100–500 cr). Genuine tension, not an obvious duplicate — **UNCERTAIN**; if kept, it's the "structured autonomous extraction" niche vs research_report's "cited report" niche.

---

## Genuinely DISTINCT clusters (the only keep candidates)

### firecrawl_interact — DISTINCT (no sibling in the web field)
Live browser session: click buttons, fill forms, run `agent-browser` commands or arbitrary JS (`code` + `language` node/python/bash), extract post-interaction content. Target a fresh `url` or reuse a `scrapeId`. Billed **2 cr/browser minute**. **Nothing in Exa/Tavily does interactive browser automation** — this is the one capability the whole field otherwise lacks. Caveat for differentiation: the *real* competitor isn't a web tool, it's the local `cmux-browser` skill (drives a real webview, no per-minute billing). So DISTINCT within the backend field, but its keep/park hinges on "hosted browser-in-an-MCP-call vs local browser skill."

### firecrawl_interact_stop — SATELLITE of interact
Tears down an interact session to free resources. No standalone value; lives or dies with `interact`.

### monitor_* (create/list/get/update/delete/run/checks/check — 8 tools) — DISTINCT (no sibling anywhere in the field)
Scheduled recurring scrape/crawl/search that diffs each result against the last snapshot and notifies via webhook/email, with an LLM **goal-judge** that suppresses noise ("only alert on meaningful changes," claimed up-to-90%-fewer-tokens). Modes: markdown diff (default) or JSON per-field change-tracking (`plans[0].price` style diffs). Min cadence 5 min. Cost: 1 cr/page/check + judging, **recurring/unattended**. **Genuinely unique** — neither Exa, Tavily, nor the builtins do scheduled change-watching. The only conceptual overlap is DIY (Claude Code's own `schedule`/cron + tavily/exa), which lacks the hosted diff+judge. `monitor_create`/`monitor_check` carry the real capability; the other six are thin CRUD/list/status satellites around them. If we keep the cluster it's for change-watching; if we don't want unattended recurring spend, park the whole cluster.

### research_* (search_papers / inspect_paper / related_papers / read_paper / search_github — 5 tools) — DISTINCT (dedicated academic corpus)
Firecrawl **Research Index**: 3M+ arXiv papers + research-repo GitHub artifacts (issues/PRs/READMEs), refreshed daily. `search_papers` = HyDE semantic search over abstracts; `inspect_paper` = canonical metadata; `related_papers` = citation-graph expansion (similar/citers/references) ranked to an intent; `read_paper` = full-text passage retrieval to verify a claim; `search_github` = research-repo issue/PR/README search. Firecrawl claims SOTA on arXivQA (0.750 MRR, +18% recall over next provider) benchmarked with Opus 4.8. **Distinguisher:** Exa has *some* academic reach via semantic search, but no dedicated citation-graph paper index with full-text passage verification; deepwiki covers repo internals, not paper corpora. This is the strongest genuinely-additive cluster — it does something the field cannot. Verdict DISTINCT. (`search_github` is the softest of the five — it overlaps Exa/deepwiki for code, but scoped to *research* repos it's still differentiated.)

### firecrawl_parse — UNCERTAIN / DISTINCT-but-low-value
Local document parsing (PDF/docx/doc/xlsx/xls/html/rtf/odt → markdown or JSON). In hosted mode it's a clunky **two-call upload flow** (mint upload URL → run a local curl PUT → call again with `uploadRef`) because the hosted server can't read your filesystem. **No web-tool sibling** parses local files — so DISTINCT in that sense. But Claude Code already reads PDFs natively (the Read tool), which removes most of the reason to round-trip a local file through a billed hosted endpoint. DISTINCT capability, low marginal value.

---

## Feedback/telemetry satellites — not capabilities

### firecrawl_search_feedback / firecrawl_feedback — SATELLITE (park with their parent endpoints)
Post-hoc quality feedback that refunds 1 credit (search) or logs endpoint-level signals (generic). Pure telemetry for Firecrawl's own quality loop; substantive-feedback validation, 2-min windows, daily refund caps. They exist to recover the credit their parent tool spent. If `search`/`scrape`/`crawl`/`map` are disabled, these are dead weight. Never surface independently.

### check_crawl_status / agent_status — SATELLITE (pollers)
Status pollers for the async `crawl` and `agent` jobs. Live or die with their parent. `crawl` self-polls to terminal state, so `check_crawl_status` is only for re-checking an old ID; `agent` genuinely needs `agent_status` (async by design).

---

## Per-tool verdict table

| Tool | Verdict | One-line reason |
|------|---------|-----------------|
| firecrawl_scrape | DUPLICATE | URL→markdown = Exa fetch_url (faster) / Tavily extract (protected); schema-extract is the only inline edge |
| firecrawl_map | DUPLICATE | site→URL list = Tavily map_site_urls (already tuned) |
| firecrawl_search | DUPLICATE | web search below Exa/Tavily on quality; only images-as-source + Google operators are unique-ish |
| firecrawl_search_feedback | SATELLITE | credit-refund telemetry for search; dead if search off |
| firecrawl_feedback | SATELLITE | generic endpoint telemetry; dead if parents off |
| firecrawl_crawl | DUPLICATE | multi-page harvest = Tavily crawl_site; adds a 10k-credit pre-flight footgun |
| firecrawl_check_crawl_status | SATELLITE | poller for crawl job |
| firecrawl_extract | DUPLICATE | LLM extraction; Firecrawl's own docs deprecate it → agent |
| firecrawl_agent | UNCERTAIN | hosted autonomous research vs Tavily research_report; structured-JSON + SPA-browsing niche, 100s of credits |
| firecrawl_agent_status | SATELLITE | poller for agent job |
| firecrawl_interact | DISTINCT | live browser actions (click/fill/JS) — no web-tool sibling; real rival is local cmux-browser skill |
| firecrawl_interact_stop | SATELLITE | session teardown for interact |
| firecrawl_parse | UNCERTAIN | local doc parsing — no sibling, but Claude Code reads PDFs natively; clunky hosted upload flow |
| firecrawl_monitor_create | DISTINCT | scheduled change-watch + diff + goal-judge — nothing in the field does this |
| firecrawl_monitor_check | DISTINCT | per-page/per-field diff results — core payload of the monitor cluster |
| firecrawl_monitor_list/get/update/delete/run/checks | SATELLITE | CRUD/list/status around the monitor capability |
| firecrawl_research_search_papers | DISTINCT | HyDE search over 3M-paper arXiv corpus — no field sibling |
| firecrawl_research_inspect_paper | DISTINCT | canonical paper metadata — part of the research corpus |
| firecrawl_research_related_papers | DISTINCT | citation-graph expansion (similar/citers/refs) — unique |
| firecrawl_research_read_paper | DISTINCT | full-text passage verification — unique |
| firecrawl_research_search_github | DISTINCT | research-repo issue/PR/README search; softest (overlaps Exa/deepwiki for code) |

**If most of firecrawl duplicates — it does.** The web-fetch/search/crawl/extract core (scrape, map, search, crawl, extract + their feedback/status satellites — ~12 tools) is a second copy of the Exa+Tavily field and is not the benchmark winner on quality or latency. The keep-worthy remainder is three capability islands the field genuinely lacks: **interact** (browser actions), **monitor_*** (scheduled change-watching), **research_*** (academic corpus) — plus **parse** as a low-value maybe and **agent** as an UNCERTAIN overlap with research_report. That's the keep/park decision surface for Alex.
