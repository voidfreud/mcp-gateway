# Differentiation — one obvious tool per intent

The hard half of tuning. Several enabled backends can serve adjacent intents (documentation lookup, web search, repo understanding, page fetching…), and more arrive over time. The bar: for any task, a cold agent knows exactly which tool it needs and why — never picking between look-alikes at random. Using two siblings in sequence is fine and sometimes right; using one *for no stated reason* is the defect.

## Method

1. **Inventory the field.** `surface.py` (no args) — every enabled backend and tool. Only enabled ones exist for this exercise.
2. **Cluster by intent.** Group tools across all backends by the task family they'd surface for (e.g. "library API docs", "search the live web", "read a known URL", "understand a specific repo"). A tool can sit in two clusters.
3. **Research every cluster of two or more.** This is mandatory extra research beyond per-tool understanding (`research/` cache): establish the genuine distinguishers — coverage and corpus, freshness, input shape, output form and granularity, depth vs speed, cost/quota, auth tier, failure modes. If you cannot name a distinguisher, you haven't researched enough to write the boundary — or the tools genuinely duplicate and one should be disabled (surface that to the user).
4. **Write the boundary by situation, not by feature.** One line per pair, phrased in task language an agent can match against: "a *library's* API usage or current snippets → context7; a *specific repo's* internals and architecture → deepwiki." Where both apply, state the order and why ("resolve the id there first, then query here").
5. **Encode on both sides.** The boundary goes into *each* sibling's server instructions naming the other, and into descriptions as a when-not clause. One-sided boundaries fail: the agent may only ever read the wrong side. Keep the two descriptions' search vocabularies anchored to their distinct intents — if the same keywords dominate both, ToolSearch returns both and stage-2 selection falls back to chance.
6. **Verify as a cold agent.** Read the whole field's turn-0 surface end to end — every enabled server's instructions plus the bare deferred names. For each representative intent from step 2, is the route unambiguous? Any hesitation is a text defect; fix the text, not your memory of it. Run it twice: once with descriptions available (via search), and once in the **covered-descriptions variant** — instructions + bare callable names only — since that is the actual turn-0 view and names must carry the route on their own.
7. **Cold-eval with fresh agents.** The author cannot cold-read its own drafts — self-grading passed a field with zero renames needed. After any tuning pass, run the eval with fresh Opus subagents (cheap seats; never Fable for fan-outs), each given ONLY the turn-0 surface: every enabled server's instructions plus the bare deferred list (`mcp__<server>__<tool>`), descriptions withheld. Give each one representative intent per step-2 cluster and ask: "which tool do you reach for, and why?" Judge answers against the expected route — hesitation or a wrong pick is a text defect on the named string. Scriptable as a Workflow (deterministic, rerunnable as a regression eval after every pass):

   ```js
   // pipeline(intents) → route-guess (fresh Opus, turn-0 surface only) → judge vs expected
   const results = await pipeline(INTENTS,
     i => agent(`${TURN0_SURFACE}\n\nTask: ${i.intent}\nWhich tool do you reach for, and why?`,
                {model: 'opus', effort: 'low', phase: 'Route', schema: PICK}),
     (pick, i) => ({intent: i.intent, expected: i.expected, got: pick, defect: pick.tool !== i.expected}))
   ```

   `TURN0_SURFACE` is exactly `surface.py --turn0` (instructions + full callables, descriptions withheld); `INTENTS` is the step-2 cluster list with the expected route per intent. Report per-string defects; fix the string, rerun until clean.

   **Harness caveat (learned 2026-07-14):** eval seats inherit the RUNNING session's real MCP context, and that system-level context beats the simulated surface in the prompt — a backend enabled mid-session reads to the seats as nonexistent ("no such tool is exposed") no matter what the pasted surface says, and seats may even cite the session's CLAUDE.md or teammates. Only intents whose servers were connected when the session STARTED produce valid verdicts. After enabling or renaming backends, run the cold-eval from a fresh session that picked up the new field — treat same-session scores for new arrivals as noise, not defects.

## Harmonization

The field is a system: adding, removing, enabling, or disabling a backend changes every neighbour's boundary. On any field change, redo steps 1–2, then rewrite the boundaries of the affected cluster(s) — including removing a departed sibling from the texts that name it. A boundary clause referencing a disabled backend is noise at best, misdirection at worst.
