---
name: mcp-tool-design
description: Design or refine the advertised MCP surface of an existing backend—server instructions, tool names, descriptions, input guidance, resources, prompts, and safe gateway overrides—using primary evidence and a selected client profile. Use when tools are difficult to find, confused with siblings, miscalled, ambiguous, too broad, or need a small client-aware adjustment. Do not use to install a server, change topology, build a new server, or write general documentation.
---

# MCP surface design

Tune an existing backend's observable MCP surface with the smallest justified
change. Keep protocol facts separate from client evidence and local observation.

## Follow the workflow

1. **Select the target.** Name the user-selected target client or clients and
   one backend or surface. Ask for a missing target rather than assuming a
   client. Keep topology,
   installation, new-server work, and general documentation out of this work.
2. **Load the evidence.** Read [the generic MCP contract](references/generic-mcp.md)
   and [wording guidance](references/wording.md) for every change. Read
   [differentiation guidance](references/differentiation.md) when two or more
   tools overlap. Read [gateway levers](references/gateway-levers.md) before
   proposing a gateway override. Read only the selected profile or profiles:
   [Claude Code](references/clients/claude-code.md) and/or
   [Codex](references/clients/codex.md). Read the root
   [contributor manual](../../../AGENTS.md) and the
   [corpus retention manifest](../../../corpus/RETENTION.md) before repository
   work or evidence retention.
3. **Establish a read-only baseline.** Before drafting or applying a change,
   obtain explicit owner- or user-scoped configuration, capture, provider, or
   receipt evidence. Inspect only that evidence. If it is unavailable, produce
   a clearly labelled placeholder containing wording or questions only—never
   present it as a validated recommendation. A placeholder must not set or
   draft enablement, renames, parameter hiding or default injection, pinning or
   `always_load`, result caps, or behavior hooks. Leave existing levers
   unchanged and new levers unset until evidence supports them and the user
   authorizes the exact target. Do not probe a live backend, call an admin API,
   alter a client, or open local private state merely to discover a surface.
   Use [surface.py](scripts/surface.py) only with explicit files, for example:
   ```sh
   uv run python .agents/skills/mcp-tool-design/scripts/surface.py \
     --config ./config.toml --defaults-dir ./defaults --client generic
   ```
   It is an offline report: it never discovers daemon configuration, creates
   captures, contacts a backend, or resolves secrets. Use `--strict` to make
   missing captures and dangling overrides fail the inspection.
4. **Research the capability.** Prefer the protocol specification, the backend
   provider's documentation, and the selected client's official documentation.
   Mark an unsupported claim as unknown. Record machine-specific observations
   only in `research/`; keep that directory local and untracked.
5. **Diagnose before drafting.** State the task trigger, required inputs,
   expected result, exclusions, failure signal, and adjacent tools. Assess
   discoverability, ambiguity, and differentiation from the selected client's
   documented perspective. Treat uncertainty as a reason to research or keep
   the current text, not to invent instructions.
6. **Draft the smallest change.** Prefer a text change over a rename, pin,
   behavior hook, enablement change, or structural change. Preserve a stable
   public name unless evidence shows that it blocks correct use. Present the
   proposed diff and its evidence before any write.
7. **Obtain explicit authorization.** Before any write or live action, get
   the user's explicit authorization for its target. This includes writing
   configuration, using an admin API, changing a live client, or otherwise
   touching a running service. Treat a read-only inspection, research request,
   or draft approval as insufficient authorization.
8. **Validate two layers.** After an authorized change, validate the generic
   MCP contract plus only the user-selected client profile or profiles. Do not
   broaden validation to every supported client. Validate only the exact
   evidence-supported, explicitly authorized levers actually applied; never
   assume every requested lever was approved. Record which layer ran, its
   result, and any unverified local-only behavior. Do not claim a reload,
   discovery, ranking, or client result that the selected profile does not
   establish.
9. **Hand off cleanly.** Keep local receipts out of Git, update project tests
   and documentation only when their owning behavior changes, and follow the
   root contributor manual for issue, branch, PR, and verification work. Run
   [check_skill.py](scripts/check_skill.py) before handing off changes to this
   skill:
   ```sh
   uv run python .agents/skills/mcp-tool-design/scripts/check_skill.py
   ```

## Keep the boundary

- Avoid topology changes, installation, new-server construction, client setup,
  and broad documentation rewrites.
- Escalate behavior hooks, virtual tools, enablement, and backend changes as
  separately scoped work; do not disguise them as wording edits.
- Keep credentials, personal paths, captures, backend inventories, and live
  output out of this skill and the repository.
