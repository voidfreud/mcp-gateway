# mcp-authoring corpus

> **Provenance.** This was the standalone `mcp-authoring` project, absorbed into
> mcp-gateway on 2026-07-03 and retired as a separate repo. It's the research
> corpus the [`mcp-tool-design`](../.claude/skills/mcp-tool-design/SKILL.md) skill
> (in this repo, under `.claude/skills/`) was distilled from. Kept here as
> read-only source material; `skills/` are third-party reference clones (`.git`
> stripped) — do not edit them. The old project's `CLAUDE.md` is preserved as
> `ABOUT-corpus.md`.

A reference corpus for **writing high-quality MCP servers** — specifically the craft that
generic MCP tutorials skip: how to write tool **names**, tool **descriptions**, **parameter
descriptions / input schemas**, and server **`instructions`** so that a model reliably picks
and calls the right tool.

Gathered to seed an in-house MCP-authoring rubric/skill. Two kinds of material:

1. **`skills/`** — third-party Claude Code skills that target MCP authoring, cloned with their
   `.git` stripped (folders only, not tracked as submodules).
2. **`docs/`** — the authoritative specs, style guides, checklists, and articles that define the
   actual requirements.

This is source material. The distilled rubric now lives in this repo at
`.claude/skills/mcp-tool-design/` (it used to live global in `~/.claude/`).

## skills/

| Folder | Origin | What it is | Most relevant piece |
|--------|--------|------------|---------------------|
| `create-mcp/` | [haomingkoo/create-mcp](https://github.com/haomingkoo/create-mcp) | `/create-mcp` skill: full MCP lifecycle organized around 10 Smithery quality dimensions | Its quality-dimension table explicitly covers tool descriptions, parameter descriptions, and server instructions — the best single skill match |
| `sawzhang_skills/` | [sawzhang/sawzhang_skills](https://github.com/sawzhang/sawzhang_skills) | Multi-skill bundle (not all MCP-related) | `plugins/sawzhang-skills/skills/mcp-review/` — audits a server's tool design against 10 rules and outputs a scored report |
| `MCP-Builder-Skill/` | [EduardoRemedios/MCP-Builder-Skill](https://github.com/EduardoRemedios/MCP-Builder-Skill) | Comprehensive "definitive guide" builder skill (FastMCP + TS) | Documentation-standards section: LLM-friendly descriptions, parameter docs, usage examples + quality checklists |
| `official-mcp-integration/` | [anthropics/claude-code](https://github.com/anthropics/claude-code) `plugins/plugin-dev/skills/mcp-integration` | Anthropic's official integration skill | Config/naming/auth focused — confirms even the official skill doesn't teach description-writing (the gap this corpus fills) |

Note: `sawzhang_skills` is a full bundle — only `mcp-review` is on-topic; the rest (cca, twitter,
harness, etc.) came along with the repo and can be ignored.

## docs/

### spec/ — how MCP tools/instructions actually work (normative)
| Path | Source | Why it's here |
|------|--------|---------------|
| `mcp-spec-server-tools-2025-11-25.md` | modelcontextprotocol.io (2025-11-25, current) | Normative tools definition **incl. annotations + `outputSchema`/structuredContent** |
| `mcp-spec-server-tools.md` | modelcontextprotocol.io (draft) | Draft tools page (kept for diffing against current) |
| `mcp-spec-lifecycle-instructions.md` | modelcontextprotocol.io (2025-06-18) | Defines the server `instructions` field |
| `mcp-spec-server-prompts.md` | modelcontextprotocol.io (2025-11-25) | Prompts primitive — how reusable prompts work |
| `mcp-spec-server-resources.md` | modelcontextprotocol.io (2025-11-25) | Resources primitive — read-only data by URI |
| `mcp-spec-architecture.md` | modelcontextprotocol.io (2025-11-25) | Overall protocol architecture + design principles |
| `mcp-tool-annotations-essay.md` | blog.modelcontextprotocol.io | Maintainers on what `readOnly/destructive/idempotent/openWorld` hints can and can't do |

### anthropic/ — Claude API tool-use behavior & best practices
| Path | Source | Why it's here |
|------|--------|---------------|
| `claude-platform-define-tools.md` | platform.claude.com | Tool-definition best practices (3–4 sentence descriptions, namespacing, consolidation) |
| `claude-platform-tool-use-overview.md` | platform.claude.com | Tool-use model + when Claude fires a tool |
| `claude-platform-tool-reference.md` | platform.claude.com | Optional tool props: `cache_control`, `strict`, `defer_loading`, `allowed_callers` |
| `claude-platform-strict-tool-use.md` | platform.claude.com | Grammar-constrained schema conformance (`strict: true`) |
| `claude-platform-tool-search-tool.md` | platform.claude.com | Tool search + `defer_loading` mechanics and prompt-cache behavior |
| `claude-platform-programmatic-tool-calling.md` | platform.claude.com | Calling tools from code-execution; `allowed_callers` |
| `writing-tools-for-agents.md` | anthropic.com/engineering | The canonical Anthropic essay on tool-writing craft |
| `advanced-tool-use.md` | anthropic.com/engineering | Tool search, programmatic calling, `input_examples` (1–5 per tool) |
| `effective-context-engineering.md` | anthropic.com/engineering | Why tools must be token-efficient and unambiguous |

### claude-code/ — Claude Code & connector specifics
| Path | Source | Why it's here |
|------|--------|---------------|
| `claude-code-mcp.md` | code.claude.com | The reference: 2KB truncation of descriptions + server instructions, tool search, `alwaysLoad` |
| `claude-code-mcp-quickstart.md` | code.claude.com | End-to-end connect flow, scopes, config-on-disk |
| `claude-code-tool-search-guide.md` | code.claude.com | Tool search deep guide + `ENABLE_TOOL_SEARCH` modes |
| `claude-code-agent-sdk-mcp.md` | code.claude.com | Agent SDK MCP: transports, `allowedTools`, error handling |
| `claude-connectors-mcp-overview.md` | claude.com/docs | MCP overview + mandatory tool hints for connectors |
| `claude-connectors-building.md` | claude.com/docs | Connector build specs: **output limits (25k tokens / ~150k chars), timeouts, transports** |

### style-guides/ · checklists/ · articles/
| Path | Source | Why it's here |
|------|--------|---------------|
| `style-guides/googleapis-mcp-toolbox-style-guide.md` | googleapis/mcp-toolbox | `snake_case <action>_<resource>`, <5 params, enums, no prompt-injection imperatives |
| `style-guides/awslabs-mcp-design-guidelines.md` | awslabs/mcp | AWS MCP design guidelines |
| `checklists/mcp-probe-checklist.md` | incultnitollc/mcp-probe | The 5-question parameter-description rubric + schema-as-contract checklist |
| `articles/llmbestpractices-mcp-tool-design.md` | llmbestpractices.com | Naming, narrow types, description = firing predicate |
| `articles/yaw-mcp-schema-design.md` | yaw.sh | Schema design in production (2026) |
| `articles/merge-mcp-tool-description.md` | merge.dev | Tool-description overview + examples |
| `articles/qubittool-mcp-tools-best-practices.md` | qubittool.com | 5-component tool anatomy + pre-ship checklist |
| `articles/claude-blog-skills-vs-mcp.md` | claude.com/blog | Skills vs MCP: where instructions belong (MCP = how to use tools; skills = process/workflow) |

## Provenance

All `docs/` files carry their source URL. Skill repos were `git clone --depth 1` then had `.git`
removed on 2026-07-02; the official skill was pulled via sparse-checkout of the one relevant path.
