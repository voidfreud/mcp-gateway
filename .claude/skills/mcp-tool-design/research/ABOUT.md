# research/ — the per-backend understanding cache

One file per backend, `research/<backend>.md`, written by the pipeline's research step and read first the next time that backend is tuned — so understanding accumulates instead of being re-searched every session. These are working notes about what the tools *really do*; they are never broadcast text.

Each file carries:

- a dated header: when researched, the tool list seen at the time, sources used;
- per tool: what it actually does (beyond its own description), inputs/outputs as observed, quirks, failure modes and miss-signals, anything a live probe verified;
- overlap notes: distinguishers vs the specific siblings it clusters with (feeds `differentiation.md`).

Refresh a file, don't trust it, when: the backend's tool list differs from the header (compare against `surface.py`), an observed behavior contradicts a note, or the notes are more than a few months old. Delete the file when its backend is removed.
