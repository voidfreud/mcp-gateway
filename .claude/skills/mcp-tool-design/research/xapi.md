# research/xapi.md

**Researched:** 2026-07-03
**Endpoint:** `https://api.x.com/mcp` (official X/Twitter API v2 MCP, server `xmcp` v1.0.0, Streamable HTTP). **Currently DISABLED in the gateway.** Auth today = App-only Bearer (`X_API_BEARER_TOKEN`), read-only — see the auth caveat below, it disqualifies two whole families.
**Tools seen (this capture):** 24, in `~/.local/state/mcp-gateway/defaults/xapi.json`. `instructions: null` (server broadcasts none). All tools exposed BARE (per-backend endpoint).
**Sources:** the capture JSON; memory note `~/.claude/memory/notes/x-api-mcp.md` (account + auth context); docs.x.com pricing (`/x-api/getting-started/pricing`), usage/billing (`/x-api/fundamentals/post-cap`), rate limits (`/x-api/fundamentals/rate-limits`), news (`/x-api/news/introduction`, `/search-news`, `/changelog`), trends-by-woeid, bookmarks (`/x-api/posts/bookmarks/*`); Sorsa / Postproxy / TwitterAPI.io 2026 pricing write-ups. **NO live probes** — reads are billed and the prepaid balance may be at/near zero (a call at zero balance fails; a call with balance spends real money).

---

## What xapi is (backend-level)

The official X (Twitter) API v2, wrapped as an MCP server by X. It is **THE** structured, real-time, first-party surface for X data: full-archive post search back to 2006, user/post lookups, engagement rosters (likers/reposters/quoters), Grok-generated News clusters, location Trends, and — with user-context auth — your own timeline/mentions/bookmarks. This is the authoritative, machine-parseable X surface; everything else (web search finding a tweet, his offline archive) is a lossy substitute for *live, structured* X data.

### The cost model is the whole story (as of 2026-02-06, pay-per-use)
X killed subscriptions for new devs in Feb 2026. Alex is on **pay-per-use prepaid credits**: buy credits upfront in the Developer Console, deducted in real time, **requests fail at zero balance until top-up**. No monthly minimum. Hard cap 2M post-reads/mo (~$10k) before Enterprise (~$42k/mo) is forced. Published per-resource rates:

| Billing category | Rate | Charged per | Which tools |
| --- | --- | --- | --- |
| Posts: Read | **$0.005** | resource *returned* | every tool that returns posts (search, lookups, timelines) |
| Users: Read | **$0.010** | resource returned | user lookups, search_users, likers/reposters/quoters (they return User objects) |
| **Owned Reads** (your own data) | **$0.001** | resource returned | get_users_me, own mentions/timeline/posts/bookmarks |
| Counts: Recent | **$0.005** | request (not per-post) | get_posts_counts_recent |
| Trends: Read | **$0.010** | resource | get_trends_by_woeid |
| News: Read | ~$0.005 (**unverified**, likely "Note: Read" class) | resource | search_news, get_news |
| Bookmark (write) | **$0.005** | request | create/delete bookmark, create folder |

**The billing trap that dominates value:** reads are billed **per resource returned, not per call.** A `search_posts_all` that returns 100 posts = 100 × $0.005 = **$0.50 for one call**; at the 500-result max, **$2.50 a call.** A tool that returns User objects bills at the $0.010 rate. **Daily dedup** softens repeat polling: the same post ID returned again *within the same UTC day* is not re-charged (across the whole app). So a monitor polling the same query hourly mostly pays once per new post per day, not once per poll.

**Cost-control levers baked into the tools:** every list-returning tool takes `max_results` — capping it is the primary spend throttle and belongs in the broadcast text. Every tool takes `*.fields`/`expansions`; these change payload richness but billing is per *resource*, not per field, so expansions are ~free to add.

---

## Per tool (grouped by intent family)

### Family 1 — News & Trends (`search_news`, `get_news`, `get_trends_by_woeid`)

**search_news(query, max_results≤100 default 10, max_age_hours 1–720 default 168, news.fields)** → `GET /2/news/search`. Grok-generated **News stories** (not raw posts): each story has `name`, `summary`, `hook`, `contexts` (entities/topics/finance tickers), and `cluster_posts_results` (the post IDs Grok clustered). `max_age_hours` caps recency, **max 720h = 30 days** back, default 168h = 7 days. Query up to 2048 chars. This is a *synthesized* news feed, editorially distinct from searching posts — closer to "what's the story on X about crypto" than "give me tweets matching crypto." Cost per News resource unverified (~$0.005 class); cheap at default 10.
**get_news(id, news.fields)** → `GET /2/news/{id}`. Single News story by ID (IDs come from search_news' results). Lookup companion — only useful after a search hands you an ID.
**get_trends_by_woeid(woeid, max_trends, trend.fields)** → `GET /2/trends/by/woeid/:id`. Trending topics for one location. **WOEID quirk:** a legacy Yahoo "Where On Earth ID" integer, *not* a name — the agent must know/lookup the code. **Worldwide = 1** (the default most agents want), USA = 23424977, UK = 23424975, Japan = 23424856, London = 44418, Tokyo = 1118370, NY = 2459115, LA = 2442047. There is no name→WOEID resolver tool in this set, so those constants should live in the broadcast description or the agent is stuck. Returns `trend_name` + `tweet_count`, up to ~50. Cost **$0.010/resource** (Trends: Read) — a single call for ~50 trends ≈ $0.50; set `max_trends` low.
**Family verdict:** genuinely useful to a solo dev tracking what's breaking on X. search_news is the standout (digested, low-result-count, cheap). Trends is useful but the WOEID friction + $0.010/resource makes it a "keep, but document WOEID=1 and cap max_trends" tool.

### Family 2 — Post search (`search_posts_all`, `get_posts_counts_recent`)

**search_posts_all(query required, max_results≤500, start/end_time, since/until_id, sort_order, next_token, expansions, post.fields, user.fields)** → `GET /2/tweets/search/all`. **Full-archive** search — every public post back to **2006**. Query up to **1024 chars** (X search operators). Rate limit **1 req/sec, 300/15min** (per-app) — cannot burst. **This is the ONLY free-text post search in the tool set** — there is no `search_posts_recent` (7-day) tool exposed, so full-archive is it. **Cost is the headline risk:** billed per post returned at $0.005; `max_results` defaults are small but a maxed 500-result page = $2.50/call, and pagination via `next_token` multiplies that fast. High value (nothing else searches X's archive structurally), high spend — the tool most in need of a `max_results`-capping, cost-warning broadcast.
**get_posts_counts_recent(query required, granularity, start/end_time, since/until_id, next_token, search_count.fields)** → `GET /2/tweets/counts/recent`. Returns **volume counts** (time-bucketed post counts) for a query over the last ~7 days — **not the posts themselves.** Billed **per request ($0.005), not per post** → the cheap way to gauge "how much is X talking about Z" before spending on the actual search. Underrated cost-saver: pair it with search_posts_all (count first, then decide whether to pull posts).
**Family verdict:** keep both — search is the core capability, counts is its cheap reconnaissance partner. Both need cost/`max_results` guidance in the broadcast.

### Family 3 — Post lookup & engagement (`get_posts_by_id`, `get_posts_by_ids`, `get_posts_liking_users`, `get_posts_reposted_by`, `get_posts_quoted_posts`)

**get_posts_by_id(id, expansions, post.fields, user.fields)** → `GET /2/tweets/:id`. One post by ID. **get_posts_by_ids(ids, …)** → `GET /2/tweets` (batch, up to 100 IDs/call). Both bill $0.005/post returned. The batch form is the efficient one — hydrate many IDs (e.g. from a News cluster) in a single call.
**get_posts_liking_users(id, max_results, …)** → likers of a post. **get_posts_reposted_by(id, …)** → reposters. **get_posts_quoted_posts(id, exclude, …)** → quote-posts of a post. The first two return **User objects → billed at $0.010/resource** (the pricier rate); quoted_posts returns posts ($0.005). All three are engagement-roster tools — useful for "who amplified this," niche for a solo dev, and the User-read rate makes liker/reposter enumeration add up.
**Family verdict:** keep the two lookup tools (cheap, foundational — needed to hydrate IDs from search/news). The three engagement-roster tools are lower-value for Alex's use (research + own-presence monitoring) and liker/reposter bill at the higher User rate — reasonable to park unless he wants amplification analysis.

### Family 4 — Users (`get_users_by_username`, `get_users_by_usernames`, `get_users_by_id`, `search_users`)

**get_users_by_username(username, …)** / **get_users_by_usernames(usernames comma-sep, …)** → `GET /2/users/by/username/:username` / `GET /2/users/by`. Resolve @handle(s) → user object(s). **get_users_by_id(id, …)** → `GET /2/users/:id`. All bill **$0.010/user returned** (Users: Read). The batch `by_usernames` form is the efficient one.
**search_users(query required, max_results, next_token, …)** → `GET /2/users/search`. Free-text people search. Bills $0.010/user returned — a broad search returning many users gets pricey.
**Family verdict:** keep the username/id lookups (foundational — you constantly need to turn a handle into an ID, e.g. to feed own-account tools). search_users is more niche; keep if he does people-discovery, else park. Note the $0.010 rate throughout — cap `max_results`.

### Family 5 — Own account (`get_users_me`, `get_users_mentions`, `get_users_timeline`, `get_users_posts`)

**get_users_me(…)** → `GET /2/users/me`. The authenticated user's own profile. **get_users_mentions(id, since/until_id, start/end_time, max_results, …)** → posts mentioning a user. **get_users_timeline(id, exclude, …)** → the reverse-chron home timeline. **get_users_posts(id, exclude, …)** → a user's own posts. For **his own** account these are **Owned Reads at $0.001/resource** — 10× cheaper than public reads. This is the cheapest, highest-value family for "monitor my own X presence": mentions + own-posts polling for a solo dev costs pennies.
**⚠️ AUTH CAVEAT (critical):** `get_users_me` and owned-data access require **user-context OAuth**, not the App-only Bearer the gateway is configured with today. `get_users_mentions/timeline/posts` take an explicit `id` so they *may* work app-only for any public user at the $0.005 public rate, but `get_users_me` and the $0.001 owned-rate need OAuth. **As currently wired (Bearer), the "own account" value proposition is not fully available** — flag this to Alex; realizing it means the `xurl`/OAuth route from the memory note.

### Family 6 — Bookmarks (`get_users_bookmarks`, `create_users_bookmark`, `get_users_bookmark_folders`, `create_users_bookmark_folder`, `get_users_bookmarks_by_folder_id`, `delete_users_bookmark`)

Six tools: read bookmarks / read folders / read a folder's contents (owned reads, $0.001) + **three WRITE ops** — `create_users_bookmark` (POST, adds a post to bookmarks, optional `folder_id`), `create_users_bookmark_folder` (POST, name 1–25 chars), `delete_users_bookmark` (DELETE). Writes bill **$0.005/request** (Bookmark action).
**⚠️ AUTH CAVEAT:** ALL bookmark ops require **user-context OAuth with `bookmark.write`/`bookmark.read` scopes** — **none work with the App-only Bearer.** So today these six are **entirely non-functional** in the gateway's current auth mode.
**Family verdict:** **park the whole family, and definitely the write ops.** They can't run under the current Bearer auth; they're write-side agent actions with real side effects on his account (creating folders, mutating his bookmarks) — exactly the kind of broadcast-enabled tool an agent shouldn't reach for autonomously. Bookmark-folder CRUD in particular is pure organizational plumbing with near-zero agent-research value.

---

## Failure modes & MISS-SIGNALS

- **Zero prepaid balance:** requests **fail** (not silently empty) until top-up. An agent will see an auth/quota error, not results — the miss-signal here is a hard failure, and re-trying just keeps failing. Worth a broadcast note so a cold agent doesn't loop.
- **App-only Bearer vs user-context:** with the current Bearer, `get_users_me` and all six bookmark tools will error (need OAuth user token); owned-rate ($0.001) pricing won't apply. Not a bug — an auth-scope mismatch. Verify auth mode before trusting the "own account" families.
- **WOEID as opaque integer:** `get_trends_by_woeid` with a wrong/unknown WOEID returns nothing useful; there's no name resolver in the set. Worldwide=1 is the safe default.
- **Per-resource billing surprise:** an agent that sets `max_results=500` on `search_posts_all` spends $2.50 in one call, $0.005/User on people search — the "quirk" is fiscal, not technical. The tools succeed; the balance drains. Broadcast text must carry the cap guidance.
- **Full-archive rate ceiling:** `search_posts_all` is hard-limited to 1 req/sec — an agent paginating aggressively hits 429s, not errors it can burst past.

---

## Overlap vs siblings (distinguishers → differentiation.md)

- **vs the general web tools (Exa `search_web`, Tavily `search`)** — the sharp line: **web search can *find* a tweet's text via the open web, but xapi is THE structured, real-time, first-party X surface.** Use Exa/Tavily to read *about* X or catch a viral tweet indexed on the web (free-ish, unstructured, laggy, incomplete). Use xapi when you need *structured* post objects, engagement metrics, full-archive coverage back to 2006, live trends, or anything tied to a specific account — none of which the open web gives reliably. xapi costs real prepaid money per resource; the web tools don't. Rule: *discovering/reading* X content casually → web tools (free); *structured/complete/real-time/account-scoped* X data → xapi (paid).
- **vs his offline X archive (`~/.claude/memory/notes/x-archive-contents.md`)** — the archive is **his own historical export: free, local, but frozen** (only his data, only up to export date). xapi is **live, everyone's data, but billed.** For "what did I tweet in 2022" → the free archive. For "what's happening on X right now" or "who's talking about Z" → xapi. Never spend an xapi read on something the offline archive already answers.
- **within xapi — search_news vs search_posts_all:** search_news = Grok-*digested* story clusters (summaries, low result count, cheap); search_posts_all = *raw* posts, full archive, per-post billing that scales with volume. "What's the story" → news; "give me the actual posts / historical corpus" → search.
- **within xapi — get_posts_counts_recent vs search_posts_all:** counts = cheap ($0.005/request) volume reconnaissance, no posts; search = the posts themselves at $0.005 *each*. Count first to size the spend, then pull.

**One-line intuitions for the differentiator:**
- xapi = "structured, real-time, first-party X data — billed per resource returned from a prepaid balance."
- Not for: casually reading a tweet the open web already indexes (→ Exa/Tavily, free), or his own historical tweets (→ offline archive, free).
- Watch: per-*resource* billing (cap `max_results`), zero-balance hard-fail, and App-only-Bearer breaking `get_users_me` + all bookmarks.

---

## SHORTLIST PROPOSAL (for Alex's keep/disable decision)

Context: solo dev doing **research + monitoring his own X presence**, prepaid balance, App-only Bearer today. Recommendation — **keep a lean read-only research core; park write ops, bookmark plumbing, and (until OAuth) the owned-account tools; leave engagement rosters off unless he wants amplification analysis.**

**KEEP broadcast-enabled (research core):**
- `search_news`, `get_news` — digested breaking-news, cheap, low result count. *High value / low cost.*
- `get_trends_by_woeid` — live trends; document WOEID=1 default + cap `max_trends`. *Medium value / $0.010 per trend.*
- `search_posts_all` — the only X post search; the workhorse. **Must** carry a `max_results`-cap + cost warning. *Highest value / highest cost.*
- `get_posts_counts_recent` — cheap ($0.005/request) way to size a search before paying for posts. *Medium value / cheap — the cost-saver.*
- `get_posts_by_id`, `get_posts_by_ids` — hydrate post IDs (from news clusters/search). Foundational. *Support tools / $0.005 per post.*
- `get_users_by_username`, `get_users_by_usernames`, `get_users_by_id` — resolve handles↔IDs, needed to feed everything else. *Support tools / $0.010 per user.*

**PARK (disable) for now:**
- **All 6 bookmark tools** (`get_users_bookmarks`, `create_users_bookmark`, `get_users_bookmark_folders`, `create_users_bookmark_folder`, `get_users_bookmarks_by_folder_id`, `delete_users_bookmark`) — **non-functional under App-only Bearer**, and the writes mutate his account. Bookmark-folder CRUD is pure plumbing. *No agent-research value; blocked by auth anyway.*
- `get_users_me`, `get_users_mentions`, `get_users_timeline`, `get_users_posts` — the **cheapest, highest-value family for own-presence monitoring ($0.001 owned reads)**, BUT `get_users_me` + owned-rate need **OAuth user-context** the gateway isn't using. **Decision fork for Alex:** if he wants own-presence monitoring (worth it — pennies), switch this backend to the `xurl`/OAuth route and *then* enable this family. Until then, park.
- `get_posts_liking_users`, `get_posts_reposted_by`, `get_posts_quoted_posts` — engagement rosters; liker/reposter bill at the higher $0.010 User rate; niche for research. *Enable only if he wants amplification analysis.*
- `search_users` — people-discovery; $0.010/user, broad results get pricey. *Enable only if he does people search.*

**Per-family cost/value one-liners:**
- News & Trends — *useful for "what's breaking"; news cheap, trends $0.010/resource + WOEID friction.* **Keep.**
- Post search — *the core capability; per-post billing makes it the #1 cost risk — cap it.* **Keep with guardrails.**
- Post lookup — *cheap, foundational ID-hydration.* **Keep the two lookups; park the 3 rosters.**
- Users — *handle↔ID resolution, foundational; $0.010/user.* **Keep lookups; park search_users.**
- Own account — *pennies ($0.001) and highest personal value, but needs OAuth to work.* **Park until auth switched, then enable.**
- Bookmarks — *broken under current auth + write side-effects + plumbing.* **Park all six.**
