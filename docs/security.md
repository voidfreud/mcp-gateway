# Security

The gateway is a local service. This page explains, plainly, what it protects
against, what it does not, and how to tighten it.

## The starting point: loopback only

The gateway binds to `127.0.0.1` (loopback) by default. Nothing off your
machine can reach it — not another device on your network, not the internet.
This is the primary protection.

The trade-off: any process running as you, on the same machine, *can* reach the
loopback port. On a single-user Mac that is usually a non-issue. The bearer token
below exists for when it is not.

### Binding beyond loopback

Setting `host` to a non-loopback address (say, your Tailscale IP, to share one
gateway across your own machines) is supported, with one hard rule: **the
config refuses to load a non-loopback `host` without either `bearer_token` or a
complete `[oauth]` profile with `oauth.admin_bearer_token`.**
An open bind would hand config writes and tool execution to anything that can
reach the port, so the gateway fails loudly at startup instead of running
exposed. With the token set, every backend endpoint and every `/admin/api/*`
route demands it; `/health`, `/ready`, and the bare `GET /admin` page remain
open, and the Origin guard still rejects foreign browser origins.

Only bind to an interface you trust end-to-end (a tailnet, not a café LAN, and
never the public internet without TLS). Remember a static Admin token is a
shared secret: every host you give it to can do everything the gateway can.

### Outbound requests and update checks

Loopback limits who can call the gateway; it does not block the gateway's own
outbound connections. Configured HTTP backends, local stdio backends, tool
hooks, and tool calls can all reach services with the daemon user's network
access. The starter config includes DeepWiki and Context7 and contacts them for
initial metadata capture.

By default the daemon also sends one HTTPS GET to the fixed public
`mcp-local-gateway` PyPI JSON endpoint at startup and every 24 hours. Its
User-Agent includes the installed gateway version; the response is size- and
time-bounded, failures do not affect availability, and no update is applied
automatically. Set top-level `update_check = false` (or turn off **Daily update
checks** in Gateway settings) to eliminate those requests.

The explicit `mcp-gateway update` command also fixes its package source to
public PyPI and ignores ambient `uv`/`pip` index, find-links, constraint, and
override settings. HTTPS proxy and custom certificate environment settings
remain available; use the verified GitHub Release fallback when public PyPI is
not reachable.


## Standard OAuth resource-server mode

For a remote deployment, use `[oauth]` rather than putting a static bearer token
on every MCP endpoint. The gateway is an OAuth **resource server**, not an
authorization server. Your external OAuth/OIDC provider performs login,
consent, PKCE, client registration, and token issuance. The gateway validates
JWT signatures through `jwks_uri`, exact `issuer`, expiry, endpoint-specific
audience, and scope.

Every independent backend and `/virtual/mcp` has its own protected-resource
identifier and RFC 9728 metadata document. A client discovers the matching
authorization server from the endpoint's
`/.well-known/oauth-protected-resource/<endpoint>/mcp` response. A missing or
invalid token receives `401` with a `resource_metadata` challenge; a valid token
without `required_scopes` receives `403 insufficient_scope` with the required
`scope` and metadata URL.

The Admin API is intentionally separate: set
`oauth.admin_bearer_token = "${MCP_GATEWAY_ADMIN_TOKEN}"` for any non-loopback
bind. That token gates `/admin/api/*` only and is never used as an MCP access
token. `[oauth]` and the legacy `bearer_token` setting are mutually exclusive.

## The Origin guard (built in, always on)

Web browsers can be tricked, by a technique called DNS rebinding, into making
requests to a local gateway from a malicious web page you are visiting. The MCP
specification requires servers to defend against this, and the gateway does: a
middleware inspects the `Origin` header on every request and returns `403` for
an origin it does not recognize.

The recognized set is deliberately narrow: the configured gateway host and
port, the standard loopback spellings for that port, and—when OAuth is enabled—
the validated `oauth.public_base_url`. The OAuth origin is needed for the
configured deployment's browser traffic; it is not a general cross-origin
allowlist. `Origin: null` and all other origins are rejected. This secure
default applies to loopback and remote deployments alike.

- Requests from a real browser page always carry that page's `Origin`, so a
  rebinding attempt is rejected.
- Requests from non-browser MCP clients (including Claude Code, Codex, and
  `curl`) carry no `Origin` and pass normally.

This applies to every route, including the admin UI and `/health`. Apart from
the gateway bind configuration and the validated OAuth deployment origin, it
has no separate configuration.

## The optional bearer token

By default any local process can hit the loopback port. If you want defense
against a curious or compromised process running as you, set a bearer token.

In `config.toml` (or via the admin UI), set a token — always as an environment
reference, never a raw value:

```toml
bearer_token = "${MCP_GATEWAY_TOKEN}"
```

Supply the value through the environment or the gateway secrets file (see
[Secrets handling](#secrets-handling)). The gateway resolves it **once** at
startup; if the variable is missing — or set but **empty** — startup fails
loudly rather than running unprotected. An explicitly empty `bearer_token` is
also rejected. The same rule applies to `oauth.admin_bearer_token`.

**What it protects.** With a token set, every backend MCP endpoint requires
`Authorization: Bearer <token>` — anything else gets a `401`. Crucially, **the
admin API is gated too**: an open admin API would let the same local process
rewrite your config, restart the daemon, or execute backend tools through the
mini-inspector, so token protection would be pointless without it. Only three
things stay open: `/health`, `/ready`, and the bare `GET /admin` page shell (the
static UI has to load so it can prompt you for the token — every piece of data it
then fetches is challenged).

**How the admin UI handles it.** On the first `401`, the UI prompts you for the
token and stores it in the browser's local storage, so you enter it once.

### The CLI and the bearer token

The `mcp-gateway` control CLI is just another client of `/admin/api/*` and is
gated identically. It never accepts a token as a command-line argument (that
would leak into shell history and process listings) and never prints a resolved
secret. Instead it resolves the token in this order:

1. The variable named by `--token-env NAME` — the variable must be set, or the
   command fails rather than silently falling back.
2. `MCP_GATEWAY_ADMIN_TOKEN`.
3. The configured `bearer_token` (or `oauth.admin_bearer_token`) `${ENV}`
   reference, resolved with the same environment-then-secrets-file precedence
   the daemon uses: the process environment wins, then the gateway secrets
   file (`MCP_GATEWAY_SECRETS`, default `~/.config/mcp-gateway/secrets.env`).
   The explicit `--token-env` variable and `MCP_GATEWAY_ADMIN_TOKEN` are read
   from the process environment only.

Two rules bound where that token can go:

- **An explicit `--url` receives no implicit token.** If you pass `--url`,
  neither `MCP_GATEWAY_ADMIN_TOKEN` nor the configured token reference is
  applied — authenticating against that explicit endpoint requires
  `--token-env NAME`.
- **Transport follows the token.** A token is sent over plain HTTP only
  toward a verified loopback address (`localhost`, `127/8`, `::1`); any
  other token-bearing target must use HTTPS, and **all** redirects are
  refused — the `Authorization` header never follows a redirect. The default
  config-derived loopback URL is therefore safe with the implicit token; a
  remote gateway needs HTTPS plus an explicit `--token-env`.

If the CLI shows the token setting at all (for example `settings show`), it
prints the stored `${ENV}` reference, never the resolved value, and `--json` output contains no credential material. Requests carry no `Origin`, so the
Origin guard passes them like any non-browser client. Human (non-`--json`)
output — including error messages on stderr — escapes terminal control
characters from remote data so a hostile description or log line cannot
spoof the terminal; `--json` preserves the data structurally. File output is protected too: `settings export -o FILE`
refuses to overwrite an existing path unless `--force`, replaces only regular
files (never follows symlinks), and writes atomically with `0600`
permissions.

`check` is the deliberate exception: it probes the auth-exempt `/ready`
endpoint and neither resolves nor sends an Admin bearer token, so
`--token-env` does not apply to it — a secrets-only deployment or an unset
`--token-env` never blocks a liveness probe. For automation, `--json` makes
every finite control/query/mutation command emit exactly one JSON value;
`--help` and a foreground `run` are normal terminal behaviors and print no
JSON, and the streaming `logs follow --json` emits one JSON object per line
(NDJSON).

**Client registration.** Every client registration must carry the configured
credential or its calls return `401`. The gateway does not register endpoints
for you — add each `/<backend>/mcp` endpoint in the client you run, using that
client's supported configuration or CLI, and carry the credential there. The
mechanisms differ by client:

**Claude Code.** The registration carries an `Authorization` header. Include it
when registering the endpoint:

```bash
claude mcp add --transport http gateway-<name> http://127.0.0.1:9100/<name>/mcp --header "Authorization: Bearer ${MCP_GATEWAY_TOKEN}"
```

`--header` is variadic, so it must come **after** the positional name and URL;
otherwise Claude Code treats them as header values. The command expands
`${MCP_GATEWAY_TOKEN}` in the shell that runs it, so export the variable there
without printing its value. If you add or change the token later, re-register
the backend so Claude Code sends the new header.

**Codex.** Codex's CLI accepts a bearer-token environment
variable rather than a literal header. With `bearer_token = "${MCP_GATEWAY_TOKEN}"`,
register the endpoint with:

```bash
codex mcp add gateway-<name> --url http://127.0.0.1:9100/<name>/mcp --bearer-token-env-var MCP_GATEWAY_TOKEN
```

Only the variable name is written to Codex configuration. The Codex desktop,
CLI, or IDE process must itself receive that environment variable; putting the
value only in the gateway's secrets file is not sufficient for Codex calls.

The token comparison is constant-time, and the token is never written into
anything the gateway stores or echoes back (logs, config backups).

The same middleware protects `/virtual/mcp` and every Virtual Tool CRUD,
validation, and live-test route. Virtual Tools do not create an authentication
side door around the underlying backend endpoints.

### LLM routing data egress

`all` and `keyword` dispatch stay local to the gateway and selected backends.
`llm` dispatch sends the Virtual Tool inputs, routing descriptions, and policy to
OpenRouter. Activation is rejected unless the administrator acknowledges the
exact model, key reference, fields, member descriptions, and routing policy;
that consent fingerprint becomes stale whenever those settings change. The
router requests at most 200 output tokens, and the selected OpenRouter model's
pricing applies. The API key must be a single
`${ENV_VAR}` reference; only the resolved secret is placed in the provider request
and it is never returned or logged. Router timeout, malformed output, or provider
failure uses the configured local fallback.

## What is *not* protected

Be clear-eyed about the boundaries:

- **Anyone holding the token** can use the gateway fully. The token is a shared
  secret, not per-user identity.
- **A process running as you, when no token is set,** can reach every endpoint —
  that is the default posture, appropriate for a single-user machine.
- **Local root / another admin user** on the machine can read the secrets file and
  the token from the environment, and can inspect the process. The gateway is not
  a defense against an attacker who already has that level of access to your Mac.
- **The gateway does not add authentication to the backends themselves** beyond
  passing along the credentials you configure. It is a proxy, not an identity
  provider.

## Secrets handling

Dedicated credential fields never accept raw values: `bearer_token` and
`oauth.admin_bearer_token` must each be one `${ENV_VAR}` reference, while a
backend `auth_value` must be exactly one `${ENV_VAR}` reference or a
`Bearer`/`Basic`/`Token` prefix followed by one — the same exact template
rule applies to `headers`/`env` values under credential-like keys. The
gateway resolves references from:

1. the process environment, or
2. the gateway secrets file `~/.config/mcp-gateway/secrets.env` (`KEY=VALUE` per
   line; path overridable with `MCP_GATEWAY_SECRETS`).

The environment wins on a conflict, and a missing or empty reference fails
startup loudly rather than sending an empty credential.

Values from the secrets file are deliberately kept **out** of the process
environment. This matters when you run local `stdio` backends: those run as
subprocesses that inherit the daemon's environment, and keeping secrets out of it
means one backend's subprocess cannot read a token meant for another. Every
backend's auth reference still resolves from the same file, so:

- Put **only** the tokens the gateway's own backends need in `secrets.env`.
- **Never** point `MCP_GATEWAY_SECRETS` at a global key store — you would be
  exposing unrelated secrets to the gateway's resolution path.

Backend `headers` and `env` values under a **recognized credential-like key**
(`authorization`, `proxy-authorization`, `cookie`, `token`, `secret`,
`password`, `passwd`, `api-key`, `apikey`, `private-key`, `credential`,
`access-key`, `dsn`, `database-url`, `redis-url`, `mongodb-uri`,
`connection-string` — matched case-insensitively after normalizing `_` to
`-`, so `API_KEY`, `X-Api-Key`, `DB_PASSWORD`, `DATABASE_URL`, and
`client_secret` all classify) MUST be exactly one `${ENV_VAR}` reference or a
`Bearer|Basic|Token ${VAR}` template — a raw literal, or a reference mixed
with any other raw text, is rejected with `400`. An **env** key whose
normalized name ends in `-file`, `-path`, `-dir`, or `-directory` (for
example `PASSWORD_FILE`, `PASSWORD_STORE_DIR`, `TOKEN_CACHE_DIR`) is treated
as non-secret metadata and may be literal — headers never get this
exemption. A composite connection string (a full DSN, `DATABASE_URL` with
embedded credentials, …) still classifies as credential-like: store the
complete value in one environment variable and reference it. Ordinary names
(`HOME`, `LANG`, `X-Tenant`) never match and may stay literal. This
classifier is a guardrail, not a guarantee: arbitrary innocuous names cannot
be perfectly classified, and a credential hidden under an unrecognized name
would not be caught. Never rely on classification to make a credential safe
— if a value could be a secret, reference it. Arbitrary URLs, arguments, and
other subprocess environment values cannot be classified reliably either, so
never place credentials there as literals. On POSIX systems, config and
secrets files are created or repaired with user-only `0600` permissions; the
macOS resident service keeps its config, state, and wrapper directories at
`0700`.
Treat `config.toml` as sensitive operational data even though its dedicated
credential fields hold references. The secrets file must never be committed or
shared.

## Keeping dangerous tools off

Independently of the token, you can drop any tool you do not want exposed at all
by disabling it (`enabled = false` on the tool, or the toggle in the admin UI).
A disabled tool is not broadcast to an MCP client and cannot be called through the
gateway.

## Behavior hooks run your code

Per-tool [behavior hooks](configuration.md#behavior-hooks-validate--post_process)
(`validate` / `post_process` on a tool override) are **arbitrary code execution
inside the daemon, by design**. A hook is a Python function the gateway imports
from the hooks directory (`MCP_GATEWAY_HOOKS` > `./hooks/` > `~/.config/mcp-gateway/hooks/`)
and runs on every call to that tool, with the daemon's full privileges — your
user account, the daemon's environment, its network access. Treat the hooks
directory with exactly the same care as a stdio backend `command` in
`config.toml`: both are local-admin-owned code the gateway will execute.

What the gateway does and does not guarantee:

- **Config strings are never evaluated as code.** A hook spec is a
  `module:function` reference; the module part must be a bare identifier, so it
  always resolves to a `.py` file *inside* the hooks directory (no path
  traversal), and it is imported with `importlib`, never `eval`'d.
- **Whoever can write the hooks dir (or `config.toml`) owns the daemon.** That
  was already true — config can set a stdio backend `command` — so hooks add
  no *new* trust boundary, but they make the existing one worth restating.
  Keep both writable only by your user.
- **Load failures fail closed, per tool.** A hook that cannot be loaded never
  silently disappears (that would drop a guard you deliberately configured):
  the tool's calls error with the load failure until the file is fixed, while
  the backend's other tools and the mount stay up. Watch `hook_load_error` in
  the log and `hook_error` in `/admin/api/state`.
- **Hooks are not a sandbox.** A `validate` hook can reject calls, but it runs
  in-process; a malicious or buggy hook can do anything the daemon can. Review
  hook code like you would review a shell script you install on PATH.

## Session isolation between callers

Warm backends (`stateless = false`) share one persistent backend session across
every local caller. That is safe here because backend credentials are fixed at
boot from `${ENV}` references — identical for all callers — and the gateway
never forwards a caller's own `Authorization` header to a backend (its optional
bearer token is consumed at the gateway).

If you ever add a backend that authenticates **per caller** from the incoming
request, flip that backend to `stateless = true` (config, or
`POST /admin/api/backend/{name}/stateless`): each call then gets a fresh,
isolated backend session. The reasoning is recorded in
[ADR-0004](decisions/0004-per-session-isolation.md).

The gateway does not currently add a worker process per backend. Stdio
backends already run in FastMCP-owned child processes, while remote backends
are data-only HTTP clients; each backend still has an independent lifecycle
runner and recycle path. A separate worker is justified only for untrusted
in-process code, independent CPU/memory limits, caller-supplied credentials, or
daemon-level dependency crashes. That boundary and its future acceptance
criteria are recorded in [ADR-0009](decisions/0009-process-isolation-boundary.md).

## Related

- [configuration.md](configuration.md#secrets) — the secrets mechanism in the
  config reference.
- [operations.md](operations.md) — health checks and the 401 troubleshooting row.
- [api.md](api.md) — which admin routes the bearer token gates.
