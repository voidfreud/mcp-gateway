# Virtual Tools black-box acceptance harness

Run the ADR-0005 contract harness explicitly:

```sh
uv run python tests/live/run_virtual_tools.py --keep
```

It starts two disposable loopback HTTP MCP fixtures and a fresh gateway child.
The child receives an isolated `HOME`, config, state, secrets path, hooks path,
and log file. It never contacts the installed daemon or an external provider.
All child process groups are stopped on completion; failure artifacts are always
retained, and `--keep` retains successful receipts too.

## Admin API contract exercised

The ADR deliberately specifies product behaviour before an endpoint schema.
This harness supplies the proposed stable, explicit black-box API contract:

| Operation | Request |
| --- | --- |
| list | `GET /admin/api/virtual-tools` |
| create draft | `POST /admin/api/virtual-tools` with a definition; returns the stored draft |
| configure | `PUT /admin/api/virtual-tools/{name}`; every edit remains/returns to draft |
| validate/resolve | `POST /admin/api/virtual-tools/{name}/validate`; returns `valid` plus live member resolution |
| live test | `POST /admin/api/virtual-tools/{name}/test` with `arguments`; returns the MCP result and runtime status |
| activate | `POST /admin/api/virtual-tools/{name}/activate`; publishes only after full validation and live resolution |
| disable | `POST /admin/api/virtual-tools/{name}/disable` |
| delete | `DELETE /admin/api/virtual-tools/{name}` |

Definitions bind to `backend_id`, `tool_original`, and original target parameter
names through each member's `args` and `static_args` maps. The fixture initially
uses `alpha` and `beta`; their generated stable IDs remain unchanged when alpha
is renamed, and the final receipt requires the binding to remain resolved.

The receipts require `/virtual/mcp` to be mounted before any tool is active,
to truthfully negotiate `tools.listChanged=false` until server-wide downstream
notifications exist,
then cover draft lifecycle, all-dispatch concurrency, keyword selection and
explicit fallback, rich MCP result-block and structured-output preservation,
partial/timeout/total failures, a strict aggregate budget, and stable references.
The LLM path is covered without contacting an external provider by unit/API
tests for consent binding and deterministic local fallback on router failure.
