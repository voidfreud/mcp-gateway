# MCP Discovery Guide

Read this when the user cares about adoption, ChatGPT, AI search, directories, or reducing setup friction.

---

## The core distinction

There are two different products around an MCP server:

1. **AI search / web browsing**: the assistant finds public pages, text files, JSON APIs, and `llms.txt`, then cites them. This does not allow MCP tool execution.
2. **MCP tool use**: the user or workspace admin connects the MCP server in an MCP-capable client. Only then can the assistant call tools.

Do not imply that ChatGPT Search, Google AI Mode, Perplexity, or another search product can run an MCP just because it found the repo or endpoint. If a hosted server exposes public/current data, also ship crawlable pages or API endpoints that answer the same high-intent searches.

Normal ChatGPT web chat is missing the MCP client runtime:

- no user-configurable MCP tool registry for arbitrary servers in the chat
- no external stdio/HTTP transport binding for your server
- no dynamic tool invocation bridge from the model to your endpoint
- no persistent connection/session to a newly discovered MCP server

So if the user says "ChatGPT can find my MCP but refuses to run it", treat that as expected behavior, not a server bug. Improve the crawlable/search surface and provide connector setup instructions.

---

## For hosted public-data MCPs

Add these alongside `/mcp`:

- `/health` returning JSON with `status`, `server`, and `version`
- `/llms.txt` with project summary, canonical pages, APIs, and MCP endpoint
- `/sitemap.xml` containing the most important human and AI-readable URLs
- Topic pages for the main use cases, e.g. `/cherry-blossom-forecast`
- Plain text or Markdown summaries for high-intent AI-search queries, e.g. `/sakura-forecast.txt`
- JSON APIs for current data when useful, e.g. `/api/sakura/forecast`
- `robots.txt` that allows citation/search bots you want to serve

## Single source of truth for public MCPs

Do not maintain the same facts separately in backend output, frontend pages, `llms.txt`, README snippets, ChatGPT connector copy, and registry metadata. Centralize:

- canonical site URL and MCP endpoint
- AI-search text/JSON URLs
- ChatGPT app/connector name and operational description
- example prompts and example locations
- seasonal timing guidance and data-freshness wording
- next-tool guidance such as "use sakura_spots for the full list"

Preferred implementation:

1. Put canonical metadata in one module such as `src/lib/site-config.ts` or one JSON config consumed by both server and scripts.
2. Have backend outputs and server instructions import from that module.
3. Serve public pages from templates or tokens rendered by the server, instead of hardcoding URLs and connector copy in static HTML.
4. Add a build-time validation script that fails if public templates hardcode canonical URLs or if `package.json`, `server.json`, README snippets, and frontend templates drift from the source.

This is especially important for hosted MCPs because users compare directory pages, README, frontend pages, ChatGPT connector setup, and actual MCP output. Conflicting copy makes the MCP feel broken even when the tools work.

The plain text summary should be useful without MCP setup. Include:

- Source and freshness timestamp/date
- Current short answer
- Full table/list of important current values
- Canonical URLs for text, JSON, and MCP endpoint
- A short note: web search can cite this page; MCP tools require connecting the endpoint first

For data-only ChatGPT/deep-research compatibility, implement `search` and `fetch` tools that return exactly one text content item containing JSON. Use canonical URLs in results so the model can cite pages.

---

## ChatGPT app / connector setup

For ChatGPT, a remote MCP server must be connected as an app/connector before ChatGPT can call tools. Use official OpenAI docs as the source of truth because names and UI paths change.

Minimum metadata to provide in README and landing pages:

```text
Name: Your Product Name
Description: Use this for [specific current data/tasks]. Best for [high-intent prompts]. Do not use for [out-of-scope tasks].
Connector URL: https://your-domain.com/mcp
```

A good connector description is operational, not marketing copy. It should tell the model when to use the app and when not to use it.

When writing docs, say:

- "If ChatGPT found this through search, cite the public forecast/API page."
- "To call MCP tools, connect `https://your-domain.com/mcp` as a ChatGPT app/connector first."

---

## Reducing local setup friction

For local stdio MCPs, provide three tiers:

1. **MCPB bundle** for one-click Claude Desktop-style install when targeting nontechnical users
2. **npx config** for Claude Desktop, Claude Code, Cursor, Windsurf, and other stdio clients
3. **Manual clone/build** for developers

MCPB is best when the server runs locally and users should not edit JSON or install dependencies by hand. It does not replace npm for developer installs, but it reduces setup friction for Claude Desktop users.

Minimal MCPB direction:

```bash
npm run build
npm install --production
npx @anthropic-ai/mcpb pack
```

The bundle needs a `manifest.json` with the server command. For Node stdio servers, the important part is:

```json
{
  "manifest_version": "0.3",
  "name": "your-mcp-name",
  "version": "0.1.0",
  "description": "Brief description of what the MCP server does.",
  "author": { "name": "Your Name" },
  "server": {
    "type": "node",
    "entry_point": "dist/index.js",
    "mcp_config": {
      "command": "node",
      "args": ["${__dirname}/dist/index.js"]
    }
  }
}
```

If the server needs API keys, define `user_config` and pass values via `mcp_config.env`.

---

## Publish/version checklist

npm versions are immutable. Before publishing:

```bash
npm view your-package-name version
npm outdated --json
npm run build
npm pack --dry-run
```

If the local version already exists on npm, bump first:

```bash
npm version patch --no-git-tag-version
```

If the repo has `server.json`, sync it to the same version before publishing. Then:

```bash
npm publish
```

After publishing hosted HTTP servers, deploy the website/server too. npm publish updates the package, not the hosted endpoint. Verify:

```bash
curl https://your-domain.com/health
curl -I https://your-domain.com/your-ai-search-page.txt
```

---

## Directory submission order

1. Official MCP Registry (`server.json`) when applicable
2. Smithery
3. Glama
4. PulseMCP
5. mcp.so
6. awesome-mcp-servers PR

Directory discovery helps developers find the MCP. AI-search pages help non-MCP users get useful answers without setup.
