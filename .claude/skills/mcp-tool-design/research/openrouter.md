# openrouter — research cache

Researched 2026-07-03; **refreshed 2026-07-12 after an upstream full-rename** (every tool renamed, e.g. models-list→list-models, chat-send→send-message; all overrides migrated to the new originals — the levers.md `override_no_match` recipe). Tools seen 2026-07-12: list-models, get-model, list-model-endpoints, list-benchmarks, list-daily-model-rankings, list-app-rankings, list-task-classifications, get-credits, get-generation, list-providers, search-docs, generate-image, send-feedback, ping (+ send-message, view-skills DISABLED). Sources: live schema dump via local `/openrouter/mcp` tools/list, live probes via `POST /admin/api/run`, upstream = official `mcp.openrouter.ai/mcp` (`OpenRouterMcp` v0.0.2).

## Backend / auth
- Official OpenRouter MCP, streamable-http, **stateless**. Was `auth = "oauth"` — FastMCP stores OAuth tokens in memory, so every daemon kickstart dropped the token and dynamic re-registration then failed upstream (500 "Failed to register client"), leaving the mount alive but tools/list EMPTY (silent-empty, no error).
- Fixed 2026-07-03: upstream accepts plain API-key bearer. Config now `auth_header = "Authorization"` / `auth_value = "Bearer ${OPENROUTER_API_KEY}"` (key in gateway secrets.env, copied from api_keys.env). Verified: direct initialize 200, ping→pong, credits-get returns balance ($280 total / $276.86 used at probe time).

## The `request` wrapper
Most tools take ONE top-level param `request` (object) with typed, individually-described inner fields (draft-07 schema; the client sees them on load). Upstream ships no description on `request` itself. Args must nest: `{"request": {"author": "deepseek", "slug": "deepseek-chat"}}`. Validation errors are excellent — they enumerate allowed enum values (e.g. models-list `sort`: most-popular, newest, top-weekly, pricing-low-to-high, pricing-high-to-low, context-high-to-low, throughput-high-to-low, latency-low-to-high, intelligence-high-to-low, …).

## Per tool (inner request fields from live schema)
- **models-list** — the catalog search: q, sort (enum above), category, min_price/max_price, context, arch, model_authors, providers (case-sensitive display names), input/output_modalities, supported_parameters, zdr/region, distillable. Rich server-side filtering; upstream desc already says prefer filters over full-list fetch.
- **model-get** — one model by {author, slug}; supports :variant suffixes and slug aliases.
- **model-endpoints** — {author, slug} → per-provider serving list: price, latency, throughput, data policy. This is "who serves X and at what price".
- **benchmarks** — {source: artificial-analysis | design-arena, task_type: coding/intelligence/agentic, arena, category, max_results}. Third-party quality scores / elo standings.
- **rankings-daily** — {start_date, end_date} → most-used MODELS by token volume.
- **app-rankings** — {category, subcategory, sort, dates, limit/offset} → top APPS driving traffic.
- **generation-get** — {id} → cost/tokens/provider for one generation. Id comes from chat-send output (chat-send is DISABLED here — mention only if re-enabled).
- **credits-get** — no args; account balance. Account-scoped (real account, no spend to read).
- **providers-list** — no args; all provider names (feeds allow/deny routing prefs and models-list `providers` filter).
- **docs-search** — {query, max_results}; full OpenRouter docs corpus. Only tool with flat, described params upstream.
- **ping** — pure connection health check; zero task value to a cold agent → disabled in tuning (2026-07-03).
- **chat-send / view-skill** — disabled before tuning (chat-send spends money and duplicates nothing we need; view-skill is upstream's self-doc).

## Reads are free
No per-call billing on any enabled tool (credits are the account's LLM spend, not MCP fees).

## Overlap notes
- "Which LLM should I use / what does X cost across providers" → this backend, uniquely. cc-docs explicitly lacks API pricing; context7 is library docs, not live catalogs; web search gets stale pricing.
- OpenRouter API usage ("how do I stream via OpenRouter") → docs-search here, NOT context7 (fresher, canonical) — mirror of the cc-docs pattern.
- Anthropic-direct model IDs/pricing → the claude-api skill / Anthropic docs; OpenRouter prices for Anthropic models are OpenRouter's, not Anthropic API list prices.
- Model quality *benchmarks* live here (artificial-analysis, design-arena); general "best model" web chatter → Exa/Tavily.

## New tools (upstream 2026-07-12; tuned same day, cold-eval 5/5)
- **list-task-classifications** → `list_usage_by_task` — traffic market-share by task type (code gen, web search, …) + top models per type; shares are 0–1 fractions, no absolute volumes. Boundary added vs list_top_models (by model) / list_top_apps (by app).
- **send-feedback** → `report_generation_issue` — feedback on one generation (id from get_generation_cost). Upstream desc referenced raw/disabled names (get-generation, send-message) — rewritten.
- **generate-image** → `generate_image` — text→image inline content block; BILLS the account per call; flat params (model/prompt/size), no request wrapper. Desc points at search_model_catalog (output_modalities=image) for slugs.
- Instructions extended (+117B → 1584B/2048) with a one-line route for both new capabilities.
