# research/github.md

**Researched:** 2026-07-03
**Endpoint:** `https://api.githubcopilot.com/mcp` — GitHub's official *remote/hosted* MCP server (the `github` backend), currently **disabled** in the gateway config. Streamable HTTP. Server `github-mcp-server` version `remote-561d59400ead4164cb492689118a05a7a171aeb5`. Auth = Alex's GitHub token (PAT via `Authorization: Bearer`, since Claude Code is a no-OAuth host per GitHub's support matrix) → the tool surface and every write capability are bounded by that token's scopes.
**Tools seen (this tier):** 44 tools — the **default toolset** (see grouping below). NOT `/x/all`: the capture contains none of the `actions`, `gists`, `discussions`, `notifications`, `dependabot`, or code-scanning tools, so those toolsets are off on this endpoint. Full names + shipped descriptions in `~/.local/state/mcp-gateway/defaults/github.json`.
**Sources:** capture `github.json`; github.com/github/github-mcp-server README + `docs/remote-server.md` + `docs/server-configuration.md`; docs.github.com Copilot MCP pages (set-up, toolsets, About MCP); github.blog practical-guide (2025-07-30). **No live probe** — the backend is disabled in the gateway, so nothing here is verified against a live call; all behavior below is from docs + the capture. When it's enabled, re-verify output verbosity and the miss-signals.
**Naming rule:** official tool names are FROZEN (Alex's rule — habits/docs/muscle-memory reference `create_pull_request`, `get_me`, etc.). Tune descriptions, params, grouping, disable set — never the `original`→broadcast name.

---

## What the github backend is (backend-level)

GitHub's own hosted MCP server, the same code as the `ghcr.io/github/github-mcp-server` Docker image but run inside GitHub's infra and auto-updated. It exposes GitHub's *platform API* (REST + some GraphQL) as tools: repo files, issues, PRs + reviews, releases/tags, search, org/team/collaborator metadata, secret scanning. It is **remote-repo collaboration + platform metadata over the wire** — not local code understanding, not git plumbing on a checkout.

**Toolsets (server's own grouping).** The server ships tools in named *toolsets*, each selectable by URL path (`/x/{toolset}`, `/x/all`) or `X-MCP-Toolsets` header. This endpoint runs the **default** set. Full toolset menu (only the starred ones are live here):
- ★ `context` (get_me), `repos`, `issues`, `pull_requests`, `labels`, `orgs`, `users`, `search`-family, and secret-scanning — the 44 default tools.
- ✗ (not on this endpoint) `actions` (CI/CD, workflow runs), `gists`, `discussions`, `notifications`, `dependabot`, `code_security`/`code_quality` scanning-alerts, `git` (low-level Git API). Enabling any of these = a config change on the endpoint URL, not something the gateway override layer can do.

**Read-only mode is a real lever.** `/readonly` URL suffix or `X-MCP-Readonly: true` header makes the server a *strict* filter that drops every non-read tool even if requested — it "takes precedence over any other configuration." That's the clean way to get a safe, browse-only GitHub for a solo dev who mostly wants `gh` for writes. (Also `X-MCP-Exclude-Tools` header to drop named tools server-side — but the gateway's own `disabled` flag already does per-tool suppression, so prefer the gateway lever.)

---

## Grouping — the 44 tools by workflow cluster

**A. Identity / context (1)** — `get_me`. The server's instructions say to call it *first* to learn the user's permissions before other calls. Zero params.

**B. Repo files & contents (6)** — `get_file_contents` (file OR directory; `ref`/`sha`), `create_or_update_file` (⚠ needs the blob `sha` to update — tells the agent to run `git rev-parse <branch>:<path>`), `delete_file` (destructiveHint), `push_files` (multiple files, one commit), `create_repository`, `fork_repository`. Write tools here are the remote-file-edit path.

**C. Branches / commits / tags / releases (9)** — `create_branch`, `list_branches`, `list_commits`, `get_commit` (has a `detail` knob: `none`/`stats`/`full_patch` — `full_patch` "can be very large"), `list_tags`, `get_tag`, `list_releases`, `get_latest_release`, `get_release_by_tag`. All read except create_branch. (No release *creation* tool in the default set.)

**D. Issues (8)** — `issue_read` (method-multiplexed: get / get_comments / get_sub_issues / get_parent / get_labels), `issue_write` (method: create | update — also handles custom issue-fields, state_reason, types), `list_issues` (cursor-paginated via `after`/`endCursor`), `add_issue_comment` (also does reactions; works on PRs too via issue_number), `sub_issue_write` (add/remove/reprioritize sub-issues), `list_issue_types`, `list_issue_fields`. Plus `get_label`/labels below. Note issue_write covers PRs too ("Create or update issue/pull request").

**E. Pull requests (9)** — `create_pull_request`, `update_pull_request`, `update_pull_request_branch` (sync with base), `list_pull_requests`, `pull_request_read` (method-multiplexed, 9 methods: get / get_diff / get_status / get_files / get_commits / get_review_comments / get_reviews / get_comments / get_check_runs — this one tool is the whole PR-inspection surface incl. CI check runs), `merge_pull_request`, `add_reply_to_pull_request_comment`. Review sub-cluster: `pull_request_review_write` (create/submit_pending/delete_pending/resolve_thread/unresolve_thread), `add_comment_to_pending_review`, `request_copilot_review`. The reviews workflow is stateful: create pending review → add_comment_to_pending_review (repeat) → pull_request_review_write submit_pending. The server instructions spell this ordering out.

**F. Labels (1 here)** — `get_label` (list is in the `labels` toolset which is partly folded in; issue labels are set via issue_write).

**G. Search (6)** — `search_code`, `search_commits`, `search_issues`, `search_pull_requests`, `search_repositories`, `search_users`. All read. Rich GitHub search-qualifier syntax lives in the `query` param descriptions (esp. search_code and search_commits — worth NOT trimming those, they teach the qualifier grammar). Server instructions warn: put `sort:`/`order:` in the dedicated params, never in the query string.

**H. Org / team / collaborator metadata (4)** — `list_repository_collaborators`, `get_team_members`, `get_teams`. (get_me sits in A.)

**I. Security (1 here)** — `run_secret_scanning` (scan raw file contents/diffs for secrets; readOnlyHint, openWorldHint false).

---

## Per-tool quirks worth caching

- **Method-multiplexed tools** (`issue_read`, `issue_write`, `pull_request_read`, `pull_request_review_write`, `sub_issue_write`): one tool name hides many operations behind a `method` enum. Keeps the tool count down but the `method` param description is load-bearing — trimming it blinds the agent to what the tool can do. Grade these with the method doc intact.
- **`create_or_update_file`**: updating REQUIRES the current blob `sha`; the description instructs `git rev-parse <branch>:<path>` to get it. Miss-signal: a 422 if the sha is stale/missing.
- **`get_commit` / `pull_request_read get_diff` / `get_file_contents`**: the token-heavy tools. `get_commit detail=full_patch` and PR diffs can be huge. GitHub's own instructions push `minimal_output=true` (on `search_repositories` it *defaults* true) and pagination in **batches of 5–10**. Verbosity is the known pain point of this backend — favor the `detail`/`minimal_output`/`perPage` knobs.
- **Pagination is inconsistent**: most list/search tools use `page`+`perPage` (max 100), but `list_issues` uses **cursor** pagination (`after` + `endCursor` from `pageInfo`), and `pull_request_read get_review_comments` also uses cursor `after`. Two paradigms in one backend.
- **`create_repository private`**: defaults to **true** (private) when omitted — safe default, good.
- **`create_pull_request`**: this is the standout MCP-over-gh win (see below).
- **`request_copilot_review`**: fires GitHub's Copilot bot reviewer — a write action, and one Alex's own git-plugin flows already cover via `gh`.

---

## Auth / scopes / rate limits / miss-signals (docs, unverified live)

- **Which writes are live = whatever Alex's token grants.** The server applies "scope filtering: always enabled" — it only surfaces/permits tools the token's scopes allow. A fine-grained PAT without `contents:write` can't push; without `pull_requests:write` can't open PRs. So the *effective* write surface depends on the token behind the `Authorization` header, not on the tool list. When enabling, check the token's scopes to know the real boundary. Read-only mode (`/readonly`) is the belt-and-suspenders option.
- **Rate limits** = GitHub REST/GraphQL limits under Alex's token (5,000 req/hr authenticated core; search has its own tighter per-minute cap). No MCP-specific limit documented. Not probed.
- **Miss-signals to expect** (per patterns, verify live): 401 (bad/expired token — the blog's #1 gotcha is a leftover `GITHUB_TOKEN` env var), 403/404 for private resources the token can't see (GitHub returns 404 to hide existence), 422 on stale file sha or validation, secondary-rate-limit 403 on bursts. Large-toolset "model times out" is a documented symptom → keep the surface lean.
- **Output verbosity is THE reputation problem** — GitHub MCP responses are notoriously token-heavy. The levers are all present (minimal_output, detail=none/stats, perPage, method-scoped reads); a tuning pass should make sure the param descriptions keep steering toward them.

---

## Overlap vs siblings (distinguishers → differentiation.md)

- **vs `gh` CLI (the main sibling — Bash + gh, already authenticated in the gateway's shell).** This is the sharpest boundary and the one that decides most tools' fate. `gh` covers essentially all of this backend's read surface and most writes, faster and with zero MCP token overhead, and Alex has a whole git-plugin skill suite built on `gh`. So the MCP tool must earn its place; it wins only when:
  1. **`create_pull_request`** — the standout. Opening a PR via the MCP tool avoids a known false-negative in a local pre-push hook (structured API call, not a shell `gh pr create` the hook trips on). This is the one write tool with a concrete reason to stay enabled.
  2. **Structured JSON outputs** the model can consume directly without parsing `gh` text/`--json` field lists — e.g. `pull_request_read` returning diff + checks + reviews in one typed shape.
  3. **Cross-repo search without a checkout** — `search_code`/`search_issues`/`search_repositories` over ALL of GitHub, when you don't have (and don't want) a local clone. `gh search` exists but the MCP versions carry richer qualifier docs inline.
  Default posture for a solo dev: **`gh` is the default for reads and routine writes; the github MCP earns keep-enabled only for create_pull_request, cross-repo search, and structured PR inspection.** Everything `gh` does just as well is a disable candidate on token-economy grounds.
- **vs gitnexus (local code tools).** No real overlap — gitnexus understands a repo **checked out locally** (call-graph, impact, symbols, taint, current with the worktree). github MCP is **remote metadata/collaboration** on repos you may not have locally (issues, PRs, releases, remote file reads). Local code comprehension → gitnexus; remote repo state/collaboration → github MCP.
- **vs deepwiki.** deepwiki = "how does this *public* repo work internally," AI-synthesized architecture with citations. github MCP = raw current repo *state/metadata* (this PR's files, this issue's comments) on any repo the token can see, incl. private. Understand an unfamiliar public codebase → deepwiki; act on/read live repo data → github MCP.
- **vs Exa/Tavily.** General web. Not repo-scoped. Only touch github MCP for GitHub-hosted data.

**One-line intuitions for the differentiator:**
- github MCP = "act on / read live GitHub repo state (issues, PRs, files, releases, search) for repos my token can see, incl. private."
- Prefer `gh` in Bash for anything it does equally well; reach for the MCP tool for `create_pull_request` (hook-safe), structured PR inspection, and cross-repo search.
- Not for: local code understanding (→gitnexus), public-repo architecture (→deepwiki), the open web (→Exa/Tavily).

---

## Disable candidates for a solo dev (FLAG — decision is Alex's)

Token-economy pruning, not correctness. All are read-only and low-value for a solo operator, or duplicated by `gh`:
- **`get_teams`, `get_team_members`** — team/org membership; a solo dev with no org teams gets near-empty results. Strong disable candidates.
- **`list_repository_collaborators`** — same reasoning for solo repos; occasionally useful. Softer candidate.
- **`request_copilot_review`** — write; overlaps the git-plugin PR-review flow. Candidate if Alex doesn't want the Copilot bot reviewer surfaced.
- **`search_users`** — rarely needed in a coding loop; `gh search users` covers it. Candidate.
- **`fork_repository`, `create_repository`** — infrequent, and `gh repo fork`/`gh repo create` are the muscle-memory path. Candidates on the "gh does it fine" axis.
- **Broad `gh`-duplicated reads** (`list_branches`, `list_tags`, `list_commits`, `get_commit`, `list_releases`, `get_latest_release`, `get_release_by_tag`, `get_tag`, `get_label`, `get_file_contents`): each is a judgment call — keep the ones whose structured output the model actually consumes, disable the rest to fight verbosity. Don't blanket-cut; decide per Alex's actual habits.

Do NOT casually disable: `get_me` (the server wants it called first for permissions/context), `create_pull_request` (the hook-safe win), the search-code/issues/PRs cluster (cross-repo, no-checkout value), `pull_request_read` (one-tool PR inspection incl. check runs).
