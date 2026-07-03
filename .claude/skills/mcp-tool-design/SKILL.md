---
name: mcp-tool-design
description: "Tunes the broadcast surface of the gateway's MCP backends — server instructions, tool names, descriptions, parameters, pinning — so a cold-start agent discovers the right tool, picks it over similar ones, and uses it correctly. Use when tuning, overriding, or sharpening a backend's tool text, when agents confuse, ignore, or misuse gateway tools, or when a backend is added, removed, or toggled (re-harmonize the field). Not for installing MCP servers or building new ones."
---

# Tune a backend's broadcast surface

The gateway rewrites every text a backend broadcasts to Claude Code — this skill is the pipeline for making that text razor-sharp for a cold-start agent: one that has never seen these tools, sees only what Claude Code shows it, and must reach for the right tool for the right reason without hesitating between similar ones. Changing the text is the easy half; the work is knowing what to write, grounded in research and the sources in `corpus/`, never in guesswork.

The user names the backend(s) to tune; with no target named, run a harmonization pass over the whole field. Scope is always the currently-enabled backends and tools only.

Execute this pipeline:

1. **Dump the live surface.** From the repo root run `uv run ${CLAUDE_SKILL_DIR}/scripts/surface.py` (add `<backend> --full` for the full text of a target). This is the text under judgment — instructions, effective names, descriptions, pinning, byte budgets.
2. **Load the discovery model.** Read [references/discovery.md](references/discovery.md) — what a cold agent sees at each stage (instructions and bare names at turn 0; descriptions only through tool search; params only after load). Every judgment below is made from that agent's seat.
3. **Research the target until understood.** Read the backend's `research/<backend>.md` cache first ([research/ABOUT.md](research/ABOUT.md)); build or refresh it for every tool not yet understood: provider docs, web research, read-only live probes (`POST /admin/api/run`) — until you can state what the tool really does, its inputs, outputs, quirks, and miss-signals. Write findings back to the cache.
4. **Grade the current text as a cold agent.** Read [references/wording.md](references/wording.md) and walk every string asking: seeing this for the first time, would I reach for it exactly when I should — and could I act on it without guessing? Record each insufficiency with the rule it breaks.
5. **Map the overlap field.** Read [references/differentiation.md](references/differentiation.md). Cluster every enabled tool across all backends by intent, research the genuine distinguishers of each cluster, and derive the boundary each pair needs — encoded on both sides.
6. **Draft replacements** per wording.md, with the reasoning attached. Come with the draft, not a questionnaire; present options only at a genuine fork the research cannot settle.
7. **Apply and verify live.** Read [references/levers.md](references/levers.md). Apply through the admin API, reconnect the client (`/mcp`), then re-run step 1 and re-read the live broadcast as a cold agent. Done means the live text is the graded text.
8. **Harmonize on field change.** A backend added, removed, enabled, or disabled shifts every neighbour's boundary — redo steps 5–7 for the affected clusters, including scrubbing departed siblings from texts that name them.
