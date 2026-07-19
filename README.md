# mcp-gateway

[![check](https://github.com/voidfreud/mcp-gateway/actions/workflows/check.yml/badge.svg)](https://github.com/voidfreud/mcp-gateway/actions/workflows/check.yml)

A local service that sits between MCP clients such as Claude Code and Codex and
the MCP servers they talk to, and
**rewrites everything those servers broadcast** — tool names, titles,
descriptions, parameter docs, resource and prompt text, and the server's own
instructions — while passing the actual tool calls through untouched. When an MCP server is written badly
enough that Claude can't tell what its tools do, you fix what Claude reads about
it here, without forking the server. It runs as one background daemon on your Mac,
shared by every client session, with a web admin UI at
**http://127.0.0.1:9100/admin**.

![The admin UI's backend view — live status, grouped controls, stale-override
repair, and inline tool editing (follows your system's light/dark theme)](docs/img/admin-backend-dark.png)

## Why this exists

MCP is largely wasted in Claude Code today, and the fault is upstream: server
authors ship tools with vague names, empty instructions, and parameter
descriptions that tell the model nothing. The protocol is fine — the surface
authors expose over it is not, so many servers arrive as dead weight Claude can't
reliably select or call. And that waste is doubly a shame, because Claude Code
**lazy-loads** tools: a server costs almost nothing to keep connected until one of
its tools is actually used. mcp-gateway reclaims those servers — it lets you
override the text a backend broadcasts until a sloppy server reads to the model
like a well-designed one, no fork required. It is hand-work, but it turns
otherwise-useless MCP servers into ones that work.

## Quickstart (about 5 minutes)

You need [`uv`](https://docs.astral.sh/uv/) installed. Then:

```bash
git clone https://github.com/voidfreud/mcp-gateway
cd mcp-gateway
./install.sh
```

`./install.sh` sets everything up: it builds the environment, installs a login
service so the gateway starts with your Mac, and starts it now. Removal is just
as easy: `./install.sh --uninstall` reverses it all, keeping your config and
state unless you add `--purge` (see
[docs/installation.md](docs/installation.md)). Confirm it's up:

```bash
curl -s http://127.0.0.1:9100/health   # -> ok mcp-gateway <version> @ /path/to/clone
```

To update a Path A installation after changes are merged to GitHub, run the
single guarded deployment command from a clean checkout on `main`:

```bash
just update
```

It fast-forwards from `origin/main`, synchronizes the locked environment,
reloads the LaunchAgent, and verifies `/health` and `/ready`. It preserves your
admin-edited configuration and runtime state.

Now open the admin UI at **http://127.0.0.1:9100/admin** and:

1. Click **Import MCP** and add a backend (its URL, or its local command).
2. Use the backend's **Claude Code** or **Codex** controls to register that
   independent MCP with either client.
3. Edit the backend's tool names and descriptions to taste — edits auto-save.
4. Optionally open **Virtual Tools** to compose or route several backend tools
   behind one gateway-owned tool on `/virtual/mcp`.

That's it. See [docs/admin-guide.md](docs/admin-guide.md) for the full tour.

## What you get

- **One endpoint per backend** — each backend is its own MCP server in Claude
  Code (`/<backend>/mcp`), with its own instructions budget.
- **First-class Virtual Tools** — create, validate, live-test, and activate
  gateway-owned composite/routing tools from the UI. Stable source bindings,
  concurrent fan-out, keyword/LLM selection, explicit fallbacks, rich MCP result
  preservation, and output budgets share one permanent `/virtual/mcp` endpoint.
- **Live editing with hot reload** — rewrite any tool name, title, description, or
  parameter doc; text changes apply to the running gateway instantly, no restart.
- **Connection status dots** — each backend shows a live green/red health dot with
  its tool count, probed through the real proxy.
- **Auto-refreshing tool lists** — the captured baseline refreshes itself on
  reconnect, on a backend's own change signal, and on admin page load; your edits
  are never clobbered.
- **Injected parameter defaults** — pin a fixed value the gateway sends on every
  call, and safely hide the parameter from Claude even when it's required.
- **Hard rename** — change a backend's real identity (endpoint, config key,
  registration) in one action, with a prompt to re-register.
- **One-click Claude Code registration** — register or remove a backend's endpoint
  from the UI, at the scope you choose, no terminal.
- **One-click Codex registration** — add or remove that same independent backend
  through Codex's own CLI/config. Codex discovers its tools after restart or in a
  new task; backends are never collapsed into one aggregate MCP.
- **Collision handling** — two tools can never share a broadcast name; bulk
  renames can auto-uniquify with a suffix.
- **Resource & prompt rewriting** — the same override story for everything else
  a backend broadcasts: resource names and descriptions, prompt renames (calls
  reverse-map to the original), prompt argument docs.
- **Behavior hooks** — attach your own `validate` (reject bad calls with a clear
  message) or `post_process` (reshape noisy output) Python function to any tool,
  without forking the backend.
- **Optional bearer token** — require an `Authorization` header on every endpoint
  and the admin API, for defense against other local processes. With it set, the
  gateway may also bind beyond loopback (e.g. a Tailscale IP) — refused otherwise.
- **Standard OAuth resource-server mode** — protect each independent backend and
  `/virtual/mcp` with endpoint-specific JWT audiences, RFC 9728 metadata, and
  proper 401/403 scope challenges. Login and token issuance stay in your
  external OAuth/OIDC provider; the Admin API uses a separate static token.
- **Export / import** — round-trip all your overrides as one JSON bundle.

## Alternative install (any platform)

To install just the `mcp-gateway` command without the login service (the only
option off macOS):

```bash
uv tool install git+https://github.com/voidfreud/mcp-gateway
mcp-gateway            # runs in the foreground; config auto-seeds at
                       # ~/.config/mcp-gateway/config.toml on first run
```

Distribution is uv-from-GitHub by choice — the package is not on PyPI.

## Documentation

- **[docs/installation.md](docs/installation.md)** — both install paths,
  upgrading, moving the repo, uninstalling.
- **[docs/admin-guide.md](docs/admin-guide.md)** — a full tour of the admin UI.
- **[docs/configuration.md](docs/configuration.md)** — the complete `config.toml`
  reference and how secrets work.
- **[docs/operations.md](docs/operations.md)** — the daemon, logs, health checks,
  backups, recovery, and troubleshooting.
- **[docs/security.md](docs/security.md)** — the threat model: what's protected and
  what isn't.
- **[docs/api.md](docs/api.md)** — the admin HTTP API for scripting.

## License

MIT. See [LICENSE](LICENSE).
