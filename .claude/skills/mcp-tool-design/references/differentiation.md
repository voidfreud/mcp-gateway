# Differentiation — one obvious tool per intent

The hard half of tuning. Several enabled backends can serve adjacent intents (documentation lookup, web search, repo understanding, page fetching…), and more arrive over time. The bar: for any task, a cold agent knows exactly which tool it needs and why — never picking between look-alikes at random. Using two siblings in sequence is fine and sometimes right; using one *for no stated reason* is the defect.

## Method

1. **Inventory the field.** `surface.py` (no args) — every enabled backend and tool. Only enabled ones exist for this exercise.
2. **Cluster by intent.** Group tools across all backends by the task family they'd surface for (e.g. "library API docs", "search the live web", "read a known URL", "understand a specific repo"). A tool can sit in two clusters.
3. **Research every cluster of two or more.** This is mandatory extra research beyond per-tool understanding (`research/` cache): establish the genuine distinguishers — coverage and corpus, freshness, input shape, output form and granularity, depth vs speed, cost/quota, auth tier, failure modes. If you cannot name a distinguisher, you haven't researched enough to write the boundary — or the tools genuinely duplicate and one should be disabled (surface that to the user).
4. **Write the boundary by situation, not by feature.** One line per pair, phrased in task language an agent can match against: "a *library's* API usage or current snippets → context7; a *specific repo's* internals and architecture → deepwiki." Where both apply, state the order and why ("resolve the id there first, then query here").
5. **Encode on both sides.** The boundary goes into *each* sibling's server instructions naming the other, and into descriptions as a when-not clause. One-sided boundaries fail: the agent may only ever read the wrong side. Keep the two descriptions' search vocabularies anchored to their distinct intents — if the same keywords dominate both, ToolSearch returns both and stage-2 selection falls back to chance.
6. **Verify as a cold agent.** Read the whole field's turn-0 surface end to end — every enabled server's instructions plus the bare deferred names. For each representative intent from step 2, is the route unambiguous? Any hesitation is a text defect; fix the text, not your memory of it.

## Harmonization

The field is a system: adding, removing, enabling, or disabling a backend changes every neighbour's boundary. On any field change, redo steps 1–2, then rewrite the boundaries of the affected cluster(s) — including removing a departed sibling from the texts that name it. A boundary clause referencing a disabled backend is noise at best, misdirection at worst.
