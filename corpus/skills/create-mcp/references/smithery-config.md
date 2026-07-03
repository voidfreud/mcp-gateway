# Smithery Config & Scoring Reference

Read this during Phase 3 (Build), the AUDIT PATH, and the PUBLISH PATH.

---

## smithery.yaml — stdio servers (installed via npx)

**With auth (API key):**
```yaml
startCommand:
  type: stdio
  configSchema:
    type: object
    properties:
      apiKey:
        type: string
        title: "API Key"
        description: "Your API key from example.com/settings"
    required: []
  commandFunction: |-
    (config) => ({
      command: "npx",
      args: ["your-mcp-name"],
      env: config.apiKey ? { API_KEY: config.apiKey } : {}
    })
```

**No auth:**
```yaml
startCommand:
  type: stdio
  configSchema:
    type: object
    properties: {}
    required: []
  commandFunction: |-
    (config) => ({ command: "npx", args: ["your-mcp-name"] })
```

The `required: []` matters — Smithery awards 15pt for optional config (vs. 10pt if you mark fields as required).

---

## smithery.remote-config.json — hosted HTTP servers

**With optional auth:**
```json
{
  "type": "object",
  "properties": {
    "apiKey": {
      "type": "string",
      "title": "API Key",
      "description": "Optional API key for authenticated endpoints"
    }
  },
  "required": []
}
```

**No config needed:**
```json
{ "type": "object", "properties": {}, "required": [] }
```

---

## Smithery scoring reference

| Category | Points | How to earn it |
|---|---|---|
| Tool descriptions | 12pt | Verb-first, ≤2 sentences, states next tool |
| Parameter descriptions | 11pt | `.describe()` + `.meta({ title })` on every param |
| Annotations | 7pt | `readOnlyHint` minimum; `openWorldHint` for external APIs |
| **Tool names** | **5pt** | Navigable names. Smithery rewards dot notation, but target-client compatibility takes priority. |
| Prompts | 5pt | Register ≥1 prompt for a real workflow |
| Resources | 5pt | Awarded automatically — no action needed |
| Server description | 10pt | Set in Smithery UI settings (not package.json) |
| Homepage | 10pt | `homepage` field in package.json |
| Icon | 7pt | Upload in Smithery UI settings |
| Display name | 3pt | Set in Smithery UI settings |
| Config schema | 10pt | `smithery.yaml` or `smithery.remote-config.json` present |
| Optional config | 15pt | All config fields in `required: []` (not required) |

**Total: 100pt**

---

## Score ladder

| Score | What's typically missing |
|---|---|
| ~60 | Noun-first descriptions, no annotations, incomplete package.json |
| ~75 | Descriptions fixed, annotations added, package.json complete |
| ~85 | Server instructions added, parameter descriptions complete |
| ~90 | Prompts registered, caching and error handling clean |
| ~95 | smithery.yaml with configSchema, README polished |
| ~98 | All code dimensions maxed, Smithery UI metadata not set |
| **100** | **All scoring dimensions plus Smithery UI icon/display name/description** |

---

## Tool naming tradeoff

Smithery's exact tooltip text: *"Measures how well tool names form a navigable tree using dot-notation (e.g., admin.tools.list). Scores higher when hierarchy depth matches the ideal for the number of tools — flat lists of many tools and unnecessarily deep nesting both reduce the score."*

Do not blindly optimize for this if the target client cannot reliably call dotted tool names.

Rules:
- Default to client-compatible names such as `sakura_forecast`, `user_search`, `order_create`
- Avoid vague uniform prefixes such as `get_*` when a domain/action name is clearer
- Use dot notation only when the target MCP clients have been tested with dotted names
- Accept a lower Smithery tool-name subscore when compatibility requires it; 98/100 with reliable Claude usage is a good outcome

Client-safe example structure:
```
sakura_forecast    koyo_forecast    weather_forecast
sakura_spots       koyo_spots       flowers_spots
sakura_best_dates  koyo_best_dates  fruit_seasons
                   kawazu_forecast  fruit_farms
                                    festivals_list
```

---

## Smithery UI metadata (20pt — code alone can't earn these)

After the server is indexed on Smithery, complete these in the UI:

1. smithery.ai → your server → Settings
2. **Upload icon** (PNG, 256×256 recommended) → **+7pt**
3. **Set display name** (human-readable, not the npm slug) → **+3pt**
4. **Set server description** (1–2 sentences, separate from package.json) → **+10pt**

These 20 points require manual action. Remind the user after pushing code.

---

## Smithery publish commands

**Hosted HTTP servers:**
```bash
npx @smithery/cli mcp publish \
  "https://YOUR_DOMAIN/mcp" \
  -n YOUR_GITHUB_USERNAME/YOUR_REPO_NAME \
  --config-schema "$(cat smithery.remote-config.json)"
```

**stdio servers:** smithery.ai → Add Server → paste GitHub URL → trigger scan.
