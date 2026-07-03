# TypeScript Boilerplate

Read this file during Phase 3 (Build) of the CREATE PATH or when fixing code in the AUDIT PATH.

---

## package.json

Before scaffolding, check current package versions:

```bash
npm view @modelcontextprotocol/sdk version
npm view zod version
npm view typescript version
```

Use the latest stable versions unless the existing repo already pins a compatible range.

```json
{
  "name": "your-mcp-name",
  "version": "0.1.0",
  "description": "One clear sentence: what it does, what data source, key differentiator.",
  "keywords": ["mcp", "domain-specific", "keyword"],
  "author": "Your Name",
  "license": "MIT",
  "homepage": "https://github.com/user/repo",
  "repository": { "type": "git", "url": "https://github.com/user/repo.git" },
  "type": "module",
  "bin": { "your-mcp-name": "dist/index.js" },
  "files": ["dist"],
  "scripts": {
    "build": "tsc && chmod +x dist/index.js",
    "start": "node dist/index.js",
    "dev": "tsc --watch"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.29.0",
    "zod": "^4.3.6"
  },
  "devDependencies": {
    "typescript": "^6.0.3"
  }
}
```

All fields are required for Smithery's Server Metadata score. `homepage` and `repository` alone are worth 10pt.

For hosted servers with frontend/public pages, add a central copy check to `build` after the first working scaffold:

```json
"build": "tsc && chmod +x dist/index.js && node scripts/check-site-copy.mjs"
```

---

## tsconfig.json

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true
  },
  "include": ["src"]
}
```

---

## src/lib/fetch.ts

```typescript
export async function safeFetch(url: string, options?: RequestInit, timeoutMs = 10000): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, { ...options, signal: controller.signal });
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    return res;
  } finally {
    clearTimeout(timer);
  }
}
```

---

## src/lib/cache.ts

```typescript
interface CacheEntry<T> { data: T; expires: number; }
const store = new Map<string, CacheEntry<unknown>>();

export const TTL = {
  WEATHER:  30 * 60 * 1000,          // 30 min — live conditions
  FORECAST:  4 * 60 * 60 * 1000,     // 4 h   — daily forecasts
  DAILY:     6 * 60 * 60 * 1000,     // 6 h   — data updated daily
  STATIC:  365 * 24 * 60 * 60 * 1000, // 1 year — never changes
};

export async function getOrFetch<T>(key: string, ttlMs: number, fn: () => Promise<T>): Promise<T> {
  const hit = store.get(key);
  if (hit && Date.now() < hit.expires) return hit.data as T;
  const data = await fn();
  store.set(key, { data, expires: Date.now() + ttlMs });
  return data;
}
```

---

## src/lib/site-config.ts — hosted/public servers

Create this before writing frontend pages, server instructions, or connector setup docs. It is the single source of truth for values that otherwise drift across backend and frontend.

```typescript
export const SITE_CONFIG = {
  name: "Your Product Name",
  serverName: "your-mcp-name",
  siteUrl: "https://your-domain.com",
  mcpPath: "/mcp",
  connector: {
    name: "Your Product Name",
    description:
      "Use this for [specific current data/tasks]. Best for [high-intent prompts]. Do not use for [out-of-scope tasks].",
  },
  pages: {
    textSummaryPath: "/your-topic.txt",
    apiPath: "/api/your-topic",
  },
  examples: {
    locations: ["Tokyo", "Kyoto", "Hokkaido"],
    prompts: ["Where should I view sakura today?"],
  },
} as const;

export const SITE_URL = SITE_CONFIG.siteUrl;
export const MCP_ENDPOINT = `${SITE_CONFIG.siteUrl}${SITE_CONFIG.mcpPath}`;
export const TEXT_SUMMARY_URL = `${SITE_CONFIG.siteUrl}${SITE_CONFIG.pages.textSummaryPath}`;
export const API_URL = `${SITE_CONFIG.siteUrl}${SITE_CONFIG.pages.apiPath}`;

export const SITE_PUBLIC_CONFIG = {
  name: SITE_CONFIG.name,
  serverName: SITE_CONFIG.serverName,
  siteUrl: SITE_URL,
  mcpEndpoint: MCP_ENDPOINT,
  textSummaryUrl: TEXT_SUMMARY_URL,
  apiUrl: API_URL,
  connector: { ...SITE_CONFIG.connector, url: MCP_ENDPOINT },
} as const;
```

Use this module from:

- server instructions and tool output copy
- `/site-config.json`
- sitemap/robots/llms generation or template rendering
- ChatGPT connector setup copy
- build-time validation scripts

Do not copy-paste canonical URLs into static pages without a token/render/check path.

---

## src/index.ts — complete server template

```typescript
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";
import type { ToolAnnotations } from "@modelcontextprotocol/sdk/types.js";
import { getOrFetch, TTL } from "./lib/cache.js";
import { safeFetch } from "./lib/fetch.js";
import { readFileSync } from "fs";
import { resolve } from "path";

// ── Annotation constants ──
const READONLY: ToolAnnotations           = { readOnlyHint: true, idempotentHint: true };
const READONLY_EXTERNAL: ToolAnnotations  = { readOnlyHint: true, idempotentHint: true, openWorldHint: true };
const WRITE: ToolAnnotations              = { readOnlyHint: false, idempotentHint: false };
const DESTRUCTIVE: ToolAnnotations        = { readOnlyHint: false, destructiveHint: true };

// ── Static data — load once at startup, never inside handlers ──
function loadStaticJSON(filename: string) {
  const p = resolve(process.cwd(), "data", filename);
  try { return JSON.parse(readFileSync(p, "utf-8")); }
  catch { return null; }
}
const STATIC = {
  items: loadStaticJSON("items.json"),
};

// ── Server ──
const server = new McpServer(
  { name: "your-mcp-name", version: "0.1.0" },
  {
    instructions: `[Server name]. [One sentence: what it covers and data source].

Tool usage guide:
- Start with [domain.overview] for the big picture
- Use [domain.search] when the user gives specific criteria  
- Call [domain.detail] only after narrowing down with search
- [weather.forecast] last — check when conditions affect timing

All tools are read-only. No auth required.`
  }
);

// ── Tools ──
server.registerTool(
  "domain.overview",
  {
    title: "Overview of Domain Items",
    description: "Get a summary of all [items] with current status. Use this first before narrowing to specific items. Call domain.detail next for a single item.",
    inputSchema: {
      filter: z.string().optional()
        .describe("Optional filter by category. Accepted values: 'all', 'active', 'pending'. Defaults to 'all'.")
        .meta({ title: "Category Filter" }),
    },
    annotations: READONLY_EXTERNAL,
  },
  async ({ filter }) => {
    try {
      const data = await getOrFetch(`overview:${filter ?? "all"}`, TTL.FORECAST, () =>
        safeFetch(`https://api.example.com/items?filter=${filter ?? "all"}`).then(r => r.json())
      );
      return { content: [{ type: "text", text: JSON.stringify(data) }] };
    } catch (err) {
      return {
        isError: true,
        content: [{ type: "text", text: `Failed to load overview: ${err instanceof Error ? err.message : String(err)}. Try domain.search with a specific query instead.` }]
      };
    }
  }
);

// ── Prompts — 1–2 covering the most common full workflows ──
server.registerPrompt(
  "plan_full_workflow",
  {
    name: "plan_full_workflow",
    description: "Guide through the complete [domain] workflow: overview → search → detail.",
    argsSchema: {
      user_goal: z.string().describe("What the user wants to accomplish"),
    },
  },
  ({ user_goal }) => ({
    messages: [{
      role: "user",
      content: {
        type: "text",
        text: `Help me with: ${user_goal}. Use domain.overview to get started, then domain.search to narrow down, then domain.detail for specifics.`,
      },
    }],
  })
);

// ── Transport ──
const transport = new StdioServerTransport();
await server.connect(transport);
```

---

## Tool description rules

Every tool description must:
1. **Start with a verb**: Get, List, Search, Find, Create
2. **Name the resource specifically**: "cherry blossom bloom percentages" not "seasonal data"
3. **State the call-next tool** if there's a natural follow-up
4. **Stay under 2 sentences** — agents don't read long descriptions

**Bad:** `"This tool provides information about items"`
**Good:** `"Get live status for all [items] in the system. Filter by category. Call domain.search next to narrow results."`

---

## Parameter descriptions

Every parameter needs both `.describe()` and `.meta({ title })`:

```typescript
city: z.string()
  .describe("City name such as 'Tokyo', 'Kyoto', or 'Osaka'. Partial case-insensitive matching accepted.")
  .meta({ title: "City Name" }),

prefecture: z.string().optional()
  .describe("Filter by prefecture code, e.g. '13' for Tokyo. Omit to search all prefectures.")
  .meta({ title: "Prefecture Code" }),
```

`.meta({ title })` sets the display name shown in Claude's UI and is counted separately from `.describe()` in Smithery's parameter score.
